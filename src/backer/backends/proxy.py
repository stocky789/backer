"""Proxy backend for server-side local directory backups.

The proxy backend allows agents to use server-side local directories as backup
destinations. Agents stream backup data to the server via HTTP, which then
executes the actual backend (restic/kopia/etc.) locally on a configured path.

This enables:
- Docker containers to use server-local paths without complex mounts
- Cross-platform compatibility (Windows/Linux agents)
- Server-side storage management without agent filesystem access
- Reverse proxy support (Cloudflare, nginx, etc.)
- Memory-safe chunked streaming for large backups/restores

Key Features:
- HTTP Basic authentication with agent credentials (client_id:client_secret)
- Automatic retries with exponential backoff (Tenacity)
- HTTPS/TLS support with configurable CA bundles (BACKER_SSL_VERIFY env)
- Session pooling for efficient connection reuse
- Chunked streaming (1MB default) to prevent memory exhaustion on large transfers
- SSL error logging with helpful guidance for dev environments
"""

import base64
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backer.backends.base import (
    BackendBase,
    BackendResult,
    BackendType,
    BackupDestination,
    BackupSource,
    OperationType,
)

logger = logging.getLogger(__name__)


class ProxyBackend(BackendBase):
    """Backend that proxies operations to a remote Backer server.

    URI format: proxy://server[:port]/repo/{repo_id}[?password=xxx]

    Examples:
    - proxy://localhost:8420/repo/repo-123
    - proxy://backer.example.com/repo/repo-456?password=secret
    """

    backend_type = BackendType.PROXY

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.server_url: str | None = None
        self.repo_id: str | None = None
        self.password: str | None = None
        self.client_id: str | None = None
        self.client_secret: str | None = None
        self.capability: str | None = self.config.get("proxy_capability")
        self.timeout: int = 300  # 5 minutes for large transfers
        self.chunk_size: int = 1024 * 1024 * 5  # 5MB chunks
        self.verify_ssl: bool | str = True  # SSL verification (True, False, or path to CA bundle)

        # Create persistent session for connection pooling
        self.session = requests.Session()

        # Parse location from config
        location = self.config.get("location", "")
        if location:
            self._parse_proxy_uri(location)

        # Get agent credentials from config or environment
        self.client_id = self.config.get("client_id") or os.getenv("BACKER_CLIENT_ID")
        self.client_secret = self.config.get("client_secret") or os.getenv("BACKER_CLIENT_SECRET")

        # TLS verification is mandatory; this may only select a custom CA bundle.
        ssl_verify = os.getenv("BACKER_SSL_VERIFY", "true").lower()
        if ssl_verify == "true":
            self.verify_ssl = True
        else:
            if os.path.exists(ssl_verify):
                self.verify_ssl = ssl_verify
            else:
                logger.warning(f"SSL CA bundle not found at {ssl_verify}, using default verification")

        self.session.verify = self.verify_ssl

        # Get JWT bearer token from environment (for token-based auth)
        self.bearer_token: str | None = os.getenv("BACKER_AGENT_TOKEN")
        if self.bearer_token:
            logger.debug("[PROXY] Using JWT bearer token for authentication")

    def _parse_proxy_uri(self, location: str) -> None:
        """Parse proxy://server/repo/{id} or proxys://server/repo/{id} URI.

        Schemes:
        - proxy://  : Use HTTP (insecure, dev only)
        - proxys:// : Use HTTPS (secure, production)
        """
        try:
            parsed = urlparse(location)

            if parsed.scheme not in ("proxy", "proxys"):
                raise ValueError(f"Invalid scheme: {parsed.scheme}, expected 'proxy' or 'proxys'")

            # Determine protocol from URI scheme
            protocol = "https" if parsed.scheme == "proxys" else "http"

            # Build server URL
            host = parsed.hostname or "localhost"
            port = f":{parsed.port}" if parsed.port else ""
            self.server_url = f"{protocol}://{host}{port}"

            # Extract repo ID from path (/repo/{id})
            path_parts = parsed.path.strip("/").split("/")
            if len(path_parts) < 2 or path_parts[0] != "repo":
                raise ValueError(f"Invalid path: {parsed.path}, expected /repo/{{id}}")

            self.repo_id = path_parts[1]

            # Password comes from config, not URI (security best practice)
            self.password = self.config.get("password") or os.getenv("REPO_PASSWORD")

            logger.debug(
                f"[PROXY] Initialized proxy backend: {self.server_url}/repo/{self.repo_id} (protocol={protocol})"
            )

        except Exception as e:
            logger.error(f"[PROXY] Failed to parse proxy URI '{location}': {e}")
            raise

    def _get_auth_headers(self) -> dict[str, str]:
        """Get HTTP headers with authentication (JWT token or HTTP Basic auth).

        Priority:
        1. JWT Bearer token (BACKER_AGENT_TOKEN env var) - preferred for security
        2. HTTP Basic auth (client_id:client_secret) - fallback
        """
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "backer-agent/proxy",
        }
        if self.capability:
            headers["X-Backer-Capability"] = self.capability

        # Prefer JWT bearer token if available
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
            logger.debug("[PROXY] Using JWT bearer token for request")
        # Fall back to HTTP Basic authentication (agent credentials)
        elif self.client_id and self.client_secret:
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
            logger.debug("[PROXY] Using HTTP Basic auth for request")
        else:
            logger.warning("[PROXY] No authentication method configured (bearer token or credentials)")

        return headers

    def _stream_to_file(
        self,
        response: requests.Response,
        destination: Path,
        progress_callback: Any | None = None,
    ) -> tuple[int, int]:
        """Stream response data to file with memory-safe chunked reading.

        Args:
            response: HTTP response with stream=True
            destination: Path to write data to
            progress_callback: Optional callback(bytes_received, total_bytes) for progress

        Returns:
            (bytes_written, chunks_count)
        """
        bytes_written = 0
        chunks_written = 0
        total_size = int(response.headers.get("content-length", 0))

        try:
            with open(destination, "wb") as f:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if chunk:  # filter keep-alive chunks
                        f.write(chunk)
                        bytes_written += len(chunk)
                        chunks_written += 1

                        # Call progress callback if provided
                        if progress_callback:
                            try:
                                progress_callback(bytes_written, total_size)
                            except Exception as e:
                                logger.warning(f"[PROXY] Progress callback error: {e}")

                        # Log progress periodically (every 100 chunks = ~500MB)
                        if chunks_written % 100 == 0:
                            logger.debug(
                                f"[PROXY] Streamed {bytes_written / 1024 / 1024:.1f}MB in {chunks_written} chunks"
                            )

            logger.info(f"[PROXY] Successfully streamed {bytes_written / 1024 / 1024:.1f}MB in {chunks_written} chunks")
            return bytes_written, chunks_written
        except Exception as e:
            logger.error(f"[PROXY] Error streaming to file {destination}: {e}")
            # Clean up partial file on error
            try:
                destination.unlink()
            except Exception:
                pass
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.RequestException, OSError)),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        json_data: Any = None,
        data: bytes | None = None,
        stream: bool = False,
        timeout: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        """Make HTTP request with automatic retries via tenacity."""
        if not self.server_url or not self.repo_id:
            raise ValueError("Proxy not initialized (server_url or repo_id missing)")

        url = f"{self.server_url}/api/repo/{self.repo_id}{path}"
        timeout = timeout or self.timeout
        headers = self._get_auth_headers()

        # Merge extra headers if provided
        if extra_headers:
            headers.update(extra_headers)

        # Use persistent session with SSL verification settings
        try:
            if method == "GET":
                response = self.session.get(
                    url,
                    headers=headers,
                    stream=stream,
                    timeout=timeout,
                    verify=self.verify_ssl,
                )
            elif method == "POST":
                response = self.session.post(
                    url,
                    json=json_data,
                    data=data,
                    headers=headers,
                    stream=stream,
                    timeout=timeout,
                    verify=self.verify_ssl,
                )
            else:
                raise ValueError(f"Unsupported method: {method}")

            # Handle HTTP error status codes
            if response.status_code >= 400:
                error_detail = response.text[:500] if response.text else "No response body"
                logger.error(
                    f"[PROXY] HTTP {response.status_code} error:\n"
                    f"URL: {url}\n"
                    f"Method: {method}\n"
                    f"Response: {error_detail}"
                )
                response.raise_for_status()

            return response
        except requests.exceptions.SSLError as e:
            logger.error(f"[PROXY] SSL verification failed: {e}")
            if self.verify_ssl is True:
                logger.error("[PROXY] Set BACKER_SSL_VERIFY to a trusted CA bundle path if required")
            raise
        except requests.exceptions.Timeout:
            logger.error(f"[PROXY] Request timeout after {timeout}s: {url}")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"[PROXY] Connection failed: {url} - {e}")
            raise
        except requests.exceptions.HTTPError:
            # Already logged above in status_code check
            raise
        except Exception as e:
            logger.error(f"[PROXY] Unexpected error during {method} {url}: {e}")
            raise

    def check_available(self) -> tuple[bool, str]:
        """Check if proxy server is available."""
        try:
            response = self._request(
                "GET",
                "/check",
                timeout=10,
            )
            data = response.json()
            return True, f"Proxy: {data.get('message', 'OK')}"
        except Exception as e:
            return False, f"Proxy server unavailable: {e}"

    def init_repo(self, destination: BackupDestination) -> BackendResult:
        """Proxy repositories are initialized by the server, not agents."""
        started_at = datetime.now()
        error = "Proxy backend initialization is server-managed"
        logger.warning(f"[PROXY] {error}")
        return BackendResult(
            success=False,
            operation=OperationType.BACKUP,
            started_at=started_at,
            finished_at=datetime.now(),
            errors=[error],
            return_code=1,
        )

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Stream backup data to server via tar archive.

        Creates a tar.gz archive of the source directory and streams it
        to the server, which extracts it to the configured local path.
        """
        import tarfile
        import tempfile

        started_at = datetime.now()
        bytes_transferred = 0
        files_transferred = 0

        try:
            logger.info(f"[PROXY] Backup starting: {source.path} -> {self.server_url}/repo/{self.repo_id}")

            if dry_run:
                logger.info("[PROXY] Dry run - not actually transferring data")
                return BackendResult(
                    success=True,
                    operation=OperationType.BACKUP,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    output="Dry run completed (no data transferred)",
                    return_code=0,
                )

            # Verify source exists
            source_path = Path(source.path)
            if not source_path.exists():
                raise FileNotFoundError(f"Source path does not exist: {source_path}")

            # Create tar archive in memory-efficient way using temp file
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                logger.info(f"[PROXY] Creating tar archive of {source_path}")

                # Build exclude filter
                def exclude_filter(tarinfo: Any) -> Any | None:
                    for pattern in source.excludes:
                        if pattern in tarinfo.name or tarinfo.name.endswith(pattern):
                            return None
                    return tarinfo

                # Create compressed tar archive
                # Use .as_posix() for arcname to ensure portable paths (forward slashes)
                # that work when extracting on both Windows and Linux
                with tarfile.open(tmp_path, "w:gz") as tar:
                    # Add files with progress tracking
                    if source_path.is_file():
                        tar.add(str(source_path), arcname=source_path.name, filter=exclude_filter)
                        files_transferred = 1
                    else:
                        for item in source_path.rglob("*"):
                            if item.is_file():
                                arcname = item.relative_to(source_path).as_posix()
                                member = tar.gettarinfo(str(item), arcname=arcname)
                                if exclude_filter(member):
                                    with open(item, "rb") as f:
                                        tar.addfile(member, f)
                                    files_transferred += 1

                                    if progress_callback and files_transferred % 100 == 0:
                                        try:
                                            progress_callback(
                                                bytes_done=0,
                                                files_done=files_transferred,
                                                current_file=arcname,
                                            )
                                        except Exception:
                                            pass

                # Get archive size
                archive_size = Path(tmp_path).stat().st_size
                logger.info(f"[PROXY] Archive created: {archive_size / 1024 / 1024:.1f}MB, {files_transferred} files")

                # Stream upload to server
                logger.info(f"[PROXY] Uploading to {self.server_url}/api/repo/{self.repo_id}/backup")

                # Extract just the job subfolder from the destination path
                # destination.path is the full proxy URI like:
                #   proxy://192.168.0.158:8420/repo/5279b837/Agents/testjob
                # We need just: Agents/testjob
                dest_subfolder = ""
                if destination.path:
                    dest_str = str(destination.path)
                    # Check if it's a proxy URI and extract the path after /repo/{id}/
                    if "/repo/" in dest_str:
                        # Split on /repo/{repo_id}/ and take what's after
                        parts = dest_str.split("/repo/", 1)
                        if len(parts) > 1:
                            # parts[1] is like: "5279b837/Agents/testjob"
                            # Skip the repo_id and get the rest
                            subparts = parts[1].split("/", 1)
                            if len(subparts) > 1:
                                dest_subfolder = subparts[1]  # "Agents/testjob"
                    elif not dest_str.startswith(("proxy://", "proxys://", "http://", "https://")):
                        # It's a local path, use as-is
                        dest_subfolder = dest_str

                logger.debug(f"[PROXY] Using subfolder: {dest_subfolder}")

                with open(tmp_path, "rb") as f:
                    # Use chunked upload for memory efficiency
                    headers = self._get_auth_headers()
                    headers["Content-Type"] = "application/gzip"
                    headers["Content-Length"] = str(archive_size)
                    headers["X-Backup-Subfolder"] = dest_subfolder
                    headers["X-Source-Path"] = str(source_path)

                    response = self.session.post(
                        f"{self.server_url}/api/repo/{self.repo_id}/backup",
                        data=f,
                        headers=headers,
                        timeout=self.timeout,
                        verify=self.verify_ssl,
                    )

                    if response.status_code >= 400:
                        error_detail = response.text[:500] if response.text else "No response body"
                        raise RuntimeError(f"Upload failed (HTTP {response.status_code}): {error_detail}")

                    result = response.json()
                    bytes_transferred = archive_size

                logger.info(f"[PROXY] Backup completed: {bytes_transferred / 1024 / 1024:.1f}MB uploaded")

                # Extract snapshot_id from server response for kopia backups
                metadata = {}
                if result.get("snapshot_id"):
                    metadata["snapshot_id"] = result["snapshot_id"]
                    logger.info(f"[PROXY] Server returned snapshot_id: {result['snapshot_id']}")

                return BackendResult(
                    success=result.get("success", True),
                    operation=OperationType.BACKUP,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    output=result.get("message", "Backup streamed via proxy"),
                    bytes_transferred=bytes_transferred,
                    files_transferred=files_transferred,
                    return_code=0 if result.get("success") else 1,
                    errors=result.get("errors", []),
                    metadata=metadata,
                )

            finally:
                # Clean up temp file
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[PROXY] Backup failed: {e}")
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=1,
            )

    def restore(
        self,
        source: BackupDestination,
        destination: Path,
        snapshot: str | None = None,
        dry_run: bool = False,
        progress_callback: Any | None = None,
        original_source_path: str | None = None,
        include_path: str | None = None,
    ) -> BackendResult:
        """Stream restore data from server with memory-safe chunked reading.

        Retrieves the backup from the server via tar.gz and extracts to destination.
        """
        import tarfile
        import tempfile

        started_at = datetime.now()
        bytes_transferred = 0
        files_transferred = 0

        try:
            logger.info(f"[PROXY] Restore starting: {self.server_url}/repo/{self.repo_id} -> {destination}")

            if dry_run:
                logger.info("[PROXY] Dry run - not actually restoring data")
                return BackendResult(
                    success=True,
                    operation=OperationType.RESTORE,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    output="Dry run completed (no data restored)",
                    return_code=0,
                )

            # Extract the job subfolder from the source path
            # source.path is the full proxy URI like:
            #   proxy://192.168.0.158:8420/repo/5279b837/Agents/testjob
            # We need just: Agents/testjob
            source_subfolder = ""
            if source.path:
                source_str = str(source.path)
                # Check if it's a proxy URI and extract the path after /repo/{id}/
                if "/repo/" in source_str:
                    # Split on /repo/{repo_id}/ and take what's after
                    parts = source_str.split("/repo/", 1)
                    if len(parts) > 1:
                        # parts[1] is like: "5279b837/Agents/testjob"
                        # Skip the repo_id and get the rest
                        subparts = parts[1].split("/", 1)
                        if len(subparts) > 1:
                            source_subfolder = subparts[1]  # "Agents/testjob"
                elif not source_str.startswith(("proxy://", "proxys://", "http://", "https://")):
                    # It's a local path, use as-is
                    source_subfolder = source_str

            logger.debug(f"[PROXY] Using restore subfolder: {source_subfolder}")

            # Build restore request with optional snapshot
            path = "/restore"
            if snapshot:
                path += f"?snapshot={snapshot}"
            if dry_run:
                sep = "&" if snapshot else "?"
                path += f"{sep}dry_run=true"
            if original_source_path:
                sep = "&" if (snapshot or dry_run) else "?"
                path += f"{sep}original_source_path={original_source_path}"

            logger.info(f"[PROXY] Requesting restore from {self.server_url}/api/repo/{self.repo_id}{path}")

            # Build extra headers for restore subfolder
            restore_headers = {}
            if source_subfolder:
                restore_headers["X-Restore-Subfolder"] = source_subfolder

            # Stream response to temp file
            response = self._request(
                "GET",
                path,
                stream=True,
                timeout=self.timeout * 2,  # Double timeout for restore
                extra_headers=restore_headers if restore_headers else None,
            )

            # Verify response
            if response.status_code >= 400:
                error_detail = response.text[:500] if response.text else "No response body"
                raise RuntimeError(f"Restore failed (HTTP {response.status_code}): {error_detail}")

            # Create destination directory
            destination.parent.mkdir(parents=True, exist_ok=True)

            # Stream to temp file
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = tmp.name
                bytes_transferred = 0

                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if chunk:
                        tmp.write(chunk)
                        bytes_transferred += len(chunk)

                        # Progress callback every 5MB
                        if progress_callback and bytes_transferred % (5 * 1024 * 1024) == 0:
                            try:
                                progress_callback(
                                    bytes_done=bytes_transferred,
                                    files_done=0,
                                    current_file="Downloading archive",
                                )
                            except Exception:
                                pass

            logger.info(f"[PROXY] Downloaded {bytes_transferred / 1024 / 1024:.1f}MB")

            # Extract tar archive to destination
            try:
                with tarfile.open(tmp_path, "r:gz") as tar:
                    tar.extractall(path=str(destination))
                    members = tar.getnames()
                    files_transferred = len(members)

                logger.info(f"[PROXY] Extracted {files_transferred} files to {destination}")

                return BackendResult(
                    success=True,
                    operation=OperationType.RESTORE,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    output=f"Restore completed: {files_transferred} files, {bytes_transferred / 1024 / 1024:.1f}MB",
                    bytes_transferred=bytes_transferred,
                    files_transferred=files_transferred,
                    return_code=0,
                )

            finally:
                # Clean up temp file
                try:
                    Path(tmp_path).unlink()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[PROXY] Restore failed: {e}")
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=1,
            )

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """Proxy capabilities do not authorize repository-wide snapshot listing."""
        logger.warning("[PROXY] Listing snapshots is not supported by agent proxy capabilities")
        return []

    def prune(
        self,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Proxy capabilities do not authorize repository-wide pruning."""
        started_at = datetime.now()
        error = "Proxy backend pruning is not supported by agent proxy capabilities"
        logger.warning(f"[PROXY] {error}")
        return BackendResult(
            success=False,
            operation=OperationType.PRUNE,
            started_at=started_at,
            finished_at=datetime.now(),
            errors=[error],
            return_code=1,
        )

    def check(
        self,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Repository-wide integrity checks are not agent proxy operations."""
        started_at = datetime.now()
        error = "Proxy backend integrity checks are not supported by agent proxy capabilities"
        logger.warning(f"[PROXY] {error}")
        return BackendResult(
            success=False,
            operation=OperationType.CHECK,
            started_at=started_at,
            finished_at=datetime.now(),
            errors=[error],
            return_code=1,
        )
    def cleanup(self) -> None:
        """Close the persistent HTTP session."""
        if hasattr(self, "session") and self.session:
            try:
                self.session.close()
                logger.debug("[PROXY] Session closed")
            except Exception as e:
                logger.warning(f"[PROXY] Error closing session: {e}")

    def __del__(self) -> None:
        """Cleanup resources when backend is destroyed."""
        try:
            self.cleanup()
        except Exception:
            pass  # Ignore errors during cleanup

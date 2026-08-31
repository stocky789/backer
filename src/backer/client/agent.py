"""Backer agent - runs on client machines and executes backups."""

import logging
import os
import platform
import shutil
import signal
import socket
import subprocess  # noqa: F401
import sys
import tempfile
import threading
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from backer import __version__
from backer.backends import get_backend
from backer.backends.base import BackupDestination, BackupSource
from backer.core.config import ClientConfig, load_config
from backer.core.mounts import (
    check_cifs_available,
    check_nfs_available,
    is_nfs_path,
    is_smb_path,
    nfs_mount_context,
    parse_nfs_path,
    parse_smb_path,
    smb_mount_context,
)
from backer.core.paths import get_config_dir, get_data_dir  # noqa: F401
from backer.core.repo_metadata import RepositoryMetadata

logger = logging.getLogger(__name__)

_SENSITIVE_OPTION_PARTS = (
    "password",
    "token",
    "secret",
    "access_key",
    "api_key",
    "private_key",
    "authorization",
    "credential",
    "proxy_capability",
)


def _redact_repository_options(value: Any) -> Any:
    """Return a safe-to-log copy of backend options."""
    if isinstance(value, dict):
        return {
            key: "***"
            if any(part in str(key).lower() for part in _SENSITIVE_OPTION_PARTS)
            else _redact_repository_options(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_repository_options(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_repository_options(item) for item in value)
    return value


def _log_repository_options(operation: str, options: dict[str, Any]) -> None:
    print(f"[{operation}] Repository options: {_redact_repository_options(options)}")


def _backend_for_location(location: str, options: dict[str, Any]):
    lowered = location.lower()
    if lowered.startswith(("proxy://", "proxys://")):
        return get_backend("proxy", {**options, "location": location})
    if "://" in location and not lowered.startswith("s3://"):
        raise RuntimeError(f"Unsupported repository location: {location}")
    return get_backend("kopia", options)


class BackerAgent:
    """Agent that runs on client machines.

    Responsibilities:
    - Register with the server
    - Send periodic heartbeats
    - Execute backup commands from server
    - Report results back to server
    """

    def __init__(
        self,
        server_url: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        config_path: Path | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.config_path = config_path or get_config_dir() / "config.yaml"
        self.hostname = socket.gethostname()

        # Threading synchronization
        self._stop_event = threading.Event()  # Replaces _running boolean
        self._http_client_lock = threading.Lock()  # Protects HTTP client
        self._active_jobs_lock = threading.Lock()  # Protects active jobs set
        self._active_jobs: set[str] = set()  # Track running job names

        self._heartbeat_thread: threading.Thread | None = None
        self._http_client: httpx.Client | None = None
        self._smb_manager: Any | None = None

    def _get_client(self) -> httpx.Client:
        """Get or create HTTP client (thread-safe)."""
        with self._http_client_lock:
            if self._http_client is None:
                auth = None
                if self.client_id and self.client_secret:
                    auth = (self.client_id, self.client_secret)
                self._http_client = httpx.Client(
                    base_url=self.server_url,
                    auth=auth,
                    timeout=35.0,  # Server uses 25s long-polling, need longer timeout
                )
            return self._http_client

    def register(self, enrollment_token: str | None = None) -> tuple[str, str]:
        """Register this agent with the server.

        Returns:
            Tuple of (client_id, client_secret)
        """
        client = self._get_client()

        response = client.post(
            "/api/v1/clients/register",
            json={
                "hostname": self.hostname,
                "version": __version__,
                "os_info": f"{platform.system()} {platform.release()}",
                "tags": [],
                "enrollment_token": enrollment_token,
            },
        )
        response.raise_for_status()
        data = response.json()

        self.client_id = data["client_id"]
        self.client_secret = data["client_secret"]

        # Save credentials
        self._save_credentials()

        # Recreate client with auth (thread-safe)
        with self._http_client_lock:
            self._http_client = None

        return self.client_id, self.client_secret

    def _save_credentials(self) -> None:
        """Save client credentials to config file."""
        config = load_config(self.config_path)
        config.agent_id = self.client_id or config.agent_id
        config.server = ClientConfig(
            server_url=self.server_url, client_id=self.client_id or "", client_secret=self.client_secret or ""
        )
        config.save(self.config_path)

    @classmethod
    def from_config(cls, config_path: Path | None = None) -> "BackerAgent":
        """Load agent from saved configuration."""
        config_path = config_path or get_config_dir() / "config.yaml"
        if config_path.exists():
            config = load_config(config_path)
            if config.server is None:
                raise FileNotFoundError(f"Agent config not found: {config_path}")
            return cls(config.server.server_url, config.server.client_id, config.server.client_secret, config_path)
        legacy_path = get_config_dir() / "agent.yaml"
        if not legacy_path.exists():
            raise FileNotFoundError(f"Agent config not found: {config_path}")
        import yaml

        legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8")) or {}
        return cls(legacy["server_url"], legacy.get("client_id"), legacy.get("client_secret"), legacy_path)

    def heartbeat(self) -> dict[str, Any]:
        """Send heartbeat to server and get any pending commands."""
        client = self._get_client()

        response = client.post(
            "/api/v1/clients/heartbeat",
            json={
                "client_id": self.client_id,
                "status": "online",
            },
        )
        response.raise_for_status()
        return response.json()

    def _heartbeat_loop(self, interval: int = 60) -> None:
        """Background heartbeat loop.

        Uses long-polling: the server holds the heartbeat request until
        a command is available (up to 25s), so we get commands instantly.
        We only sleep briefly between heartbeats to allow quick shutdown.

        The interval parameter is only used on connection errors to avoid
        hammering the server.
        """
        while not self._stop_event.is_set():
            try:
                result = self.heartbeat()
                # Process any pending commands
                for cmd in result.get("commands", []):
                    self._handle_command(cmd)
            except Exception as e:
                print(f"Heartbeat failed: {e}")
                # On error, wait before retry to avoid hammering server
                if self._stop_event.wait(timeout=5):
                    break
                continue

            # Brief pause between heartbeats (server handles the waiting)
            if self._stop_event.wait(timeout=1):
                break

    def _handle_command(self, command: dict[str, Any]) -> None:
        """Handle a command from the server."""
        cmd_type = command.get("command_type")
        cmd_id = command.get("id")
        payload = command.get("payload", {})

        print(f"Received command: {cmd_type} (id={cmd_id})")

        try:
            if cmd_type == "backup":
                # Run backup in background thread so heartbeat continues
                job_data = {**command, **payload}
                dry_run = payload.get("dry_run", False)
                job_name = job_data.get("job_name", "unknown")

                # Check if this job is already running
                with self._active_jobs_lock:
                    if job_name in self._active_jobs:
                        print(f"[WARN] Job '{job_name}' is already running, skipping duplicate")
                        return
                    self._active_jobs.add(job_name)

                backup_thread = threading.Thread(
                    target=self._run_backup_worker,
                    args=(job_data, dry_run, job_name, cmd_id),
                    daemon=True,
                    name=f"backup-{job_name}",
                )
                backup_thread.start()

            elif cmd_type == "restore":
                # Run restore in background thread so heartbeat continues
                dry_run = payload.get("dry_run", False)
                job_name = payload.get("job_name", "unknown")

                # Check if this job is already running
                with self._active_jobs_lock:
                    restore_key = f"restore-{job_name}"
                    if restore_key in self._active_jobs:
                        print(f"[WARN] Restore '{job_name}' is already running, skipping duplicate")
                        return
                    self._active_jobs.add(restore_key)

                restore_thread = threading.Thread(
                    target=self._run_restore_worker,
                    args=(payload, dry_run, restore_key, cmd_id),
                    daemon=True,
                    name=f"restore-{job_name}",
                )
                restore_thread.start()

            elif cmd_type == "browse_filesystem":
                # Browse is quick, run synchronously
                self._execute_browse_filesystem(payload)
            else:
                print(f"Unknown command: {cmd_type}")
                return

            # Synchronous commands are complete; workers acknowledge themselves.
            if cmd_id and cmd_type not in {"backup", "restore"}:
                self._acknowledge_command(cmd_id)

        except Exception as e:
            print(f"Command {cmd_id} failed: {e}")

    def _run_backup_worker(self, job_data: dict[str, Any], dry_run: bool, job_name: str, cmd_id: int | None) -> None:
        """Worker thread for executing backups without blocking heartbeat."""
        try:
            self.execute_backup(job_data, dry_run=dry_run)
        except Exception as e:
            print(f"Backup worker failed: {e}")
            import traceback

            traceback.print_exc()
        finally:
            if cmd_id:
                self._acknowledge_command(cmd_id)
            # Remove from active jobs when done
            with self._active_jobs_lock:
                self._active_jobs.discard(job_name)

    def _run_restore_worker(self, payload: dict[str, Any], dry_run: bool, restore_key: str, cmd_id: int | None) -> None:
        """Worker thread for executing restores without blocking heartbeat."""
        try:
            self.execute_restore(payload, dry_run=dry_run)
        except Exception as e:
            print(f"Restore worker failed: {e}")
            import traceback

            traceback.print_exc()
        finally:
            if cmd_id:
                self._acknowledge_command(cmd_id)
            # Remove from active jobs when done
            with self._active_jobs_lock:
                self._active_jobs.discard(restore_key)

    def _acknowledge_command(self, command_id: int) -> None:
        """Acknowledge that a command was processed."""
        try:
            client = self._get_client()
            client.post(f"/api/v1/commands/{command_id}/ack")
        except Exception as e:
            print(f"Failed to acknowledge command {command_id}: {e}")

    def _execute_browse_filesystem(self, payload: dict[str, Any]) -> None:
        """Execute filesystem browse command and report results."""
        request_id = payload.get("request_id")
        if not request_id:
            print("[BROWSE] Missing request_id in payload")
            return

        path = payload.get("path", "")
        print(f"[BROWSE] Browsing filesystem at: {path or '(root)'}")

        try:
            entries = []

            if not path:
                # Return root directories - Home and /
                home = Path.home()
                if home.exists():
                    entries.append(
                        {
                            "name": "Home",
                            "path": str(home),
                            "is_dir": True,
                            "size": 0,
                        }
                    )
                entries.append(
                    {
                        "name": "/",
                        "path": "/",
                        "is_dir": True,
                        "size": 0,
                    }
                )
                actual_path = ""
            else:
                # List contents of the specified path
                browse_path = Path(path)
                actual_path = str(browse_path)

                if not browse_path.exists():
                    raise FileNotFoundError(f"Path does not exist: {path}")

                if not browse_path.is_dir():
                    raise NotADirectoryError(f"Path is not a directory: {path}")

                # Use os.scandir for better performance
                dirs = []
                files = []

                try:
                    with os.scandir(str(browse_path)) as scanner:
                        for entry in scanner:
                            try:
                                # Use cached is_dir from scandir (much faster)
                                is_dir = entry.is_dir(follow_symlinks=False)

                                item_entry = {
                                    "name": entry.name,
                                    "path": entry.path,
                                    "is_dir": is_dir,
                                    "size": 0,
                                }

                                if is_dir:
                                    dirs.append(item_entry)
                                else:
                                    # Get file size from cached stat
                                    try:
                                        item_entry["size"] = entry.stat().st_size
                                    except (OSError, PermissionError):
                                        pass
                                    files.append(item_entry)

                                # Stop early if we have enough entries
                                if len(dirs) + len(files) >= 500:
                                    break

                            except (OSError, PermissionError):
                                # Skip items we can't access
                                continue
                except PermissionError:
                    raise PermissionError(f"Permission denied: {path}")

                # Sort directories and files by name, then combine (dirs first)
                dirs.sort(key=lambda x: x["name"].lower())
                files.sort(key=lambda x: x["name"].lower())
                entries = (dirs + files)[:200]

            # Report results to server
            client = self._get_client()
            client.post(
                f"/api/v1/browse/{request_id}/results",
                json={
                    "success": True,
                    "path": actual_path,
                    "entries": entries,
                },
            )

            print(f"[BROWSE] Sent {len(entries)} entries for path: {path or '(root)'}")

        except Exception as e:
            print(f"[BROWSE] Failed to browse {path}: {e}")
            # Report error to server
            try:
                client = self._get_client()
                client.post(
                    f"/api/v1/browse/{request_id}/results",
                    json={
                        "success": False,
                        "path": path,
                        "entries": [],
                        "error": str(e),
                    },
                )
            except Exception as report_err:
                print(f"[BROWSE] Failed to report error: {report_err}")

    def _report_progress(
        self,
        run_id: str,
        status: str | None = None,
        progress_percent: int | None = None,
        current_file: str | None = None,
        bytes_processed: int | None = None,
        files_processed: int | None = None,
        message: str | None = None,
    ) -> None:
        """Report progress update to server."""
        try:
            client = self._get_client()
            client.post(
                "/api/v1/progress",
                json={
                    "run_id": run_id,
                    "status": status,
                    "progress_percent": progress_percent,
                    "current_file": current_file,
                    "bytes_processed": bytes_processed,
                    "files_processed": files_processed,
                    "message": message,
                },
            )
        except Exception as e:
            print(f"Failed to report progress: {e}")

    def _is_smb_path(self, path: str) -> bool:
        return is_smb_path(path)

    def _is_nfs_path(self, path: str) -> bool:
        return is_nfs_path(path)

    def _parse_smb_path(self, path: str) -> tuple[str, str, str]:
        return parse_smb_path(path)

    def _parse_nfs_path(self, path: str) -> tuple[str, str, str]:
        return parse_nfs_path(path)

    def _check_cifs_available(self) -> bool:
        return check_cifs_available()

    def _check_nfs_available(self) -> bool:
        return check_nfs_available()

    def _smb_mount_context(
        self,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> Generator[Path, None, None]:
        return smb_mount_context(server, share, username, password, domain, cifs_check=self._check_cifs_available)

    def _nfs_mount_context(self, server: str, export_path: str) -> Generator[Path, None, None]:
        return nfs_mount_context(server, export_path, nfs_check=self._check_nfs_available)

    def _prepare_destination_for_backend(
        self,
        job: dict[str, Any],
        backend_name: str,
    ) -> tuple[str, Any]:
        """Prepare the destination path for the backend.

        On Linux, SMB and NFS paths need special handling:
        - For Kopia: Mount the share/export first
        - For proxy backend: Use destination_path as-is (it's a proxy:// URI)

        Returns:
            Tuple of (destination_path, cleanup_context_or_none)
        """
        dest_path = job.get("destination_path", "")

        # Proxy backend uses destination_path as a proxy:// URI directly
        # Don't try to mount it as a filesystem path
        if backend_name == "proxy" or dest_path.startswith(("proxy://", "proxys://")):
            return dest_path, None

        # Windows can use UNC paths directly, but does not support the NFS
        # destination format used by the agent protocol.
        if sys.platform == "win32":
            if self._is_nfs_path(dest_path) or (job.get("nfs_server") and job.get("nfs_export")):
                raise RuntimeError("NFS destinations are not supported on Windows")
            self._prepare_windows_smb(dest_path, job)
            return dest_path, None

        # Handle SMB paths
        if self._is_smb_path(dest_path):
            return self._prepare_smb_destination(job, backend_name, dest_path)

        # Check if NFS credentials were passed (job linked to NFS repository)
        # This takes priority over parsing dest_path as NFS, because the server
        # provides the actual NFS export separately from the subpath
        nfs_server = job.get("nfs_server")
        nfs_export = job.get("nfs_export")
        if nfs_server and nfs_export:
            # Server provided NFS export - mount the export and calculate subpath
            # dest_path will be like: server:/export/path/Agents/jobname
            # nfs_export is the actual mountable export: /export/path
            # We need to extract the subpath after the export
            print(f"[NFS] Using NFS repository: {nfs_server}:{nfs_export}")

            # Calculate subpath: everything in dest_path after the export
            # dest_path format: "server:/export/subpath" or just "/local/path"
            subpath = ""
            if self._is_nfs_path(dest_path):
                # Parse the full destination to get the path portion
                _, full_path, _ = self._parse_nfs_path(dest_path)
                # Remove the export prefix to get subpath
                if full_path.startswith(nfs_export):
                    subpath = full_path[len(nfs_export) :].lstrip("/")
                else:
                    # Export doesn't match - maybe path format differs, use full path as subpath
                    subpath = full_path.lstrip("/")

            ctx = self._nfs_mount_context(server=nfs_server, export_path=nfs_export)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[NFS] Using mounted path: {full_path}")
            return full_path, ctx

        # Handle NFS paths (without server-provided credentials)
        if self._is_nfs_path(dest_path):
            return self._prepare_nfs_destination(job, backend_name, dest_path)

        # Local path, use as-is
        return dest_path, None

    def _prepare_smb_destination(
        self,
        job: dict[str, Any],
        backend_name: str,
        dest_path: str,
    ) -> tuple[str, Any]:
        """Prepare SMB destination path for the backend."""
        server, share, subpath = self._parse_smb_path(dest_path)

        # Get credentials from job (passed by server)
        smb_username = job.get("smb_username")
        smb_password = job.get("smb_password")
        smb_domain = job.get("smb_domain")

        if backend_name == "kopia":
            # Kopia uses a mounted filesystem path for SMB on Linux.
            print(f"[SMB] Mounting share for {backend_name} backend")
            ctx = self._smb_mount_context(
                server=server,
                share=share,
                username=smb_username,
                password=smb_password,
                domain=smb_domain,
            )
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[SMB] Using mounted path: {full_path}")
            return full_path, ctx

        else:
            # Unknown backend, try using path as-is
            print(f"[SMB] Warning: Unknown backend '{backend_name}', using path as-is")
            return dest_path, None

    def _prepare_nfs_destination(
        self,
        job: dict[str, Any],
        backend_name: str,
        dest_path: str,
    ) -> tuple[str, Any]:
        """Prepare NFS destination path for the backend."""
        server, export_path, subpath = self._parse_nfs_path(dest_path)

        if backend_name == "kopia":
            # Kopia needs a mounted filesystem path.
            print(f"[NFS] Mounting NFS export for {backend_name} backend")
            ctx = self._nfs_mount_context(server=server, export_path=export_path)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[NFS] Using mounted path: {full_path}")
            return full_path, ctx

        else:
            # Unknown backend, try mounting anyway
            print(f"[NFS] Warning: Unknown backend '{backend_name}', mounting NFS export")
            ctx = self._nfs_mount_context(server=server, export_path=export_path)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            return full_path, ctx

    def execute_backup(
        self,
        job: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a backup job."""
        # Use run_id from command payload if provided, otherwise generate one
        run_id = job.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S")
        started_at = datetime.now()
        job_name = job.get("job_name", "unknown")
        backend_name = "repository"

        print(f"[BACKUP] Starting job '{job_name}' with backend '{backend_name}'")
        print(f"[BACKUP] Source: {job.get('source_path')}")
        print(f"[BACKUP] Destination: {job.get('destination_path')}")

        # Report that we're starting
        self._report_progress(
            run_id=run_id,
            status="running",
            progress_percent=0,
            message="Initializing backup...",
        )

        smb_cleanup_ctx = None
        try:
            repository_options = job.get("repository_options", {}).copy()

            # For proxy backend, destination_path IS the location URI
            # Also include agent credentials for authentication
            if job.get("destination_path", "").lower().startswith(("proxy://", "proxys://")):
                repository_options["client_id"] = self.client_id
                repository_options["client_secret"] = self.client_secret

            _log_repository_options("BACKUP", repository_options)
            backend = _backend_for_location(job.get("destination_path", ""), repository_options)
            is_proxy = job.get("destination_path", "").lower().startswith(("proxy://", "proxys://"))
            backend_name = "proxy" if is_proxy else "kopia"

            print("[BACKUP] Checking backend availability...")
            available, message = backend.check_available()
            if not available:
                print(f"[BACKUP] Backend not available: {message}")
                raise RuntimeError(f"Backend not available: {message}")
            print(f"[BACKUP] Backend ready: {message}")

            # Prepare destination path (handles SMB on Linux)
            print(f"[BACKUP] Preparing destination path for {backend_name} backend...")
            dest_path, smb_cleanup_ctx = self._prepare_destination_for_backend(job, backend_name)
            print(f"[BACKUP] Using destination: {dest_path}")

            self._report_progress(
                run_id=run_id,
                status="running",
                progress_percent=5,
                message="Backend ready, starting transfer...",
            )

            source = BackupSource(
                path=Path(job["source_path"]).expanduser(),
                excludes=job.get("excludes", []),
            )

            destination = BackupDestination(path=dest_path)

            # Create progress callback for backends that support it
            def progress_callback(
                bytes_done: int = 0,
                files_done: int = 0,
                current_file: str = "",
                total_bytes: int = 0,
            ) -> None:
                # Calculate percentage (leave 5% for init, 5% for finish)
                percent = 5
                if total_bytes > 0:
                    percent = 5 + int((bytes_done / total_bytes) * 90)
                elif files_done > 0:
                    # Estimate based on files if no byte info
                    percent = min(5 + files_done, 95)

                self._report_progress(
                    run_id=run_id,
                    status="running",
                    progress_percent=percent,
                    current_file=current_file[:200] if current_file else None,
                    bytes_processed=bytes_done,
                    files_processed=files_done,
                )

            # Check if backend supports progress callback
            supports_progress = (
                hasattr(backend.backup, "__code__") and "progress_callback" in backend.backup.__code__.co_varnames
            )

            print(f"[BACKUP] Executing backup: {source.path} -> {dest_path}")
            result = backend.backup(
                source=source,
                destination=destination,
                dry_run=dry_run,
                progress_callback=progress_callback if supports_progress else None,
            )

            finished_at = datetime.now()

            # Log the result
            if result.success:
                print(f"[BACKUP] Job '{job_name}' completed successfully")
                print(f"[BACKUP] Transferred: {result.bytes_transferred} bytes, {result.files_transferred} files")
            else:
                print(f"[BACKUP] Job '{job_name}' completed with errors")
                print(f"[BACKUP] Return code: {result.return_code}")
                if result.errors:
                    print(f"[BACKUP] Errors: {result.errors[:5]}")  # First 5 errors
                if result.output:
                    print(f"[BACKUP] Output (last 1000 chars): {result.output[-1000:]}")

            self._report_progress(
                run_id=run_id,
                status="finishing",
                progress_percent=95,
                message="Finalizing backup...",
            )

            # Report result to server
            # Extract snapshot_id from backend metadata.
            snapshot_id = None
            if hasattr(result, "metadata") and result.metadata:
                snapshot_id = result.metadata.get("snapshot_id")
            if snapshot_id:
                print(f"[BACKUP] Captured snapshot ID: {snapshot_id}")

            report = {
                "run_id": run_id,
                "job_name": job_name,
                "client_id": self.client_id,
                "success": result.success,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": result.bytes_transferred,
                "files_transferred": result.files_transferred,
                "errors": result.errors,
                "output": result.output[:5000],
                "snapshot_id": snapshot_id,
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as e:
                print(f"Failed to report result: {e}")

            # Write metadata to repository for discovery. Written for failed
            # runs too, so a repository doesn't read as an unbroken run of
            # green when a job has actually been failing.
            original_dest = job.get("destination_path", "")
            self._write_repo_metadata(
                job=job,
                dest_path=original_dest,
                backend_name=backend_name,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
                snapshot_id=snapshot_id,
            )

            return report

        except Exception as e:
            import traceback

            finished_at = datetime.now()
            error_msg = str(e)
            error_trace = traceback.format_exc()

            print(f"[BACKUP] Job '{job_name}' FAILED: {error_msg}")
            print(f"[BACKUP] Traceback:\n{error_trace}")

            self._report_progress(
                run_id=run_id,
                status="failed",
                progress_percent=0,
                message=error_msg[:200],
            )

            report = {
                "run_id": run_id,
                "job_name": job_name,
                "client_id": self.client_id,
                "success": False,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": 0,
                "files_transferred": 0,
                "errors": [error_msg],
                "output": error_trace[:5000],
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as report_err:
                print(f"[BACKUP] Failed to report error to server: {report_err}")

            return report

        finally:
            # Clean up SMB mount if used
            if smb_cleanup_ctx is not None:
                try:
                    smb_cleanup_ctx.__exit__(None, None, None)
                except Exception as cleanup_err:
                    print(f"[SMB] Cleanup error: {cleanup_err}")

    def _prepare_source_for_backend(
        self,
        job: dict[str, Any],
        backend_name: str,
    ) -> tuple[str, Any]:
        """Prepare source path for the backend, mounting SMB/NFS if needed.

        Returns:
            Tuple of (prepared_path, cleanup_context).
            cleanup_context should be used to unmount if not None.
        """
        source_path = job.get("source_path", "")

        # Proxy backend uses source_path as a proxy:// URI directly
        # Don't try to mount it as a filesystem path
        if backend_name == "proxy" or source_path.startswith(("proxy://", "proxys://")):
            print(f"[RESTORE] Using proxy URI directly: {source_path}")
            return source_path, None

        # Windows can use UNC paths directly, but does not support the NFS
        # source format used by the agent protocol.
        if sys.platform == "win32":
            if self._is_nfs_path(source_path) or (job.get("nfs_server") and job.get("nfs_export")):
                raise RuntimeError("NFS restores are not supported on Windows")
            self._prepare_windows_smb(source_path, job)
            return source_path, None

        # Check if NFS credentials were passed (job linked to NFS repository)
        # This takes priority over parsing source_path as NFS, because the server
        # provides the actual NFS export separately from the subpath
        nfs_server = job.get("nfs_server")
        nfs_export = job.get("nfs_export")
        if nfs_server and nfs_export:
            # Server provided NFS export - mount the export and calculate subpath
            # source_path will be like: server:/export/path/Agents/jobname
            # nfs_export is the actual mountable export: /export/path
            # We need to extract the subpath after the export
            print(f"[RESTORE] Using NFS repository: {nfs_server}:{nfs_export}")

            # Calculate subpath: everything in source_path after the export
            subpath = ""
            if self._is_nfs_path(source_path):
                # Parse the full source to get the path portion
                _, full_path, _ = self._parse_nfs_path(source_path)
                # Remove the export prefix to get subpath
                if full_path.startswith(nfs_export):
                    subpath = full_path[len(nfs_export) :].lstrip("/")
                else:
                    # Export doesn't match - maybe path format differs, use full path as subpath
                    subpath = full_path.lstrip("/")

            ctx = self._nfs_mount_context(server=nfs_server, export_path=nfs_export)
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[RESTORE] Using mounted path: {full_path}")
            return full_path, ctx

        # Check for NFS path (server:/export format) without explicit credentials
        if self._is_nfs_path(source_path):
            print(f"[RESTORE] Detected NFS source path: {source_path}")
            return self._prepare_nfs_source(job, backend_name, source_path)

        # Check for SMB path (//server/share or \\server\share format)
        if self._is_smb_path(source_path):
            print(f"[RESTORE] Detected SMB source path: {source_path}")
            return self._prepare_smb_source(job, backend_name, source_path)

        # Local path, use as-is
        return source_path, None

    def _prepare_windows_smb(self, path: str, job: dict[str, Any]) -> None:
        """Open the SMB session once before either backup or restore uses a UNC path."""
        if not self._is_smb_path(path):
            return
        server, share, _ = self._parse_smb_path(path)
        if self._smb_manager is None:
            from backer.agent.service import SMBConnectionManager

            self._smb_manager = SMBConnectionManager()
        if not self._smb_manager.connect(
            server, share, job.get("smb_username"), job.get("smb_password"), job.get("smb_domain")
        ):
            raise RuntimeError(f"Failed to connect to SMB share: \\\\{server}\\{share}")

    def _prepare_smb_source(
        self,
        job: dict[str, Any],
        backend_name: str,
        source_path: str,
    ) -> tuple[str, Any]:
        """Prepare SMB source path for restore."""
        server, share, subpath = self._parse_smb_path(source_path)

        # Get credentials from job (passed by server)
        smb_username = job.get("smb_username")
        smb_password = job.get("smb_password")
        smb_domain = job.get("smb_domain")

        if backend_name == "kopia":
            # Kopia needs a mounted filesystem path.
            print(f"[RESTORE] Mounting SMB share for {backend_name} backend")
            ctx = self._smb_mount_context(
                server=server,
                share=share,
                username=smb_username,
                password=smb_password,
                domain=smb_domain,
            )
            mount_path = ctx.__enter__()
            full_path = str(mount_path / subpath) if subpath else str(mount_path)
            print(f"[RESTORE] Using mounted path: {full_path}")
            return full_path, ctx

        else:
            print(f"[RESTORE] Warning: Unknown backend '{backend_name}', using path as-is")
            return source_path, None

    def _prepare_nfs_source(
        self,
        job: dict[str, Any],
        backend_name: str,
        source_path: str,
    ) -> tuple[str, Any]:
        """Prepare NFS source path for restore.

        Note: This is called when nfs_server/nfs_export were NOT provided explicitly.
        It parses the full NFS path and extracts server, export, and subpath.
        """
        server, export_path, subpath = self._parse_nfs_path(source_path)

        # All backends need mounted path for NFS
        print(f"[RESTORE] Mounting NFS export {server}:{export_path} for {backend_name} backend")
        ctx = self._nfs_mount_context(server=server, export_path=export_path)
        mount_path = ctx.__enter__()
        full_path = str(mount_path / subpath) if subpath else str(mount_path)
        print(f"[RESTORE] Using mounted path: {full_path}")
        return full_path, ctx

    def execute_restore(
        self,
        job: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute a restore job."""
        run_id = job.get("run_id") or f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        started_at = datetime.now()
        job_name = job.get("job_name", "unknown")
        backend_name = "repository"

        print(f"[RESTORE] Starting restore for job '{job_name}' with backend '{backend_name}'")
        print(f"[RESTORE] Source (backup repo): {job.get('source_path')}")
        print(f"[RESTORE] Destination: {job.get('destination_path')}")

        # Report that we're starting restore
        self._report_progress(
            run_id=run_id,
            status="running",
            progress_percent=0,
            message="Initializing restore...",
        )

        mount_cleanup_ctx = None
        try:
            repository_options = job.get("repository_options", {}).copy()

            # For proxy backend, source_path IS the location URI
            # Also include agent credentials for authentication
            if job.get("source_path", "").lower().startswith(("proxy://", "proxys://")):
                repository_options["client_id"] = self.client_id
                repository_options["client_secret"] = self.client_secret

            _log_repository_options("RESTORE", repository_options)
            backend = _backend_for_location(job.get("source_path", ""), repository_options)
            is_proxy = job.get("source_path", "").lower().startswith(("proxy://", "proxys://"))
            backend_name = "proxy" if is_proxy else "kopia"

            print("[RESTORE] Checking backend availability...")
            available, message = backend.check_available()
            if not available:
                print(f"[RESTORE] Backend not available: {message}")
                raise RuntimeError(f"Backend not available: {message}")
            print(f"[RESTORE] Backend ready: {message}")

            # Prepare source path (handles SMB/NFS mounting)
            print(f"[RESTORE] Preparing source path for {backend_name} backend...")
            source_path, mount_cleanup_ctx = self._prepare_source_for_backend(job, backend_name)
            print(f"[RESTORE] Prepared source path: {source_path}")

            self._report_progress(
                run_id=run_id,
                status="running",
                progress_percent=5,
                message="Backend ready, starting restore...",
            )

            source = BackupDestination(path=source_path)
            destination = Path(job["destination_path"])

            # Validate the selected repository/snapshot before a clean restore
            # removes anything.  A dry-run is non-mutating for the supported
            # backends except Kopia, which explicitly rejects it.
            clean_restore = job.get("clean_restore", False)
            restore_snapshot = job.get("snapshot")
            staged_destination: Path | None = None
            if clean_restore and not dry_run:
                if backend_name == "proxy":
                    raise RuntimeError(
                        "Clean restore is not supported for proxy backends because "
                        "the server cannot yet validate a restore without modifying files"
                    )
                if backend_name == "kopia":
                    snapshots = backend.list_snapshots(source)
                    selected = job.get("snapshot")
                    if not snapshots or (
                        selected
                        and selected != "latest"
                        and not any(selected in (item.get("id"), item.get("full_id")) for item in snapshots)
                    ):
                        raise RuntimeError(
                            "Clean restore requires an accessible Kopia repository and selected snapshot"
                        )
                else:
                    validation = backend.restore(
                        source=source,
                        destination=destination,
                        snapshot=restore_snapshot,
                        dry_run=True,
                        original_source_path=job.get("original_source_path"),
                        include_path=job.get("source_subfolder") or None,
                    )
                    if not validation.success:
                        raise RuntimeError("Clean restore validation failed: " + "; ".join(validation.errors))
                resolved_destination = destination.resolve()
                if resolved_destination == resolved_destination.parent:
                    raise RuntimeError("Clean restore refuses to replace a filesystem root")

                print(f"[RESTORE] Clean restore enabled - staging destination: {destination}")
                self._report_progress(
                    run_id=run_id,
                    status="running",
                    progress_percent=3,
                    message="Clean restore: staging existing files...",
                )
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_dir():
                            raise RuntimeError("Clean restore destination must be a non-symlink directory")
                        destination_mode = destination.stat().st_mode & 0o7777
                        staged_destination = Path(tempfile.mkdtemp(prefix=".backer-restore-", dir=destination.parent))
                        staged_destination.rmdir()
                        destination.replace(staged_destination)
                        try:
                            destination.mkdir(mode=destination_mode)
                            destination.chmod(destination_mode)
                        except Exception as setup_err:
                            try:
                                if destination.exists():
                                    destination.rmdir()
                                staged_destination.replace(destination)
                            except Exception as rollback_err:
                                raise RuntimeError(
                                    "Clean restore setup rollback failed; original destination remains at "
                                    f"{staged_destination}: {rollback_err}"
                                ) from rollback_err
                            raise RuntimeError(
                                f"Clean restore failed to prepare destination: {setup_err}"
                            ) from setup_err
                        print("[RESTORE] Staged destination directory contents")
                    else:
                        destination.mkdir(parents=True, exist_ok=True)
                        print("[RESTORE] Created destination directory")
                except Exception as setup_err:
                    raise RuntimeError(f"Clean restore failed to prepare destination: {setup_err}") from setup_err

            # Pass original_source_path for Kopia snapshot lookup.
            original_source_path = job.get("original_source_path")
            if original_source_path:
                print(f"[RESTORE] Original source path for snapshot lookup: {original_source_path}")

            try:
                result = backend.restore(
                    source=source,
                    destination=destination,
                    snapshot=restore_snapshot,
                    dry_run=dry_run,
                    original_source_path=original_source_path,
                    include_path=job.get("source_subfolder") or None,
                )
            except Exception:
                if clean_restore and not dry_run:
                    try:
                        if destination.exists():
                            if destination.is_dir() and not destination.is_symlink():
                                shutil.rmtree(destination)
                            else:
                                destination.unlink()
                        if staged_destination:
                            staged_destination.replace(destination)
                    except Exception as rollback_err:
                        raise RuntimeError(
                            "Clean restore rollback failed; original destination remains at "
                            f"{staged_destination}: {rollback_err}"
                        ) from rollback_err
                raise

            # A clean restore that reports success but left the destination without
            # any actual file content is more dangerous than an outright failure:
            # when an original was staged it would be discarded below and the run
            # reported green while the user's data is simply gone; when nothing was
            # staged (fresh destination) it would report a DR restore as complete
            # when nothing was written. Directories alone don't count - Kopia exits
            # 0 and creates the tree for a snapshot containing only directories.
            # Treat "restored no files" as a failure so the rollback path below
            # restores the staged original (or removes the empty output) instead.
            if clean_restore and not dry_run and result.success:
                if not destination.exists() or not any(p.is_file() for p in destination.rglob("*")):
                    result.success = False
                    result.errors.append(
                        "Clean restore produced no files; keeping original destination"
                        if staged_destination
                        else "Clean restore produced no files"
                    )

            if clean_restore and not dry_run and not result.success:
                try:
                    if destination.exists():
                        if destination.is_dir() and not destination.is_symlink():
                            shutil.rmtree(destination)
                        else:
                            destination.unlink()
                    if staged_destination:
                        staged_destination.replace(destination)
                except Exception as rollback_err:
                    result.errors.append(
                        "Clean restore rollback failed; original destination remains at "
                        f"{staged_destination}: {rollback_err}"
                    )
            elif staged_destination:
                try:
                    shutil.rmtree(staged_destination)
                except Exception as cleanup_err:
                    result.warnings.append(
                        "Clean restore succeeded but could not remove staged files at "
                        f"{staged_destination}: {cleanup_err}"
                    )

            finished_at = datetime.now()

            self._report_progress(
                run_id=run_id,
                status="finishing",
                progress_percent=95,
                message="Finalizing restore...",
            )

            # Report result to server
            output = getattr(result, "output", "")
            if result.warnings:
                output = "\n".join((output, *(f"WARNING: {warning}" for warning in result.warnings)))
            report = {
                "run_id": run_id,
                "job_name": f"restore:{job_name}",
                "client_id": self.client_id,
                "success": result.success,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": getattr(result, "bytes_transferred", 0),
                "files_transferred": result.files_transferred,
                "errors": result.errors,
                "output": output[:5000],
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception as e:
                print(f"Failed to report restore result: {e}")

            # Write restore metadata to repository for audit trail (only for non-proxy)
            if result.success and backend_name != "proxy":
                try:
                    self._write_restore_metadata(
                        source_path=source_path,
                        job_name=job_name,
                        run_id=run_id,
                        result=result,
                        started_at=started_at,
                        finished_at=finished_at,
                        snapshot=job.get("snapshot"),
                    )
                except Exception as meta_err:
                    print(f"[RESTORE] Warning - failed to write restore metadata: {meta_err}")

            return report

        except Exception as e:
            finished_at = datetime.now()

            self._report_progress(
                run_id=run_id,
                status="failed",
                progress_percent=0,
                message=str(e)[:200],
            )

            report = {
                "run_id": run_id,
                "job_name": f"restore:{job_name}",
                "client_id": self.client_id,
                "success": False,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": 0,
                "files_transferred": 0,
                "errors": [str(e)],
                "output": "",
            }

            try:
                client = self._get_client()
                client.post("/api/v1/results", json=report)
            except Exception:
                pass

            return report

        finally:
            # Clean up any mounted paths
            if mount_cleanup_ctx:
                try:
                    print("[RESTORE] Cleaning up mounted path...")
                    mount_cleanup_ctx.__exit__(None, None, None)
                    print("[RESTORE] Mount cleanup complete")
                except Exception as cleanup_err:
                    print(f"[RESTORE] Cleanup error: {cleanup_err}")

    def _write_repo_metadata(
        self,
        job: dict[str, Any],
        dest_path: str,
        backend_name: str,
        result: Any,
        started_at: datetime,
        finished_at: datetime,
        snapshot_id: str | None,
    ) -> None:
        """Write backup metadata to the repository for discovery.

        Enables a new Backer server to discover existing backups when pointed
        to a repository. For SMB shares on Linux, mounts the share first.
        """
        try:
            print(f"[METADATA] Writing metadata to repository: {dest_path}")

            job_name = job.get("job_name", "unknown")
            run_id = job.get("run_id", "unknown")
            source_path = job.get("source_path", "")

            # For SMB paths on Linux, we need to mount first
            if sys.platform != "win32" and self._is_smb_path(dest_path):
                server, share, subpath = self._parse_smb_path(dest_path)
                smb_username = job.get("smb_username")
                smb_password = job.get("smb_password")
                smb_domain = job.get("smb_domain")

                print(f"[METADATA] Mounting SMB share //{server}/{share} for metadata")
                with self._smb_mount_context(
                    server=server,
                    share=share,
                    username=smb_username,
                    password=smb_password,
                    domain=smb_domain,
                ) as mount_point:
                    if subpath:
                        local_path = mount_point / subpath
                    else:
                        local_path = mount_point
                    self._write_metadata_to_path(
                        local_path,
                        job_name,
                        run_id,
                        source_path,
                        backend_name,
                        result,
                        started_at,
                        finished_at,
                        snapshot_id,
                    )
            else:
                # Local path or Windows UNC path
                self._write_metadata_to_path(
                    Path(dest_path),
                    job_name,
                    run_id,
                    source_path,
                    backend_name,
                    result,
                    started_at,
                    finished_at,
                    snapshot_id,
                )

            print(f"[METADATA] Successfully wrote metadata to: {dest_path}")

        except Exception as e:
            # Don't fail the backup just because metadata writing failed, but
            # don't let the failure vanish into stdout either - a failed
            # metadata write can itself hide a failed run from a second
            # machine reading this repository.
            logger.error("[METADATA] Failed to write metadata to %s: %s", dest_path, e)

    def _write_metadata_to_path(
        self,
        repo_path: Path,
        job_name: str,
        run_id: str,
        source_path: str,
        backend_name: str,
        result: Any,
        started_at: datetime,
        finished_at: datetime,
        snapshot_id: str | None,
    ) -> None:
        """Write metadata files to a local path."""
        repo = RepositoryMetadata(repo_path)

        # Initialize repository metadata if needed
        if not repo.is_initialized():
            print(f"[METADATA] Initializing metadata directory at {repo.metadata_dir}")
            repo.initialize()

        # Save agent information
        repo.save_agent(
            agent_id=self.client_id,
            agent_data={
                "hostname": socket.gethostname(),
                "platform": sys.platform,
                "os_info": f"{platform.system()} {platform.release()}",
            },
        )

        # Save job configuration
        repo.save_job(
            job_name=job_name,
            job_config={
                "source_path": source_path,
                "client_id": self.client_id,
            },
        )

        # Save run record
        run_data = {
            "status": "success" if result.success else "failed",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "bytes_transferred": result.bytes_transferred,
            "files_transferred": result.files_transferred,
            "errors": result.errors,
            "snapshot_id": snapshot_id,
            "agent_id": self.client_id,
            "hostname": socket.gethostname(),
        }
        repo.save_job_run(job_name, run_id, run_data)

        # Save Kopia snapshot metadata.
        if snapshot_id and backend_name == "kopia":
            repo.save_snapshot(
                snapshot_id=snapshot_id,
                snapshot_data={
                    "job_name": job_name,
                    "run_id": run_id,
                    "hostname": socket.gethostname(),
                    "paths": [source_path],
                    "time": finished_at.isoformat(),
                },
            )

    def _write_restore_metadata(
        self,
        source_path: str,
        job_name: str,
        run_id: str,
        result: Any,
        started_at: datetime,
        finished_at: datetime,
        snapshot: str | None,
    ) -> None:
        """Write restore operation metadata to the repository for audit trail.

        Tracks restore operations for compliance and debugging purposes.
        """
        try:
            print(f"[RESTORE METADATA] Writing restore metadata to: {source_path}")

            # For SMB paths on Linux, we need to mount first
            if sys.platform != "win32" and self._is_smb_path(source_path):
                # Skip metadata for SMB on Linux (would need credentials)
                print("[RESTORE METADATA] Skipping SMB metadata write (not supported)")
                return

            repo_path = Path(source_path)
            repo = RepositoryMetadata(repo_path)

            if not repo.is_initialized():
                print("[RESTORE METADATA] Repository metadata not initialized, skipping")
                return

            # Save restore operation record
            restore_run_data = {
                "operation_type": "restore",
                "status": "success" if result.success else "failed",
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "bytes_transferred": getattr(result, "bytes_transferred", 0),
                "files_transferred": result.files_transferred,
                "snapshot_id": snapshot,
                "agent_id": self.client_id,
                "hostname": socket.gethostname(),
            }
            repo.save_job_run(job_name, run_id, restore_run_data)

            print(f"[RESTORE METADATA] Successfully wrote restore metadata for job '{job_name}'")

        except Exception as e:
            print(f"[RESTORE METADATA] Warning - failed to write metadata: {e}")

    def run(self, heartbeat_interval: int = 60) -> None:
        """Run the agent in daemon mode."""
        self._stop_event.clear()

        # Set up signal handlers (SIGTERM not available on Windows)
        signal.signal(signal.SIGINT, self._handle_signal)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._handle_signal)

        print(f"Backer agent starting (client_id: {self.client_id})")
        print(f"Connecting to server: {self.server_url}")

        # Start heartbeat thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(heartbeat_interval,),
            daemon=True,
        )
        self._heartbeat_thread.start()

        # Main loop - wait for stop signal
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            pass

        self.stop()

    def stop(self) -> None:
        """Stop the agent."""
        print("Stopping agent...")
        self._stop_event.set()

        with self._http_client_lock:
            if self._http_client:
                self._http_client.close()
                self._http_client = None

    def _handle_signal(self, signum: int, frame: object) -> None:
        """Handle shutdown signals gracefully.

        Sets stop event to allow current operations to complete
        before exiting. This ensures backup results are reported to the server.
        """
        print(f"\nReceived signal {signum}, initiating graceful shutdown...")
        self._stop_event.set()
        # Don't call sys.exit(0) immediately - let the main loop exit naturally
        # This allows any in-progress backup reporting to complete

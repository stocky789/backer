"""Kopia backend implementation."""

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from backer.backends.base import (
    BackendBase,
    BackendResult,
    BackendType,
    BackupDestination,
    BackupSource,
    OperationType,
)
from backer.backends.s3 import kopia_s3_config
from backer.tools.manager import get_tool_manager

logger = logging.getLogger(__name__)


class KopiaBackend(BackendBase):
    """Backend for Kopia deduplicated backups.

    Kopia provides:
    - Deduplication
    - Encryption
    - Snapshots
    - Multiple storage backends (filesystem, S3, Azure, GCS, etc.)
    - Compression
    - Content-addressable storage
    """

    backend_type = BackendType.KOPIA

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._tool_manager = get_tool_manager()
        self._binary_path: Path | None = None
        self._env = os.environ.copy()

        password = self.config.get("repository_password")
        if password:
            self._env["KOPIA_PASSWORD"] = password
        self._has_repository_password = bool(password)

    def _get_binary(self, auto_install: bool = True) -> Path:
        """Get path to kopia binary, downloading if necessary."""
        if self._binary_path and self._binary_path.exists():
            return self._binary_path

        path = self._tool_manager.get_tool_path("kopia")
        if path:
            self._binary_path = path
            return path

        if auto_install:
            self._binary_path = self._tool_manager.download("kopia")
            return self._binary_path

        raise RuntimeError("kopia not installed. Run 'backer setup' to install tools.")

    def check_available(self) -> tuple[bool, str]:
        """Check if kopia is available, downloading if needed."""
        try:
            binary = self._get_binary(auto_install=False)
        except RuntimeError:
            try:
                binary = self._get_binary(auto_install=True)
            except Exception as e:
                return False, f"kopia not available and auto-install failed: {e}"

        try:
            result = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False, f"kopia version check failed: {result.stderr.strip()}"
            return True, result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    def _get_repo_type(self, path: str) -> tuple[str, list[str]]:
        """Determine repository type and extra args from path.

        Returns:
            Tuple of (repo_type, extra_args)

        Raises:
            ValueError: If the path format is invalid (e.g., empty bucket name)
        """
        if path.startswith("s3://"):
            s3 = self.config.get("s3")
            if not isinstance(s3, dict):
                raise ValueError("S3 repository configuration is required")
            config = kopia_s3_config(s3)
            self._env.update(config["environment"])
            return "s3", config["options"]
        elif path.startswith("gs://"):
            # Google Cloud Storage
            remainder = path[5:]
            if not remainder or remainder.startswith("/"):
                raise ValueError(f"Invalid GCS path '{path}': bucket name is required")
            parts = remainder.split("/", 1)
            bucket = parts[0]
            prefix = parts[1] if len(parts) > 1 else ""
            return "gcs", ["--bucket", bucket, "--prefix", prefix]
        elif path.startswith("azure://"):
            # Azure Blob Storage
            remainder = path[8:]
            if not remainder or remainder.startswith("/"):
                raise ValueError(f"Invalid Azure path '{path}': container name is required")
            parts = remainder.split("/", 1)
            container = parts[0]
            prefix = parts[1] if len(parts) > 1 else ""
            return "azure", ["--container", container, "--prefix", prefix]
        elif path.startswith("sftp://"):
            # SFTP
            remainder = path[7:]
            if not remainder:
                raise ValueError(f"Invalid SFTP path '{path}': host/path is required")
            return "sftp", ["--path", remainder]
        else:
            # Local filesystem (default)
            if not path:
                raise ValueError("Repository path cannot be empty")
            return "filesystem", ["--path", path]

    def init_repo(self, destination: BackupDestination) -> BackendResult:
        """Initialize a new kopia repository."""
        started_at = datetime.now()

        if not self._has_repository_password:
            return BackendResult(
                success=False, operation=OperationType.BACKUP, started_at=started_at,
                finished_at=datetime.now(), errors=["Repository encryption password is required"], return_code=-1,
            )

        try:
            binary = self._get_binary()
            repo_type, repo_args = self._get_repo_type(destination.path)

            cmd = [str(binary), "repository", "create", repo_type]
            cmd.extend(repo_args)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=120,
            )

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                errors=[result.stderr] if result.returncode != 0 else [],
            )
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

    def _connect_repo(self, path: str) -> tuple[bool, str]:
        """Connect to an existing kopia repository."""
        if not self._has_repository_password:
            return False, "Repository encryption password is required"
        try:
            binary = self._get_binary()
            repo_type, repo_args = self._get_repo_type(path)

            cmd = [str(binary), "repository", "connect", repo_type]
            cmd.extend(repo_args)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60,
            )

            if result.returncode == 0:
                return True, "Connected to repository"
            else:
                return False, result.stderr.strip()
        except Exception as e:
            return False, str(e)

    def _disconnect_repo(self) -> None:
        """Disconnect from the current repository."""
        try:
            binary = self._get_binary()
            subprocess.run(
                [str(binary), "repository", "disconnect"],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=10,
            )
        except Exception:
            # Disconnect errors are non-fatal and expected when not connected
            pass

    def test_connection(self, destination: BackupDestination) -> tuple[bool, str]:
        """Test S3 access without creating a repository."""
        connected, message = self._connect_repo(destination.path)
        if connected:
            self._disconnect_repo()
            return True, "Repository is accessible"
        if "repository does not exist" in message.lower() or "not initialized" in message.lower():
            return True, "S3 is accessible; repository will be initialized on first backup"
        return False, message

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Run kopia backup (snapshot create)."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Connect to repository first, or initialize if it doesn't exist
        connected, err = self._connect_repo(destination.path)
        if not connected:
            # Repository might not exist - try to initialize it
            print(f"[KOPIA] Repository not found, attempting to initialize at {destination.path}")
            init_result = self.init_repo(destination)
            if not init_result.success:
                return BackendResult(
                    success=False,
                    operation=OperationType.BACKUP,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[f"Failed to initialize repository: {init_result.errors}"],
                    return_code=-1,
                )
            # Now try to connect again
            connected, err = self._connect_repo(destination.path)
            if not connected:
                return BackendResult(
                    success=False,
                    operation=OperationType.BACKUP,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[f"Failed to connect to repository after init: {err}"],
                    return_code=-1,
                )

        try:
            # Build snapshot command
            cmd = [str(binary), "snapshot", "create", "--json"]

            if dry_run:
                cmd.append("--dry-run")

            # Replace the source policy so changed or removed job excludes cannot leak into later runs.
            policy_cmd = [str(binary), "policy", "set", str(source.path), "--clear-ignore"]
            for exclude in source.excludes or []:
                policy_cmd.extend(["--add-ignore", exclude])
            policy = subprocess.run(
                policy_cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=self.config.get("timeout", 86400),
            )
            if policy.returncode != 0:
                return BackendResult(
                    success=False,
                    operation=OperationType.BACKUP,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[line for line in policy.stderr.split("\n") if line.strip()],
                    return_code=policy.returncode,
                )

            # Add tags from config
            for tag in self.config.get("tags", []):
                cmd.extend(["--tags", tag])

            cmd.append(str(source.path))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=self.config.get("timeout", 86400),
            )

            finished_at = datetime.now()

            # Parse JSON output for stats
            stats: dict[str, Any] = {}
            snapshot_id = None
            errors = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    # Kopia outputs different JSON structures
                    if "rootEntry" in data:
                        root = data.get("rootEntry", {})
                        summary = root.get("summ", {})
                        stats = {
                            "total_size": summary.get("size", 0),
                            "total_files": summary.get("files", 0),
                            "total_dirs": summary.get("dirs", 0),
                        }
                    if "id" in data:
                        snapshot_id = data.get("id")
                    elif "snapshotID" in data:
                        snapshot_id = data.get("snapshotID")
                except json.JSONDecodeError:
                    pass

            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("total_size", 0),
                files_transferred=stats.get("total_files", 0),
                errors=errors,
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                metadata={"snapshot_id": snapshot_id, **stats},
            )

        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["Backup operation timed out"],
                return_code=-1,
            )
        except OSError as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to execute kopia: {e}"],
                return_code=-1,
            )
        finally:
            self._disconnect_repo()

    def _find_latest_snapshot_for_source(self, source_path: str) -> str | None:
        """Find the latest snapshot ID for a given source path.

        Args:
            source_path: The original source path that was backed up

        Returns:
            The snapshot ID if found, None otherwise
        """
        try:
            binary = self._get_binary()

            # List snapshots filtered by source path
            # kopia snapshot list --json returns snapshots with source info
            result = subprocess.run(
                [str(binary), "snapshot", "list", "--json", "--all"],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60,
            )

            if result.returncode != 0 or not result.stdout.strip():
                print(f"[KOPIA] Failed to list snapshots: {result.stderr}")
                return None

            snapshots = json.loads(result.stdout)

            # Filter snapshots by source path and find the latest
            matching_snapshots = []
            for snap in snapshots:
                snap_source = snap.get("source", {}).get("path", "")
                # Check if the source path matches (normalize paths)
                if snap_source.rstrip("/") == source_path.rstrip("/"):
                    matching_snapshots.append(snap)

            if not matching_snapshots:
                print(f"[KOPIA] No snapshots found for source path: {source_path}")
                # Try a more lenient match (in case of hostname differences)
                for snap in snapshots:
                    snap_source = snap.get("source", {}).get("path", "")
                    if snap_source and source_path.endswith(snap_source.split("/")[-1]):
                        matching_snapshots.append(snap)

            if not matching_snapshots:
                print("[KOPIA] Still no matching snapshots. Available sources:")
                for snap in snapshots[:5]:  # Show first 5
                    print(f"  - {snap.get('source', {}).get('path', 'unknown')}")
                return None

            # Sort by start time (most recent first)
            matching_snapshots.sort(
                key=lambda x: x.get("startTime", ""),
                reverse=True
            )

            latest = matching_snapshots[0]
            snapshot_id = latest.get("id")
            print(f"[KOPIA] Found {len(matching_snapshots)} matching snapshot(s), using latest: {snapshot_id}")
            return snapshot_id

        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            print(f"[KOPIA] Error finding snapshot: {e}")
            return None

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
        """Restore from kopia snapshot."""
        started_at = datetime.now()

        if dry_run:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["Kopia restore dry runs are not supported"],
                return_code=-1,
            )

        try:
            binary = self._get_binary()
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Connect to repository first
        connected, err = self._connect_repo(source.path)
        if not connected:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to connect to repository: {err}"],
                return_code=-1,
            )

        try:
            # Determine the snapshot identifier to restore
            # If user provided a specific snapshot ID, use it directly
            # Only look up "latest" if no snapshot was specified
            snapshot_id = snapshot

            if not snapshot_id or snapshot_id == "latest":
                if original_source_path:
                    # Use the original source path to find the latest snapshot
                    print(f"[KOPIA] Looking up latest snapshot for source: {original_source_path}")
                    latest_id = self._find_latest_snapshot_for_source(original_source_path)
                    if latest_id:
                        snapshot_id = latest_id
                        print(f"[KOPIA] Found latest snapshot: {snapshot_id}")
                    else:
                        # Fall back to using the source path directly
                        snapshot_id = f"{original_source_path}@latest"
                        print(f"[KOPIA] Using source path qualifier: {snapshot_id}")
                else:
                    snapshot_id = "latest"
            else:
                print(f"[KOPIA] Using user-specified snapshot: {snapshot_id}")

            if include_path:
                snapshot_id = f"{snapshot_id.rstrip('/')}/{include_path.lstrip('/')}"

            # Kopia restore syntax: kopia snapshot restore <snapshot-id> <target-path>
            # Add flags to overwrite existing files and restore all content
            cmd = [
                str(binary), "snapshot", "restore",
                "--overwrite-files",
                "--overwrite-directories",
                "--overwrite-symlinks",
                snapshot_id,
                str(destination),
            ]

            print(f"[KOPIA] Running restore command: {' '.join(cmd)}")

            # Kopia doesn't have a direct --dry-run for restore
            # We could use --skip-existing or similar

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=self.config.get("timeout", 86400),
            )

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=errors,
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                metadata={"snapshot": snapshot_id},
            )

        except subprocess.TimeoutExpired:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["Restore operation timed out"],
                return_code=-1,
            )
        except OSError as e:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to execute kopia: {e}"],
                return_code=-1,
            )
        finally:
            self._disconnect_repo()

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List available kopia snapshots."""
        try:
            binary = self._get_binary()

            # Connect to repository first
            connected, _ = self._connect_repo(destination.path)
            if not connected:
                return []

            try:
                result = subprocess.run(
                    [str(binary), "snapshot", "list", "--json", "--all"],
                    capture_output=True,
                    text=True,
                    env=self._env,
                    timeout=60,
                )

                if result.returncode == 0 and result.stdout.strip():
                    snapshots = json.loads(result.stdout)
                    return [
                        {
                            "id": snap.get("id", "unknown")[:12],
                            "full_id": snap.get("id"),
                            "timestamp": snap.get("startTime"),
                            "hostname": snap.get("hostname"),
                            "username": snap.get("username"),
                            "paths": [snap.get("source", {}).get("path", "")],
                            "tags": snap.get("tags", []),
                            "size": snap.get("stats", {}).get("totalSize", 0),
                        }
                        for snap in snapshots
                    ]
            finally:
                self._disconnect_repo()

        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError) as e:
            logger.warning(f"[KOPIA] Failed to list snapshots: {e}")

        return []

    def prune(
        self,
        destination: BackupDestination,
        keep_last: int | None = None,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        dry_run: bool = False,
    ) -> BackendResult:
        """Prune old snapshots using kopia policy and maintenance."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Connect to repository first
        connected, err = self._connect_repo(destination.path)
        if not connected:
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[f"Failed to connect to repository: {err}"],
                return_code=-1,
            )

        try:
            # Set retention policy
            policy_cmd = [str(binary), "policy", "set", "--global"]

            if keep_last:
                policy_cmd.extend(["--keep-latest", str(keep_last)])
            if keep_daily:
                policy_cmd.extend(["--keep-daily", str(keep_daily)])
            if keep_weekly:
                policy_cmd.extend(["--keep-weekly", str(keep_weekly)])
            if keep_monthly:
                policy_cmd.extend(["--keep-monthly", str(keep_monthly)])

            # Apply policy
            policy_result = subprocess.run(
                policy_cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60,
            )

            if policy_result.returncode != 0:
                return BackendResult(
                    success=False,
                    operation=OperationType.PRUNE,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[policy_result.stderr],
                    output=policy_result.stdout + policy_result.stderr,
                    return_code=policy_result.returncode,
                )

            # Run snapshot expire to apply retention
            expire_cmd = [str(binary), "snapshot", "expire", "--all"]
            if dry_run:
                expire_cmd.append("--dry-run")

            expire_result = subprocess.run(
                expire_cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=self.config.get("timeout", 3600),
            )

            # Also run maintenance to clean up deleted data
            if not dry_run and expire_result.returncode == 0:
                maint_result = subprocess.run(
                    [str(binary), "maintenance", "run", "--full"],
                    capture_output=True,
                    text=True,
                    env=self._env,
                    timeout=self.config.get("timeout", 3600),
                )
                output = expire_result.stdout + expire_result.stderr + maint_result.stdout + maint_result.stderr
                return_code = maint_result.returncode
            else:
                output = expire_result.stdout + expire_result.stderr
                return_code = expire_result.returncode

            errors = []
            if return_code != 0:
                errors = [line for line in output.split("\n") if line.strip() and "error" in line.lower()]

            return BackendResult(
                success=return_code == 0,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=errors,
                output=output,
                return_code=return_code,
            )

        except (subprocess.TimeoutExpired, OSError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )
        finally:
            self._disconnect_repo()

    def check(self, destination: BackupDestination) -> BackendResult:
        """Check repository integrity using kopia maintenance."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()

            # Connect to repository first
            connected, err = self._connect_repo(destination.path)
            if not connected:
                return BackendResult(
                    success=False,
                    operation=OperationType.CHECK,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    errors=[f"Failed to connect to repository: {err}"],
                    return_code=-1,
                )

            try:
                # Kopia uses 'repository validate-client' for integrity checks
                result = subprocess.run(
                    [str(binary), "repository", "validate-client"],
                    capture_output=True,
                    text=True,
                    env=self._env,
                    timeout=self.config.get("timeout", 3600),
                )

                return BackendResult(
                    success=result.returncode == 0,
                    operation=OperationType.CHECK,
                    started_at=started_at,
                    finished_at=datetime.now(),
                    output=result.stdout + result.stderr,
                    return_code=result.returncode,
                )
            finally:
                self._disconnect_repo()

        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.CHECK,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

    def get_snapshot_files(
        self,
        destination: BackupDestination,
        snapshot_id: str,
        path: str = "",
    ) -> list[dict[str, Any]]:
        """List files in a snapshot (for browsing before restore)."""
        try:
            binary = self._get_binary()

            connected, _ = self._connect_repo(destination.path)
            if not connected:
                return []

            try:
                cmd = [str(binary), "snapshot", "list", "--json"]
                if path:
                    cmd.extend(["--path", path])
                cmd.append(snapshot_id)

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=self._env,
                    timeout=60,
                )

                if result.returncode == 0 and result.stdout.strip():
                    return json.loads(result.stdout)
            finally:
                self._disconnect_repo()

        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError) as e:
            logger.warning(f"[KOPIA] Failed to get snapshot files: {e}")

        return []

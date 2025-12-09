"""Restic backend implementation."""

import json
import logging
import os
import re
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
from backer.backends.registry import BackendRegistry
from backer.tools.manager import get_tool_manager

logger = logging.getLogger(__name__)


@BackendRegistry.register(BackendType.RESTIC)
class ResticBackend(BackendBase):
    """Backend for restic deduplicated backups.

    Restic provides:
    - Deduplication
    - Encryption
    - Snapshots
    - Multiple storage backends
    """

    backend_type = BackendType.RESTIC

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._tool_manager = get_tool_manager()
        self._binary_path: Path | None = None
        self._env = os.environ.copy()

        # Set repository password from config if provided
        # Check multiple keys for compatibility (UI uses restic_password)
        password = (
            self.config.get("password")
            or self.config.get("restic_password")
        )
        if password:
            self._env["RESTIC_PASSWORD"] = password
        elif "password_file" in self.config:
            self._env["RESTIC_PASSWORD_FILE"] = self.config["password_file"]
        elif "RESTIC_PASSWORD" not in self._env:
            # Use default password if none provided - allows auto-initialization
            self._env["RESTIC_PASSWORD"] = "backer-default-password"
            logger.debug("[RESTIC] Using default repository password")

    def _get_binary(self, auto_install: bool = True) -> Path:
        """Get path to restic binary, downloading if necessary."""
        if self._binary_path and self._binary_path.exists():
            return self._binary_path

        path = self._tool_manager.get_tool_path("restic")
        if path:
            self._binary_path = path
            return path

        if auto_install:
            self._binary_path = self._tool_manager.download("restic")
            return self._binary_path

        raise RuntimeError("restic not installed. Run 'backer setup' to install tools.")

    def check_available(self) -> tuple[bool, str]:
        """Check if restic is available, downloading if needed."""
        try:
            binary = self._get_binary(auto_install=False)
        except RuntimeError:
            try:
                binary = self._get_binary(auto_install=True)
            except Exception as e:
                return False, f"restic not available and auto-install failed: {e}"

        try:
            result = subprocess.run(
                [str(binary), "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True, result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    def init_repo(self, destination: BackupDestination) -> BackendResult:
        """Initialize a new restic repository."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "init", "--repo", destination.path],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60,
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

    def _build_backup_command(
        self,
        repo: str,
        source: str,
        excludes: list[str] | None = None,
        tags: list[str] | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Build restic backup command."""
        binary = self._get_binary()
        cmd = [str(binary), "backup", "--repo", repo, "--json"]

        if dry_run:
            cmd.append("--dry-run")

        for exclude in excludes or []:
            cmd.extend(["--exclude", exclude])

        for tag in tags or self.config.get("tags", []):
            cmd.extend(["--tag", tag])

        cmd.append(source)
        return cmd

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Run restic backup."""
        started_at = datetime.now()

        try:
            cmd = self._build_backup_command(
                repo=destination.path,
                source=str(source.path),
                excludes=source.excludes,
                dry_run=dry_run,
            )
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        # Check if repository exists, initialize if not
        try:
            binary = self._get_binary()
            check_cmd = [str(binary), "-r", destination.path, "snapshots", "--json"]
            check_result = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=30,
            )
            if check_result.returncode != 0 and "repository does not exist" in check_result.stderr.lower():
                print(f"[RESTIC] Repository not found, attempting to initialize at {destination.path}")
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
        except Exception as e:
            print(f"[RESTIC] Warning: Could not check repository: {e}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=self.config.get("timeout", 86400),
            )

            finished_at = datetime.now()

            stats: dict[str, Any] = {}
            snapshot_id = None
            errors = []

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("message_type") == "summary":
                        stats = {
                            "files_new": data.get("files_new", 0),
                            "files_changed": data.get("files_changed", 0),
                            "files_unmodified": data.get("files_unmodified", 0),
                            "data_added": data.get("data_added", 0),
                            "total_files_processed": data.get("total_files_processed", 0),
                            "total_bytes_processed": data.get("total_bytes_processed", 0),
                            "snapshot_id": data.get("snapshot_id"),
                        }
                        snapshot_id = data.get("snapshot_id")
                except json.JSONDecodeError:
                    pass

            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("data_added", 0),
                files_transferred=stats.get("files_new", 0) + stats.get("files_changed", 0),
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
                errors=[f"Failed to execute restic: {e}"],
                return_code=-1,
            )

    def restore(
        self,
        source: BackupDestination,
        destination: Path,
        snapshot: str | None = None,
        dry_run: bool = False,
        progress_callback: Any | None = None,
        original_source_path: str | None = None,
    ) -> BackendResult:
        """Restore from restic snapshot.

        Restic preserves full absolute paths in snapshots. When restoring:
        - If destination matches original_source_path, use --target / so files
          restore to their original absolute locations
        - Otherwise, restore to destination but files will be nested under the
          original path structure (e.g., dest/home/user/files/)
        """
        started_at = datetime.now()
        snapshot_id = snapshot or "latest"

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

        # Determine the correct target for restic
        # Restic stores absolute paths, so restoring with --target /dest creates /dest/original/path
        # If restoring to the original location, use --target / to restore to absolute paths
        target = str(destination)
        include_path = None

        print(f"[RESTIC] original_source_path: {original_source_path}")
        print(f"[RESTIC] destination: {destination}")

        if original_source_path:
            # Normalize paths for comparison
            orig_normalized = str(Path(original_source_path).resolve())
            dest_normalized = str(destination.resolve())

            print(f"[RESTIC] orig_normalized: {orig_normalized}")
            print(f"[RESTIC] dest_normalized: {dest_normalized}")

            if dest_normalized == orig_normalized or dest_normalized.rstrip('/') == orig_normalized.rstrip('/'):
                # Restoring to original location - use / as target so files go to absolute paths
                target = "/"
                include_path = original_source_path
                print("[RESTIC] Detected restore to original location, using --target /")
            else:
                # Restoring to different location
                print(f"[RESTIC] Restoring to different location: {destination}")
                print(f"[RESTIC] Files will be under: {destination}/{original_source_path.lstrip('/')}")
        else:
            print("[RESTIC] No original_source_path provided")

        cmd = [
            str(binary), "restore",
            "--repo", source.path,
            "--target", target,
            snapshot_id,
        ]

        # Include only the original source path if specified
        if include_path:
            cmd.extend(["--include", include_path])

        print(f"[RESTIC] Running command: {' '.join(cmd)}")

        if dry_run:
            cmd.append("--dry-run")

        try:
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

            # Parse restore stats from output
            files_restored = 0
            bytes_restored = 0
            output = result.stdout + result.stderr

            # Try newer format first: "Restored 3 files, 1 directories and 0 symbolic links (113 B)."
            new_pattern = r'Restored\s+(\d+)\s+files?,\s*(\d+)\s+director(?:y|ies).*?\((\d+(?:\.\d+)?)\s*(\w*)\)'
            new_match = re.search(new_pattern, output)
            if new_match:
                files_restored = int(new_match.group(1)) + int(new_match.group(2))  # files + directories
                size_val = float(new_match.group(3))
                size_unit = new_match.group(4).upper() if new_match.group(4) else 'B'
            else:
                # Try older format: "Restored 6 / 4 files/dirs (66 B / 66 B)"
                old_pattern = r'Restored\s+(\d+)\s*/\s*\d+\s+files/dirs\s+\((\d+(?:\.\d+)?)\s*(\w*)\s*/'
                old_match = re.search(old_pattern, output)
                if old_match:
                    files_restored = int(old_match.group(1))
                    size_val = float(old_match.group(2))
                    size_unit = old_match.group(3).upper() if old_match.group(3) else 'B'
                else:
                    size_val = 0
                    size_unit = 'B'

            # Convert to bytes
            if size_val > 0:
                if 'KIB' in size_unit or 'KB' in size_unit:
                    bytes_restored = int(size_val * 1024)
                elif 'MIB' in size_unit or 'MB' in size_unit:
                    bytes_restored = int(size_val * 1024 * 1024)
                elif 'GIB' in size_unit or 'GB' in size_unit:
                    bytes_restored = int(size_val * 1024 * 1024 * 1024)
                else:
                    bytes_restored = int(size_val)

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                files_transferred=files_restored,
                bytes_transferred=bytes_restored,
                errors=errors,
                output=output,
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
                errors=[f"Failed to execute restic: {e}"],
                return_code=-1,
            )

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List available restic snapshots."""
        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "snapshots", "--repo", destination.path, "--json"],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60,
            )

            if result.returncode == 0 and result.stdout.strip():
                snapshots = json.loads(result.stdout)
                return [
                    {
                        "id": snap.get("short_id", snap.get("id", "unknown")),
                        "full_id": snap.get("id"),
                        "timestamp": snap.get("time"),
                        "hostname": snap.get("hostname"),
                        "paths": snap.get("paths", []),
                        "tags": snap.get("tags", []),
                    }
                    for snap in snapshots
                ]
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError) as e:
            logger.warning(f"[RESTIC] Failed to list snapshots: {e}")

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
        """Prune old snapshots using restic forget + prune."""
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

        cmd = [str(binary), "forget", "--repo", destination.path, "--prune"]

        if dry_run:
            cmd.append("--dry-run")

        if keep_last:
            cmd.extend(["--keep-last", str(keep_last)])
        if keep_daily:
            cmd.extend(["--keep-daily", str(keep_daily)])
        if keep_weekly:
            cmd.extend(["--keep-weekly", str(keep_weekly)])
        if keep_monthly:
            cmd.extend(["--keep-monthly", str(keep_monthly)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=self.config.get("timeout", 3600),
            )

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.PRUNE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=errors,
                output=result.stdout + result.stderr,
                return_code=result.returncode,
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

    def check(self, destination: BackupDestination) -> BackendResult:
        """Check repository integrity."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "check", "--repo", destination.path],
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
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.CHECK,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

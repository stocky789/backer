"""Restic backend implementation."""

import json
import os
import shutil
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
        self._env = os.environ.copy()

        # Set repository password from config if provided
        if "password" in self.config:
            self._env["RESTIC_PASSWORD"] = self.config["password"]
        elif "password_file" in self.config:
            self._env["RESTIC_PASSWORD_FILE"] = self.config["password_file"]

    def check_available(self) -> tuple[bool, str]:
        """Check if restic is available."""
        restic_path = shutil.which("restic")
        if not restic_path:
            return False, "restic not found in PATH"

        try:
            result = subprocess.run(
                ["restic", "version"],
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
            result = subprocess.run(
                ["restic", "init", "--repo", destination.path],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60,
            )

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,  # No INIT type, use BACKUP
                started_at=started_at,
                finished_at=datetime.now(),
                output=result.stdout + result.stderr,
                return_code=result.returncode,
                errors=[result.stderr] if result.returncode != 0 else [],
            )
        except (subprocess.TimeoutExpired, OSError) as e:
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
        cmd = ["restic", "backup", "--repo", repo, "--json"]

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

        cmd = self._build_backup_command(
            repo=destination.path,
            source=str(source.path),
            excludes=source.excludes,
            dry_run=dry_run,
        )

        try:
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
    ) -> BackendResult:
        """Restore from restic snapshot."""
        started_at = datetime.now()

        snapshot_id = snapshot or "latest"

        cmd = [
            "restic", "restore",
            "--repo", source.path,
            "--target", str(destination),
            snapshot_id,
        ]

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
                errors=[f"Failed to execute restic: {e}"],
                return_code=-1,
            )

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List available restic snapshots."""
        try:
            result = subprocess.run(
                ["restic", "snapshots", "--repo", destination.path, "--json"],
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
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

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

        cmd = ["restic", "forget", "--repo", destination.path, "--prune"]

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
            result = subprocess.run(
                ["restic", "check", "--repo", destination.path],
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
        except (subprocess.TimeoutExpired, OSError) as e:
            return BackendResult(
                success=False,
                operation=OperationType.CHECK,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

"""Rsync backend implementation."""

import re
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


class RsyncBackend(BackendBase):
    """Backend for rsync file synchronization."""

    backend_type = BackendType.RSYNC

    # Default rsync options for backup
    DEFAULT_OPTIONS = [
        "-a",  # Archive mode (preserves permissions, timestamps, etc.)
        "-v",  # Verbose
        "--delete",  # Delete files in dest that don't exist in source
        "--stats",  # Show transfer statistics
    ]

    def check_available(self) -> tuple[bool, str]:
        """Check if rsync is available."""
        rsync_path = shutil.which("rsync")
        if not rsync_path:
            return False, "rsync not found in PATH"

        try:
            result = subprocess.run(
                ["rsync", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = result.stdout.strip().split("\n")
            version_line = lines[0] if lines else "rsync (unknown version)"
            return True, version_line
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    def _build_command(
        self,
        source: str,
        destination: str,
        excludes: list[str] | None = None,
        includes: list[str] | None = None,
        dry_run: bool = False,
        extra_options: list[str] | None = None,
    ) -> list[str]:
        """Build rsync command with options."""
        cmd = ["rsync"] + self.DEFAULT_OPTIONS.copy()

        if dry_run:
            cmd.append("--dry-run")

        # Add progress indicator
        cmd.append("--progress")

        # Add excludes
        for exclude in excludes or []:
            cmd.extend(["--exclude", exclude])

        # Add includes
        for include in includes or []:
            cmd.extend(["--include", include])

        # Add any extra options from config
        cmd.extend(extra_options or self.config.get("extra_options", []))

        # Source and destination
        cmd.append(source)
        cmd.append(destination)

        return cmd

    def _parse_stats(self, output: str) -> dict[str, Any]:
        """Parse rsync stats output."""
        stats: dict[str, Any] = {}

        patterns = {
            "files_transferred": r"Number of regular files transferred:\s*(\d+)",
            "total_size": r"Total file size:\s*([\d,]+)",
            "bytes_sent": r"Total bytes sent:\s*([\d,]+)",
            "bytes_received": r"Total bytes received:\s*([\d,]+)",
            "transfer_speed": r"([\d.]+)\s*bytes/sec",
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, output)
            if match:
                value = match.group(1).replace(",", "")
                stats[key] = int(float(value))

        return stats

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Run rsync backup."""
        started_at = datetime.now()

        # Ensure source path has trailing slash for rsync semantics
        source_path = str(source.path)
        if not source_path.endswith("/"):
            source_path += "/"

        cmd = self._build_command(
            source=source_path,
            destination=destination.path,
            excludes=source.excludes,
            includes=source.includes,
            dry_run=dry_run,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.get("timeout", 86400),  # Default 24h timeout
            )

            finished_at = datetime.now()
            stats = self._parse_stats(result.stdout + result.stderr)

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("bytes_sent", 0) + stats.get("bytes_received", 0),
                files_transferred=stats.get("files_transferred", 0),
                errors=errors,
                output=result.stdout,
                return_code=result.returncode,
                metadata=stats,
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
                errors=[f"Failed to execute rsync: {e}"],
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
        include_path: str | None = None,
    ) -> BackendResult:
        """Restore from rsync backup.

        Note: rsync doesn't have snapshot support - this just syncs back.
        The snapshot parameter is ignored.
        """
        started_at = datetime.now()

        # For restore, source and dest are swapped
        source_path = source.path
        if not source_path.endswith("/"):
            source_path += "/"

        cmd = self._build_command(
            source=source_path,
            destination=str(destination),
            dry_run=dry_run,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.get("timeout", 86400),
            )

            finished_at = datetime.now()
            stats = self._parse_stats(result.stdout + result.stderr)

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip()]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("bytes_sent", 0) + stats.get("bytes_received", 0),
                files_transferred=stats.get("files_transferred", 0),
                errors=errors,
                output=result.stdout,
                return_code=result.returncode,
                metadata=stats,
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
                errors=[f"Failed to execute rsync: {e}"],
                return_code=-1,
            )

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List snapshots - rsync doesn't support snapshots natively.

        Returns a single entry representing the current state of the backup.
        """
        dest_path = Path(destination.path)
        if dest_path.exists():
            return [
                {
                    "id": "current",
                    "path": str(dest_path),
                    "timestamp": datetime.fromtimestamp(dest_path.stat().st_mtime).isoformat(),
                    "note": "rsync does not support snapshots - this is the current backup state",
                }
            ]
        return []

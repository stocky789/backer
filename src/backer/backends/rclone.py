"""Rclone backend implementation."""

import json
import logging
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
from backer.tools.manager import get_tool_manager

logger = logging.getLogger(__name__)


class RcloneBackend(BackendBase):
    """Backend for rclone cloud sync.

    Supports syncing to any rclone-supported backend including:
    - S3 and S3-compatible storage
    - Google Drive
    - Backblaze B2
    - Azure Blob Storage
    - SFTP
    - Local filesystem
    - And many more...
    """

    backend_type = BackendType.RCLONE

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._tool_manager = get_tool_manager()
        self._binary_path: Path | None = None

    def _get_binary(self, auto_install: bool = True) -> Path:
        """Get path to rclone binary, downloading if necessary."""
        if self._binary_path and self._binary_path.exists():
            return self._binary_path

        path = self._tool_manager.get_tool_path("rclone")
        if path:
            self._binary_path = path
            return path

        if auto_install:
            self._binary_path = self._tool_manager.download("rclone")
            return self._binary_path

        raise RuntimeError("rclone not installed. Run 'backer setup' to install tools.")

    def check_available(self) -> tuple[bool, str]:
        """Check if rclone is available, downloading if needed."""
        try:
            binary = self._get_binary(auto_install=False)
        except RuntimeError:
            # Try to auto-install
            try:
                binary = self._get_binary(auto_install=True)
            except Exception as e:
                return False, f"rclone not available and auto-install failed: {e}"

        try:
            result = subprocess.run(
                [str(binary), "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False, f"rclone version check failed: {result.stderr.strip()}"
            lines = result.stdout.strip().split("\n")
            version_line = lines[0] if lines else "rclone (unknown version)"
            return True, version_line
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    def list_remotes(self) -> list[str]:
        """List configured rclone remotes."""
        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "listremotes"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]
        except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
            logger.warning(f"[RCLONE] Failed to list remotes: {e}")
        return []

    def _build_command(
        self,
        operation: str,
        source: str,
        destination: str,
        excludes: list[str] | None = None,
        dry_run: bool = False,
        extra_flags: list[str] | None = None,
    ) -> list[str]:
        """Build rclone command."""
        binary = self._get_binary()
        cmd = [str(binary), operation]

        # Common flags
        cmd.extend([
            "-v",  # Verbose - shows transfer summary
            "--stats", "0",  # Disable periodic stats, just show final summary
        ])

        if dry_run:
            cmd.append("--dry-run")

        # Add excludes
        for exclude in excludes or []:
            cmd.extend(["--exclude", exclude])

        # Config file if specified
        if "config_file" in self.config:
            cmd.extend(["--config", self.config["config_file"]])

        # Bandwidth limit if specified
        if "bwlimit" in self.config:
            cmd.extend(["--bwlimit", self.config["bwlimit"]])

        # Extra flags
        cmd.extend(extra_flags or self.config.get("extra_flags", []))

        # Source and destination
        cmd.append(source)
        cmd.append(destination)

        return cmd

    def _parse_output(self, output: str) -> dict[str, Any]:
        """Parse rclone output for stats.

        Rclone verbose output includes lines like:
            Transferred:   	  1.234 GiB / 5.678 GiB, 22%, 10.5 MiB/s, ETA 5m30s
            Transferred:        123 / 456, 27%
            Errors:                 0
            Checks:              1234
            Transferred:          456
            Elapsed time:       1m23s
        """
        import re

        stats: dict[str, Any] = {}

        # Handle both \n and \r as line separators (--stats-one-line uses \r)
        output = output.replace("\r", "\n")
        lines = output.strip().split("\n")

        for line in lines:
            # Parse bytes transferred line: "Transferred: X GiB / Y GiB, ..." or "X B / Y B"
            if "Transferred:" in line and "/" in line:
                # Try to extract bytes with unit (GiB, MiB, KiB, B)
                match = re.search(r'Transferred:\s*([\d.]+)\s*(\w+)\s*/\s*([\d.]+)\s*(\w+)', line)
                if match:
                    done_val, done_unit, total_val, total_unit = match.groups()
                    # Only update if this looks like bytes (has a size unit)
                    if any(u in done_unit.lower() for u in ['b', 'gib', 'mib', 'kib', 'byte']):
                        stats["bytes_transferred"] = self._parse_size(done_val, done_unit)
                        stats["total_bytes"] = self._parse_size(total_val, total_unit)
                    else:
                        # No unit = file count (e.g., "3 / 3")
                        try:
                            stats["files_transferred"] = int(float(done_val))
                            stats["total_files"] = int(float(total_val))
                        except ValueError:
                            pass
                else:
                    # Try simpler pattern for file count: "Transferred: 3 / 3, 100%"
                    match = re.search(r'Transferred:\s*(\d+)\s*/\s*(\d+)', line)
                    if match and "files_transferred" not in stats:
                        stats["files_transferred"] = int(match.group(1))
                        stats["total_files"] = int(match.group(2))

            # Parse standalone "Transferred: 456" (file count without total)
            elif "Transferred:" in line and "/" not in line:
                match = re.search(r'Transferred:\s*(\d+)\s*$', line)
                if match and "files_transferred" not in stats:
                    stats["files_transferred"] = int(match.group(1))

            # Parse errors count
            if "Errors:" in line:
                match = re.search(r'Errors:\s*(\d+)', line)
                if match:
                    stats["error_count"] = int(match.group(1))

            # Parse checks (verified files)
            if "Checks:" in line:
                match = re.search(r'Checks:\s*(\d+)', line)
                if match:
                    stats["checks"] = int(match.group(1))

        return stats

    def _parse_size(self, value: str, unit: str) -> int:
        """Parse a size value with unit to bytes.

        Args:
            value: Numeric value as string (e.g., "1.234")
            unit: Unit string (e.g., "GiB", "MiB", "KiB", "Bytes")

        Returns:
            Size in bytes
        """
        try:
            num = float(value)
            unit_lower = unit.lower()
            if "gib" in unit_lower or "gb" in unit_lower:
                return int(num * 1024 * 1024 * 1024)
            elif "mib" in unit_lower or "mb" in unit_lower:
                return int(num * 1024 * 1024)
            elif "kib" in unit_lower or "kb" in unit_lower:
                return int(num * 1024)
            else:
                return int(num)
        except (ValueError, TypeError):
            return 0

    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Run rclone sync backup."""
        started_at = datetime.now()

        try:
            cmd = self._build_command(
                operation="sync",
                source=str(source.path),
                destination=destination.path,
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

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.get("timeout", 86400),
            )

            finished_at = datetime.now()
            stats = self._parse_output(result.stdout + result.stderr)

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip() and "ERROR" in line]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.BACKUP,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("bytes_transferred", 0),
                files_transferred=stats.get("files_transferred", 0),
                errors=errors,
                output=result.stdout + result.stderr,
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
                errors=[f"Failed to execute rclone: {e}"],
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
        """Restore from rclone backup."""
        started_at = datetime.now()

        if snapshot not in (None, "", "current"):
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=["rclone restores current state only; historical snapshots are not available"],
                return_code=-1,
            )

        try:
            cmd = self._build_command(
                operation="sync",
                source=source.path,
                destination=str(destination),
                dry_run=dry_run,
            )
        except RuntimeError as e:
            return BackendResult(
                success=False,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=datetime.now(),
                errors=[str(e)],
                return_code=-1,
            )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.get("timeout", 86400),
            )

            finished_at = datetime.now()
            stats = self._parse_output(result.stdout + result.stderr)

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip() and "ERROR" in line]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.RESTORE,
                started_at=started_at,
                finished_at=finished_at,
                bytes_transferred=stats.get("bytes_transferred", 0),
                files_transferred=stats.get("files_transferred", 0),
                errors=errors,
                output=result.stdout + result.stderr,
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
                errors=[f"Failed to execute rclone: {e}"],
                return_code=-1,
            )

    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List contents at destination."""
        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "lsjson", destination.path, "--dirs-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0 and result.stdout.strip():
                items = json.loads(result.stdout)
                return [
                    {
                        "id": item.get("Name", "unknown"),
                        "path": f"{destination.path}/{item.get('Name', '')}",
                        "timestamp": item.get("ModTime", ""),
                        "size": item.get("Size", 0),
                    }
                    for item in items
                ]
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError) as e:
            logger.warning(f"[RCLONE] Failed to list snapshots: {e}")

        return [{"id": "current", "path": destination.path, "note": "Current backup state"}]

    def check(self, destination: BackupDestination) -> BackendResult:
        """Check integrity of rclone destination.

        Uses 'rclone size' to verify the destination is accessible and get stats.
        """
        started_at = datetime.now()

        try:
            binary = self._get_binary()
            # Use 'size' command to verify destination is accessible
            result = subprocess.run(
                [str(binary), "size", destination.path, "--json"],
                capture_output=True,
                text=True,
                timeout=self.config.get("timeout", 3600),
            )

            errors = []
            if result.returncode != 0:
                errors = [line for line in result.stderr.split("\n") if line.strip() and "ERROR" in line]

            return BackendResult(
                success=result.returncode == 0,
                operation=OperationType.CHECK,
                started_at=started_at,
                finished_at=datetime.now(),
                output=result.stdout + result.stderr,
                errors=errors,
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

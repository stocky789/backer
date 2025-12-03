"""Rclone backend implementation."""

import json
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


@BackendRegistry.register(BackendType.RCLONE)
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
            version_line = result.stdout.split("\n")[0]
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
        except (subprocess.TimeoutExpired, OSError, RuntimeError):
            pass
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
            "--progress",
            "--stats", "1s",
            "--stats-one-line",
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
        """Parse rclone output for stats."""
        stats: dict[str, Any] = {}

        lines = output.strip().split("\n")
        for line in lines:
            if "Transferred:" in line and "Errors:" not in line:
                parts = line.split(",")
                if len(parts) >= 1:
                    stats["transfer_summary"] = parts[0].replace("Transferred:", "").strip()
            if "Errors:" in line:
                try:
                    error_count = int(line.split(":")[1].strip().split()[0])
                    stats["error_count"] = error_count
                except (ValueError, IndexError):
                    pass

        return stats

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
    ) -> BackendResult:
        """Restore from rclone backup."""
        started_at = datetime.now()

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
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError):
            pass

        return [{"id": "current", "path": destination.path, "note": "Current backup state"}]

    def check(self, destination: BackupDestination) -> BackendResult:
        """Check integrity of rclone destination."""
        started_at = datetime.now()

        try:
            binary = self._get_binary()
            result = subprocess.run(
                [str(binary), "check", destination.path, "--one-way"],
                capture_output=True,
                text=True,
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

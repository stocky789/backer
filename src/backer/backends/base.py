"""Abstract base class for all backup backends."""

import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BackendType(str, Enum):
    """Supported backend types."""

    RSYNC = "rsync"
    RCLONE = "rclone"
    RESTIC = "restic"
    BORG = "borg"
    KOPIA = "kopia"
    PROXY = "proxy"  # Proxy to remote server (for local directory storage)


class OperationType(str, Enum):
    """Type of backup operation."""

    BACKUP = "backup"
    RESTORE = "restore"
    LIST = "list"
    PRUNE = "prune"
    CHECK = "check"


@dataclass
class BackendResult:
    """Result from a backend operation."""

    success: bool
    operation: OperationType
    started_at: datetime
    finished_at: datetime
    bytes_transferred: int = 0
    files_transferred: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output: str = ""
    return_code: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Get operation duration in seconds."""
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class BackupSource:
    """Source definition for a backup."""

    path: Path
    excludes: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)


@dataclass
class BackupDestination:
    """Destination definition for a backup."""

    path: str  # Can be local path or remote URI (rclone remote:path, etc.)
    backend_type: BackendType = BackendType.RCLONE  # Default to rclone (rsync not supported for agents)


class BackendBase(ABC):
    """Abstract base class for backup backends.

    Each backend wraps a specific backup tool (rsync, rclone, restic, etc.)
    and provides a unified interface for backup operations.
    """

    backend_type: BackendType

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize the backend with optional configuration."""
        self.config = config or {}

    def run_command(
        self,
        cmd: list[str],
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a subprocess command with proper error logging and stderr handling.

        Args:
            cmd: Command and arguments as list
            timeout: Timeout in seconds
            env: Environment variables dict
            check: If True, raise CalledProcessError on non-zero return code

        Returns:
            CompletedProcess with stdout, stderr, and returncode

        Raises:
            subprocess.CalledProcessError: If check=True and returncode != 0
            subprocess.TimeoutExpired: If command times out
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                check=False,  # We handle return codes ourselves
            )

            # Log stderr if present
            if result.stderr:
                backend_name = getattr(self, 'backend_type', 'unknown')
                if result.returncode != 0:
                    logger.error(
                        f"[{backend_name}] Command failed with return code {result.returncode}:\n"
                        f"Command: {' '.join(cmd)}\n"
                        f"stderr: {result.stderr}"
                    )
                else:
                    logger.debug(
                        f"[{backend_name}] Command warnings (stderr):\n"
                        f"stderr: {result.stderr}"
                    )

            # Check for errors if requested
            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    cmd,
                    output=result.stdout,
                    stderr=result.stderr,
                )

            return result
        except subprocess.TimeoutExpired:
            backend_name = getattr(self, 'backend_type', 'unknown')
            logger.error(
                f"[{backend_name}] Command timeout after {timeout}s: {' '.join(cmd)}"
            )
            raise
        except Exception as e:
            backend_name = getattr(self, 'backend_type', 'unknown')
            logger.error(f"[{backend_name}] Error executing command: {e}")
            raise

    @abstractmethod
    def check_available(self) -> tuple[bool, str]:
        """Check if the backend tool is available on the system.

        Returns:
            Tuple of (is_available, version_or_error_message)
        """
        pass

    @abstractmethod
    def backup(
        self,
        source: BackupSource,
        destination: BackupDestination,
        dry_run: bool = False,
        progress_callback: Any | None = None,
    ) -> BackendResult:
        """Run a backup operation.

        Args:
            source: The source to backup
            destination: Where to store the backup
            dry_run: If True, simulate the backup without making changes
            progress_callback: Optional callback for progress updates

        Returns:
            BackendResult with operation details
        """
        pass

    @abstractmethod
    def restore(
        self,
        source: BackupDestination,
        destination: Path,
        snapshot: str | None = None,
        dry_run: bool = False,
        progress_callback: Any | None = None,
        original_source_path: str | None = None,
    ) -> BackendResult:
        """Restore from a backup.

        Args:
            source: The backup location to restore from
            destination: Where to restore files to
            snapshot: Specific snapshot/version to restore (if supported)
            dry_run: If True, simulate the restore without making changes
            progress_callback: Optional callback for progress updates
            original_source_path: The original path that was backed up (for kopia/restic snapshot lookup)

        Returns:
            BackendResult with operation details
        """
        pass

    @abstractmethod
    def list_snapshots(self, destination: BackupDestination) -> list[dict[str, Any]]:
        """List available snapshots/versions at the destination.

        Args:
            destination: The backup location to list

        Returns:
            List of snapshot information dicts
        """
        pass

    def prune(
        self,
        destination: BackupDestination,
        keep_last: int | None = None,
        keep_daily: int | None = None,
        keep_weekly: int | None = None,
        keep_monthly: int | None = None,
        dry_run: bool = False,
    ) -> BackendResult:
        """Prune old snapshots according to retention policy.

        Not all backends support this - base implementation raises NotImplementedError.
        """
        raise NotImplementedError(f"{self.backend_type} does not support pruning")

    def check(self, destination: BackupDestination) -> BackendResult:
        """Verify integrity of backups at destination.

        Not all backends support this - base implementation raises NotImplementedError.
        """
        raise NotImplementedError(f"{self.backend_type} does not support integrity checks")

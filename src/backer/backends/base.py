"""Abstract base class for all backup backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class BackendType(str, Enum):
    """Supported backend types."""

    RSYNC = "rsync"
    RCLONE = "rclone"
    RESTIC = "restic"
    BORG = "borg"


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
    backend_type: BackendType = BackendType.RSYNC


class BackendBase(ABC):
    """Abstract base class for backup backends.

    Each backend wraps a specific backup tool (rsync, rclone, restic, etc.)
    and provides a unified interface for backup operations.
    """

    backend_type: BackendType

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize the backend with optional configuration."""
        self.config = config or {}

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
    ) -> BackendResult:
        """Restore from a backup.

        Args:
            source: The backup location to restore from
            destination: Where to restore files to
            snapshot: Specific snapshot/version to restore (if supported)
            dry_run: If True, simulate the restore without making changes
            progress_callback: Optional callback for progress updates

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

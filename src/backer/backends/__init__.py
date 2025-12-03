"""Backup backends - wrappers around rsync, rclone, restic, kopia, etc."""

from backer.backends.base import BackendBase, BackendResult
from backer.backends.registry import BackendRegistry, get_backend

# Import backend modules to register them
from backer.backends import rclone, restic, rsync, kopia  # noqa: F401

__all__ = ["BackendBase", "BackendResult", "BackendRegistry", "get_backend", "rclone", "restic", "rsync", "kopia"]

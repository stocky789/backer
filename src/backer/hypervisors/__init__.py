"""Hypervisor integrations for backing up VMs and containers."""

from backer.hypervisors.incremental import (
    BackupDecision,
    BackupType,
    IncrementalBackupManager,
)
from backer.hypervisors.metadata import HypervisorMetadata
from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxBackupManager
from backer.hypervisors.qmp import DirtyBitmap, QMPClient, QMPError
from backer.hypervisors.unraid import (
    UnraidAPI,
    UnraidAPIError,
    UnraidBackupManager,
    UnraidGuestType,
)

__all__ = [
    "ProxmoxAPI",
    "ProxmoxBackupManager",
    "UnraidAPI",
    "UnraidAPIError",
    "UnraidBackupManager",
    "UnraidGuestType",
    "QMPClient",
    "QMPError",
    "DirtyBitmap",
    "IncrementalBackupManager",
    "BackupType",
    "BackupDecision",
    "HypervisorMetadata",
]

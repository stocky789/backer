"""Hypervisor integrations for backing up VMs and containers."""

from backer.hypervisors.incremental import (
    BackupDecision,
    BackupType,
    IncrementalBackupManager,
)
from backer.hypervisors.proxmox import ProxmoxAPI, ProxmoxBackupManager
from backer.hypervisors.qmp import DirtyBitmap, QMPClient, QMPError

__all__ = [
    "ProxmoxAPI",
    "ProxmoxBackupManager",
    "QMPClient",
    "QMPError",
    "DirtyBitmap",
    "IncrementalBackupManager",
    "BackupType",
    "BackupDecision",
]

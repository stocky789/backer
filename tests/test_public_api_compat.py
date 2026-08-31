"""Compatibility imports retained for patch releases."""

import backer.core as core
import backer.core.config as config
import backer.core.job as job
import backer.hypervisors as hypervisors
from backer.hypervisors import hyperv, incremental, metadata, proxmox, qmp, unraid


def test_core_public_imports_are_canonical():
    assert core.BackerConfig is config.BackerConfig
    assert core.load_config is config.load_config
    assert core.JobStatus is job.JobStatus


def test_hypervisor_public_imports_are_canonical():
    assert hypervisors.ProxmoxAPI is proxmox.ProxmoxAPI
    assert hypervisors.ProxmoxBackupManager is proxmox.ProxmoxBackupManager
    assert hypervisors.HyperVAPI is hyperv.HyperVAPI
    assert hypervisors.HyperVAPIError is hyperv.HyperVAPIError
    assert hypervisors.HyperVBackupManager is hyperv.HyperVBackupManager
    assert hypervisors.HyperVGuestType is hyperv.HyperVGuestType
    assert hypervisors.UnraidAPI is unraid.UnraidAPI
    assert hypervisors.UnraidAPIError is unraid.UnraidAPIError
    assert hypervisors.UnraidBackupManager is unraid.UnraidBackupManager
    assert hypervisors.UnraidGuestType is unraid.UnraidGuestType
    assert hypervisors.QMPClient is qmp.QMPClient
    assert hypervisors.QMPError is qmp.QMPError
    assert hypervisors.DirtyBitmap is qmp.DirtyBitmap
    assert hypervisors.IncrementalBackupManager is incremental.IncrementalBackupManager
    assert hypervisors.BackupType is incremental.BackupType
    assert hypervisors.BackupDecision is incremental.BackupDecision
    assert hypervisors.HypervisorMetadata is metadata.HypervisorMetadata

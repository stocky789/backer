"""Incremental backup support for Proxmox VMs using QEMU dirty bitmaps.

This module provides incremental backup functionality for Proxmox VMs by
leveraging QEMU's dirty bitmap feature to track changed blocks between backups.

How it works:
1. On first backup (or when bitmap is invalid), we do a full vzdump backup
   and create a dirty bitmap to track future changes.

2. On subsequent backups, we check if the dirty bitmap indicates changes.
   If there are dirty blocks, we do a vzdump backup and clear the bitmap.
   If there are no changes (dirty_bytes == 0), we can skip the backup entirely.

3. Bitmaps are persistent for qcow2 disks (survive VM reboots).
   For RAW disks, bitmaps are lost on VM power off, so we fall back to full.

Key limitations:
- Requires SSH access to Proxmox host for QMP socket access
- Only qcow2 disks support persistent bitmaps
- RAW/VMDK disks lose bitmap on VM power off (fall back to full backup)
- vzdump still creates full backup files, but we can skip unchanged VMs

This approach is similar to how Veeam implements incremental backups for Proxmox,
using QEMU's native dirty bitmap feature for change detection.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from backer.hypervisors.qmp import QMPClient, QMPError

if TYPE_CHECKING:
    from backer.server.storage import Storage

logger = logging.getLogger(__name__)


class BackupType(str, Enum):
    """Type of backup to perform."""

    FULL = "full"  # Full backup - always performed
    INCREMENTAL = "incremental"  # Incremental - only if changes detected
    SKIP = "skip"  # Skip - no changes since last backup


@dataclass
class BackupDecision:
    """Decision about what type of backup to perform for a VM."""

    vmid: int
    backup_type: BackupType
    reason: str
    dirty_bytes: int = 0  # Approximate bytes changed since last backup
    disk_count: int = 0
    all_disks_persistent: bool = True  # All disks support persistent bitmaps
    disks: list[dict[str, Any]] | None = None


class IncrementalBackupManager:
    """Manages incremental backups for Proxmox VMs.

    This manager handles the decision-making about whether to perform
    full or incremental backups based on dirty bitmap state.

    Example:
        manager = IncrementalBackupManager(
            host="192.168.1.100",
            hypervisor_id="hv-123",
            storage=storage_instance,
            ssh_user="root",
        )

        # Check what backup type is needed for a VM
        decision = manager.get_backup_decision(vmid=100)
        if decision.backup_type == BackupType.SKIP:
            print("No changes - skipping backup")
        else:
            # Perform backup...
            manager.after_backup_success(vmid=100, decision=decision)
    """

    def __init__(
        self,
        host: str,
        hypervisor_id: str,
        storage: "Storage",
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_key: str | None = None,
        ssh_password: str | None = None,
        max_incrementals: int = 7,  # Force full after this many incrementals
    ):
        """Initialize the incremental backup manager.

        Args:
            host: Proxmox host hostname or IP
            hypervisor_id: Backer hypervisor ID for state tracking
            storage: Storage instance for bitmap state persistence
            ssh_user: SSH username for QMP access
            ssh_port: SSH port
            ssh_key: Path to SSH private key
            ssh_password: SSH password
            max_incrementals: Force full backup after this many incrementals
        """
        self.host = host
        self.hypervisor_id = hypervisor_id
        self.storage = storage
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.ssh_key = ssh_key
        self.ssh_password = ssh_password
        self.max_incrementals = max_incrementals

    def _get_qmp_client(self, vmid: int) -> QMPClient:
        """Create a QMP client for a specific VM."""
        return QMPClient(
            host=self.host,
            vmid=vmid,
            ssh_user=self.ssh_user,
            ssh_port=self.ssh_port,
            ssh_key=self.ssh_key,
            ssh_password=self.ssh_password,
        )

    def setup_tracking(self, vmid: int) -> dict[str, Any]:
        """Set up dirty bitmap tracking for a VM.

        Creates bitmaps on all disks if they don't exist.
        Should be called after the first full backup.

        Args:
            vmid: VM ID to set up tracking for

        Returns:
            Setup result dict
        """
        try:
            client = self._get_qmp_client(vmid)

            if not client.is_vm_running():
                return {
                    "vmid": vmid,
                    "success": False,
                    "error": "VM is not running",
                }

            # Set up bitmaps on all disks
            result = client.setup_incremental_tracking()

            # Save state to database
            for disk in result.get("disks", []):
                if disk.get("status") in ("created", "exists", "recreated"):
                    self.storage.save_vm_bitmap_state(
                        hypervisor_id=self.hypervisor_id,
                        vmid=vmid,
                        disk_node=disk["node"],
                        disk_driver=disk["driver"],
                        bitmap_name=disk.get("bitmap", ""),
                        bitmap_valid=True,
                    )

            return {
                "vmid": vmid,
                "success": True,
                "disks": result.get("disks", []),
                "all_persistent": result.get("all_persistent", False),
            }

        except QMPError as e:
            logger.warning(f"Failed to set up tracking for VM {vmid}: {e}")
            return {
                "vmid": vmid,
                "success": False,
                "error": str(e),
            }

    def get_backup_decision(
        self,
        vmid: int,
        force_full: bool = False,
    ) -> BackupDecision:
        """Determine what type of backup to perform for a VM.

        This checks:
        1. If VM is running and QMP accessible
        2. If bitmaps exist and are valid
        3. If there are dirty blocks indicating changes
        4. If we've exceeded max incrementals since last full

        Args:
            vmid: VM ID
            force_full: Force a full backup regardless of bitmap state

        Returns:
            BackupDecision indicating what backup type to use
        """
        if force_full:
            return BackupDecision(
                vmid=vmid,
                backup_type=BackupType.FULL,
                reason="Full backup requested",
            )

        # Check stored bitmap state
        db_states = self.storage.get_vm_bitmap_state(self.hypervisor_id, vmid)

        if not db_states:
            return BackupDecision(
                vmid=vmid,
                backup_type=BackupType.FULL,
                reason="First backup - no bitmap tracking yet",
            )

        # Check if any disk has exceeded max incrementals
        for state in db_states:
            if state["backup_count"] >= self.max_incrementals:
                return BackupDecision(
                    vmid=vmid,
                    backup_type=BackupType.FULL,
                    reason=f"Exceeded {self.max_incrementals} incrementals since last full",
                )

        # Check if all bitmaps are valid in our database
        invalid_states = [s for s in db_states if not s["bitmap_valid"]]
        if invalid_states:
            # Some bitmaps are marked invalid - need full backup
            nodes = [s["disk_node"] for s in invalid_states]
            return BackupDecision(
                vmid=vmid,
                backup_type=BackupType.FULL,
                reason=f"Bitmap invalid for disks: {', '.join(nodes)}",
            )

        # Check if there's a full backup already
        has_full = any(s["last_full_backup"] for s in db_states)
        if not has_full:
            return BackupDecision(
                vmid=vmid,
                backup_type=BackupType.FULL,
                reason="No full backup exists yet",
            )

        # Now check live bitmap state via QMP
        try:
            client = self._get_qmp_client(vmid)

            if not client.is_vm_running():
                # VM not running - check if RAW disks (bitmaps would be lost)
                raw_disks = [s for s in db_states if s["disk_driver"] == "raw"]
                if raw_disks:
                    # Invalidate RAW disk bitmaps in DB
                    for s in raw_disks:
                        self.storage.invalidate_vm_bitmaps(
                            self.hypervisor_id,
                            vmid,
                            s["disk_node"],
                        )
                    return BackupDecision(
                        vmid=vmid,
                        backup_type=BackupType.FULL,
                        reason="VM not running - RAW disk bitmaps lost",
                    )
                # qcow2 bitmaps persist, we can still do incremental
                # but we can't check dirty bytes without VM running
                return BackupDecision(
                    vmid=vmid,
                    backup_type=BackupType.INCREMENTAL,
                    reason="VM not running - using persisted qcow2 bitmap state",
                )

            # VM is running - get live disk info
            disk_info = client.get_disk_info()

            if not disk_info:
                return BackupDecision(
                    vmid=vmid,
                    backup_type=BackupType.FULL,
                    reason="No disks found for VM",
                )

            # Check each disk
            total_dirty = 0
            all_persistent = True
            missing_bitmaps = []
            invalid_bitmaps = []

            for disk in disk_info:
                if not disk["supports_persistent_bitmaps"]:
                    all_persistent = False

                if not disk["has_backer_bitmap"]:
                    missing_bitmaps.append(disk["node"])
                elif not disk["bitmap_usable"]:
                    invalid_bitmaps.append(disk["node"])
                else:
                    total_dirty += disk["dirty_bytes"]

            if missing_bitmaps:
                return BackupDecision(
                    vmid=vmid,
                    backup_type=BackupType.FULL,
                    reason=f"Missing bitmaps on: {', '.join(missing_bitmaps)}",
                    disks=disk_info,
                )

            if invalid_bitmaps:
                return BackupDecision(
                    vmid=vmid,
                    backup_type=BackupType.FULL,
                    reason=f"Invalid bitmaps on: {', '.join(invalid_bitmaps)}",
                    disks=disk_info,
                )

            # Check if there are any changes
            if total_dirty == 0:
                return BackupDecision(
                    vmid=vmid,
                    backup_type=BackupType.SKIP,
                    reason="No changes since last backup",
                    dirty_bytes=0,
                    disk_count=len(disk_info),
                    all_disks_persistent=all_persistent,
                    disks=disk_info,
                )

            # There are changes - do incremental
            return BackupDecision(
                vmid=vmid,
                backup_type=BackupType.INCREMENTAL,
                reason=f"Changes detected: ~{total_dirty / (1024*1024):.1f} MB",
                dirty_bytes=total_dirty,
                disk_count=len(disk_info),
                all_disks_persistent=all_persistent,
                disks=disk_info,
            )

        except QMPError as e:
            logger.warning(f"QMP error checking VM {vmid}: {e}")
            # Fall back to full backup if we can't check bitmaps
            return BackupDecision(
                vmid=vmid,
                backup_type=BackupType.FULL,
                reason=f"Cannot check bitmap state: {e}",
            )

    def after_backup_success(
        self,
        vmid: int,
        decision: BackupDecision,
    ) -> None:
        """Update state after a successful backup.

        This clears the dirty bitmaps and updates the database state.

        Args:
            vmid: VM ID
            decision: The backup decision that was used
        """
        timestamp = datetime.now().isoformat()
        backup_type = "full" if decision.backup_type == BackupType.FULL else "incremental"

        # Update database state for existing disks
        db_states = self.storage.get_vm_bitmap_state(self.hypervisor_id, vmid)

        if db_states:
            for state in db_states:
                self.storage.update_bitmap_after_backup(
                    hypervisor_id=self.hypervisor_id,
                    vmid=vmid,
                    disk_node=state["disk_node"],
                    backup_type=backup_type,
                    timestamp=timestamp,
                )
        elif decision.disks:
            # First backup - create initial state from decision disks
            for disk in decision.disks:
                if disk.get("has_backer_bitmap") or disk.get("bitmap_name"):
                    self.storage.save_vm_bitmap_state(
                        hypervisor_id=self.hypervisor_id,
                        vmid=vmid,
                        disk_node=disk["node"],
                        disk_driver=disk.get("driver", "unknown"),
                        bitmap_name=disk.get("bitmap_name", ""),
                        bitmap_valid=True,
                    )
                    # Mark as having a full backup
                    self.storage.update_bitmap_after_backup(
                        hypervisor_id=self.hypervisor_id,
                        vmid=vmid,
                        disk_node=disk["node"],
                        backup_type="full",
                        timestamp=timestamp,
                    )

        # Clear bitmaps via QMP if VM is running
        try:
            client = self._get_qmp_client(vmid)

            if client.is_vm_running():
                disk_info = client.get_disk_info()
                for disk in disk_info:
                    if disk["has_backer_bitmap"] and disk["bitmap_name"]:
                        try:
                            client.clear_bitmap(disk["node"], disk["bitmap_name"])
                        except QMPError as e:
                            logger.warning(
                                f"Failed to clear bitmap on {disk['node']}: {e}"
                            )

        except QMPError as e:
            logger.warning(f"Could not clear bitmaps for VM {vmid}: {e}")
            # This is okay - bitmaps will just have more "dirty" data next time

        logger.info(
            f"Updated bitmap state for VM {vmid} after {backup_type} backup"
        )

    def after_backup_failure(
        self,
        vmid: int,
        decision: BackupDecision,
    ) -> None:
        """Handle state after a failed backup.

        We don't clear bitmaps on failure - they retain dirty bits
        for retry on next backup attempt.

        Args:
            vmid: VM ID
            decision: The backup decision that was used
        """
        # Don't clear bitmaps - they'll include the changes for retry
        logger.info(
            f"Backup failed for VM {vmid} - bitmaps retained for retry"
        )

    def check_bitmap_validity(self, vmid: int) -> dict[str, Any]:
        """Check if bitmaps are still valid for a VM.

        This should be called before backup to detect if bitmaps
        were lost (e.g., after VM reboot with RAW disks).

        Args:
            vmid: VM ID

        Returns:
            Dict with validity info per disk
        """
        db_states = self.storage.get_vm_bitmap_state(self.hypervisor_id, vmid)

        if not db_states:
            return {
                "vmid": vmid,
                "valid": False,
                "reason": "No bitmap state tracked",
            }

        try:
            client = self._get_qmp_client(vmid)

            if not client.is_vm_running():
                # Check for non-persistent disks that would have lost bitmaps
                non_persistent = [
                    s for s in db_states
                    if s["disk_driver"] != "qcow2"
                ]
                if non_persistent:
                    # Invalidate these in DB
                    for s in non_persistent:
                        self.storage.invalidate_vm_bitmaps(
                            self.hypervisor_id,
                            vmid,
                            s["disk_node"],
                        )
                    return {
                        "vmid": vmid,
                        "valid": False,
                        "reason": "VM not running - non-qcow2 bitmaps invalidated",
                        "invalidated": [s["disk_node"] for s in non_persistent],
                    }
                return {
                    "vmid": vmid,
                    "valid": True,
                    "reason": "All disks are qcow2 - bitmaps persisted",
                }

            # VM running - check actual bitmap state
            disk_info = client.get_disk_info()
            issues = []

            for disk in disk_info:
                if disk["has_backer_bitmap"]:
                    if not disk["bitmap_usable"]:
                        issues.append(f"{disk['node']}: bitmap not usable")
                else:
                    # Bitmap is missing - could have been lost
                    db_state = next(
                        (s for s in db_states if s["disk_node"] == disk["node"]),
                        None,
                    )
                    if db_state and db_state["bitmap_valid"]:
                        # We thought it was valid but it's gone
                        self.storage.invalidate_vm_bitmaps(
                            self.hypervisor_id,
                            vmid,
                            disk["node"],
                        )
                        issues.append(f"{disk['node']}: bitmap missing")

            if issues:
                return {
                    "vmid": vmid,
                    "valid": False,
                    "reason": "Bitmap issues detected",
                    "issues": issues,
                }

            return {
                "vmid": vmid,
                "valid": True,
                "reason": "All bitmaps valid",
                "disks": [d["node"] for d in disk_info if d["has_backer_bitmap"]],
            }

        except QMPError as e:
            return {
                "vmid": vmid,
                "valid": False,
                "reason": f"Could not check bitmaps: {e}",
            }

    def get_vm_backup_stats(self, vmid: int) -> dict[str, Any]:
        """Get backup statistics for a VM.

        Args:
            vmid: VM ID

        Returns:
            Stats dict with backup history info
        """
        db_states = self.storage.get_vm_bitmap_state(self.hypervisor_id, vmid)

        if not db_states:
            return {
                "vmid": vmid,
                "tracked": False,
            }

        total_incrementals = sum(s["backup_count"] for s in db_states)
        last_full = max(
            (s["last_full_backup"] for s in db_states if s["last_full_backup"]),
            default=None,
        )
        last_inc = max(
            (s["last_incremental_backup"] for s in db_states if s["last_incremental_backup"]),
            default=None,
        )

        # Get live dirty bytes if VM is running
        dirty_bytes = 0
        try:
            client = self._get_qmp_client(vmid)
            if client.is_vm_running():
                disk_info = client.get_disk_info()
                dirty_bytes = sum(d["dirty_bytes"] for d in disk_info)
        except QMPError:
            pass

        return {
            "vmid": vmid,
            "tracked": True,
            "disk_count": len(db_states),
            "all_valid": all(s["bitmap_valid"] for s in db_states),
            "all_qcow2": all(s["disk_driver"] == "qcow2" for s in db_states),
            "total_incrementals_since_full": total_incrementals,
            "last_full_backup": last_full,
            "last_incremental_backup": last_inc,
            "pending_dirty_bytes": dirty_bytes,
            "disks": [
                {
                    "node": s["disk_node"],
                    "driver": s["disk_driver"],
                    "valid": s["bitmap_valid"],
                    "incrementals": s["backup_count"],
                }
                for s in db_states
            ],
        }

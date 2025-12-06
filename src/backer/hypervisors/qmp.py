"""QEMU Machine Protocol (QMP) client for dirty bitmap operations.

This module provides a client for communicating with QEMU VMs via the QMP
protocol over SSH. It's used for implementing incremental backups using
QEMU's dirty bitmap feature.

QMP sockets are located at /var/run/qemu-server/<VMID>.qmp on Proxmox hosts.
"""

import base64
import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class QMPError(Exception):
    """Exception raised for QMP protocol errors."""

    def __init__(self, message: str, error_class: str = "", error_desc: str = ""):
        super().__init__(message)
        self.error_class = error_class
        self.error_desc = error_desc


@dataclass
class DirtyBitmap:
    """Represents a QEMU dirty bitmap."""

    name: str
    node: str  # Block device node name (e.g., "drive-scsi0")
    granularity: int = 65536  # 64KB default
    count: int = 0  # Dirty bytes count
    recording: bool = True
    busy: bool = False
    persistent: bool = False
    inconsistent: bool = False

    @property
    def is_usable(self) -> bool:
        """Check if bitmap is usable for incremental backup."""
        return not self.inconsistent and not self.busy


@dataclass
class BlockDevice:
    """Represents a QEMU block device with its bitmaps."""

    node_name: str  # Internal node name for QMP commands
    device: str  # Device name (e.g., "scsi0")
    driver: str  # Driver type (qcow2, raw, etc.)
    file: str  # Backing file path
    bitmaps: list[DirtyBitmap] = field(default_factory=list)
    inserted: bool = True

    @property
    def supports_persistent_bitmaps(self) -> bool:
        """Check if this device supports persistent bitmaps."""
        return self.driver == "qcow2"


class QMPClient:
    """Client for QEMU Machine Protocol operations via SSH.

    This client connects to the QMP socket on a Proxmox host via SSH
    to manage dirty bitmaps for incremental backups.

    Example:
        client = QMPClient(
            host="192.168.1.100",
            vmid=100,
            ssh_user="root",
        )
        # Check if bitmap exists
        bitmaps = client.list_bitmaps()

        # Create bitmap for incremental tracking
        client.create_bitmap("drive-scsi0", "backer-inc")

        # After backup, clear the bitmap
        client.clear_bitmap("drive-scsi0", "backer-inc")
    """

    BITMAP_PREFIX = "backer-"  # Prefix for all Backer-managed bitmaps
    DEFAULT_GRANULARITY = 65536  # 64KB - good balance of precision vs overhead

    def __init__(
        self,
        host: str,
        vmid: int,
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_key: str | None = None,
        ssh_password: str | None = None,
        timeout: int = 30,
    ):
        """Initialize QMP client.

        Args:
            host: Proxmox host hostname or IP
            vmid: VM ID to connect to
            ssh_user: SSH username (default: root)
            ssh_port: SSH port (default: 22)
            ssh_key: Path to SSH private key (optional)
            ssh_password: SSH password (optional, key preferred)
            timeout: Command timeout in seconds
        """
        self.host = host
        self.vmid = vmid
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.ssh_key = ssh_key
        self.ssh_password = ssh_password
        self.timeout = timeout

        # QMP socket path on Proxmox
        self.qmp_socket = f"/var/run/qemu-server/{vmid}.qmp"

    def _build_ssh_command(self, remote_cmd: str) -> list[str]:
        """Build SSH command with options.

        Args:
            remote_cmd: Command to run on remote host

        Returns:
            Complete SSH command as list
        """
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={self.timeout}",
            "-p", str(self.ssh_port),
        ]

        if self.ssh_key:
            ssh_cmd.extend(["-i", self.ssh_key])
            ssh_cmd.extend(["-o", "BatchMode=yes"])
        elif not self.ssh_password:
            # No key and no password - use BatchMode to fail fast
            ssh_cmd.extend(["-o", "BatchMode=yes"])

        ssh_cmd.append(f"{self.ssh_user}@{self.host}")
        ssh_cmd.append(remote_cmd)

        # If using password, wrap with sshpass
        if self.ssh_password and not self.ssh_key:
            cmd = [
                "sshpass", "-p", self.ssh_password,
            ] + ssh_cmd
            return cmd

        return ssh_cmd

    def _execute_qmp(self, command: dict[str, Any]) -> dict[str, Any]:
        """Execute a QMP command via SSH and socat.

        Args:
            command: QMP command dict to execute

        Returns:
            QMP response dict

        Raises:
            QMPError: If command fails or returns error
        """
        # Build the QMP command sequence
        # First send qmp_capabilities to negotiate, then the actual command
        qmp_commands = [
            json.dumps({"execute": "qmp_capabilities"}),
            json.dumps(command),
        ]

        # Join with actual newlines for the QMP protocol
        qmp_input = "\n".join(qmp_commands) + "\n"

        # Use base64 encoding to safely pass JSON through shell
        # This avoids any shell escaping issues with special characters
        qmp_b64 = base64.b64encode(qmp_input.encode()).decode()

        # Build the remote command using base64 decode and socat
        remote_cmd = (
            f"echo '{qmp_b64}' | base64 -d | "
            f"socat -t 2 - UNIX-CONNECT:{self.qmp_socket}"
        )

        ssh_cmd = self._build_ssh_command(remote_cmd)

        logger.debug(f"Executing QMP command on VM {self.vmid}: {command}")

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or "SSH command failed"
                if "Connection refused" in error_msg or "No such file" in error_msg:
                    raise QMPError(
                        f"VM {self.vmid} is not running or QMP socket unavailable",
                        error_class="VMNotRunning",
                    )
                raise QMPError(f"SSH error: {error_msg}")

            # Parse QMP responses (one per line)
            # Expected: greeting, capabilities response, command response
            responses = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        responses.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            if not responses:
                raise QMPError("No valid QMP response received")

            # Find the response to our command (should be last "return" or "error")
            for resp in reversed(responses):
                if "return" in resp:
                    return resp["return"] if resp["return"] is not None else {}
                if "error" in resp:
                    error = resp["error"]
                    raise QMPError(
                        error.get("desc", str(error)),
                        error_class=error.get("class", ""),
                        error_desc=error.get("desc", ""),
                    )

            # If we got here, check if last response is the greeting
            # (VM might not have processed command yet)
            raise QMPError("No command response in QMP output")

        except subprocess.TimeoutExpired:
            raise QMPError(f"QMP command timed out after {self.timeout}s")
        except subprocess.SubprocessError as e:
            raise QMPError(f"Failed to execute QMP command: {e}")

    def is_vm_running(self) -> bool:
        """Check if the VM is running and QMP socket is available.

        Returns:
            True if VM is running and QMP accessible
        """
        try:
            # Try to query VM status
            result = self._execute_qmp({"execute": "query-status"})
            return result.get("running", False) or result.get("status") == "running"
        except QMPError:
            return False

    def query_block(self) -> list[BlockDevice]:
        """Query all block devices and their bitmaps.

        Returns:
            List of BlockDevice objects with bitmap info
        """
        result = self._execute_qmp({"execute": "query-block"})

        devices = []
        for item in result if isinstance(result, list) else []:
            # Skip devices without inserted media
            if not item.get("inserted"):
                continue

            inserted = item.get("inserted", {})
            node_name = inserted.get("node-name", item.get("device", ""))
            device = item.get("device", "")

            # Parse bitmaps
            bitmaps = []
            for bm in inserted.get("dirty-bitmaps", []):
                bitmaps.append(DirtyBitmap(
                    name=bm.get("name", ""),
                    node=node_name,
                    granularity=bm.get("granularity", 65536),
                    count=bm.get("count", 0),
                    recording=bm.get("recording", True),
                    busy=bm.get("busy", False),
                    persistent=bm.get("persistent", False),
                    inconsistent=bm.get("inconsistent", False),
                ))

            devices.append(BlockDevice(
                node_name=node_name,
                device=device,
                driver=inserted.get("drv", ""),
                file=inserted.get("file", ""),
                bitmaps=bitmaps,
            ))

        return devices

    def list_bitmaps(self, node: str | None = None) -> list[DirtyBitmap]:
        """List all dirty bitmaps, optionally filtered by node.

        Args:
            node: Optional node name to filter by

        Returns:
            List of DirtyBitmap objects
        """
        devices = self.query_block()
        bitmaps = []

        for device in devices:
            if node and device.node_name != node:
                continue
            bitmaps.extend(device.bitmaps)

        return bitmaps

    def get_backer_bitmap(self, node: str) -> DirtyBitmap | None:
        """Get the Backer-managed bitmap for a device.

        Args:
            node: Block device node name

        Returns:
            DirtyBitmap if exists, None otherwise
        """
        bitmaps = self.list_bitmaps(node)
        for bitmap in bitmaps:
            if bitmap.name.startswith(self.BITMAP_PREFIX):
                return bitmap
        return None

    def create_bitmap(
        self,
        node: str,
        name: str | None = None,
        granularity: int | None = None,
        persistent: bool = True,
    ) -> str:
        """Create a new dirty bitmap on a block device.

        Args:
            node: Block device node name (e.g., "drive-scsi0")
            name: Bitmap name (auto-generated if not provided)
            granularity: Tracking granularity in bytes (default: 64KB)
            persistent: Whether to persist bitmap in qcow2 image

        Returns:
            Name of created bitmap

        Raises:
            QMPError: If bitmap creation fails
        """
        if not name:
            name = f"{self.BITMAP_PREFIX}inc"

        if not granularity:
            granularity = self.DEFAULT_GRANULARITY

        command = {
            "execute": "block-dirty-bitmap-add",
            "arguments": {
                "node": node,
                "name": name,
                "granularity": granularity,
                "persistent": persistent,
            },
        }

        try:
            self._execute_qmp(command)
            logger.info(f"Created bitmap '{name}' on {node} for VM {self.vmid}")
            return name
        except QMPError as e:
            # Check if bitmap already exists
            if "already exists" in str(e).lower():
                logger.debug(f"Bitmap '{name}' already exists on {node}")
                return name
            raise

    def clear_bitmap(self, node: str, name: str) -> None:
        """Clear all dirty bits from a bitmap.

        This should be called after a successful backup to reset tracking.

        Args:
            node: Block device node name
            name: Bitmap name to clear

        Raises:
            QMPError: If clear fails
        """
        command = {
            "execute": "block-dirty-bitmap-clear",
            "arguments": {
                "node": node,
                "name": name,
            },
        }

        self._execute_qmp(command)
        logger.info(f"Cleared bitmap '{name}' on {node} for VM {self.vmid}")

    def remove_bitmap(self, node: str, name: str) -> None:
        """Remove a dirty bitmap.

        Args:
            node: Block device node name
            name: Bitmap name to remove

        Raises:
            QMPError: If removal fails
        """
        command = {
            "execute": "block-dirty-bitmap-remove",
            "arguments": {
                "node": node,
                "name": name,
            },
        }

        self._execute_qmp(command)
        logger.info(f"Removed bitmap '{name}' on {node} for VM {self.vmid}")

    def enable_bitmap(self, node: str, name: str) -> None:
        """Enable recording on a bitmap.

        Args:
            node: Block device node name
            name: Bitmap name to enable

        Raises:
            QMPError: If enable fails
        """
        command = {
            "execute": "block-dirty-bitmap-enable",
            "arguments": {
                "node": node,
                "name": name,
            },
        }

        self._execute_qmp(command)
        logger.debug(f"Enabled bitmap '{name}' on {node} for VM {self.vmid}")

    def disable_bitmap(self, node: str, name: str) -> None:
        """Disable recording on a bitmap.

        Args:
            node: Block device node name
            name: Bitmap name to disable

        Raises:
            QMPError: If disable fails
        """
        command = {
            "execute": "block-dirty-bitmap-disable",
            "arguments": {
                "node": node,
                "name": name,
            },
        }

        self._execute_qmp(command)
        logger.debug(f"Disabled bitmap '{name}' on {node} for VM {self.vmid}")

    def get_disk_info(self) -> list[dict[str, Any]]:
        """Get information about VM disks for backup planning.

        Returns:
            List of disk info dicts with:
            - node: Block device node name
            - device: Device identifier
            - driver: Disk format (qcow2, raw, etc.)
            - file: Backing file path
            - supports_persistent_bitmaps: Whether bitmaps can persist
            - has_backer_bitmap: Whether Backer bitmap exists
            - bitmap_usable: Whether bitmap is usable for incremental
        """
        devices = self.query_block()
        disk_info = []

        for device in devices:
            backer_bitmap = None
            for bm in device.bitmaps:
                if bm.name.startswith(self.BITMAP_PREFIX):
                    backer_bitmap = bm
                    break

            disk_info.append({
                "node": device.node_name,
                "device": device.device,
                "driver": device.driver,
                "file": device.file,
                "supports_persistent_bitmaps": device.supports_persistent_bitmaps,
                "has_backer_bitmap": backer_bitmap is not None,
                "bitmap_usable": backer_bitmap.is_usable if backer_bitmap else False,
                "bitmap_name": backer_bitmap.name if backer_bitmap else None,
                "dirty_bytes": backer_bitmap.count if backer_bitmap else 0,
            })

        return disk_info

    def setup_incremental_tracking(self) -> dict[str, Any]:
        """Set up dirty bitmap tracking on all VM disks.

        Creates bitmaps on all disks that support it. For disks that
        don't support persistent bitmaps (RAW), transient bitmaps are
        created but will be lost on VM shutdown.

        Returns:
            Dict with setup results for each disk
        """
        devices = self.query_block()
        results = {
            "vmid": self.vmid,
            "disks": [],
            "all_persistent": True,
        }

        for device in devices:
            disk_result = {
                "node": device.node_name,
                "device": device.device,
                "driver": device.driver,
                "persistent": device.supports_persistent_bitmaps,
            }

            # Check for existing Backer bitmap
            existing = None
            for bm in device.bitmaps:
                if bm.name.startswith(self.BITMAP_PREFIX):
                    existing = bm
                    break

            if existing:
                if existing.is_usable:
                    disk_result["status"] = "exists"
                    disk_result["bitmap"] = existing.name
                elif existing.inconsistent:
                    # Remove inconsistent bitmap and create new one
                    try:
                        self.remove_bitmap(device.node_name, existing.name)
                        new_name = self.create_bitmap(
                            device.node_name,
                            persistent=device.supports_persistent_bitmaps,
                        )
                        disk_result["status"] = "recreated"
                        disk_result["bitmap"] = new_name
                    except QMPError as e:
                        disk_result["status"] = "error"
                        disk_result["error"] = str(e)
                else:
                    disk_result["status"] = "busy"
                    disk_result["bitmap"] = existing.name
            else:
                # Create new bitmap
                try:
                    bitmap_name = self.create_bitmap(
                        device.node_name,
                        persistent=device.supports_persistent_bitmaps,
                    )
                    disk_result["status"] = "created"
                    disk_result["bitmap"] = bitmap_name
                except QMPError as e:
                    disk_result["status"] = "error"
                    disk_result["error"] = str(e)

            if not device.supports_persistent_bitmaps:
                results["all_persistent"] = False

            results["disks"].append(disk_result)

        return results

    def cleanup_bitmaps(self) -> int:
        """Remove all Backer-managed bitmaps from VM.

        Returns:
            Number of bitmaps removed
        """
        devices = self.query_block()
        removed = 0

        for device in devices:
            for bitmap in device.bitmaps:
                if bitmap.name.startswith(self.BITMAP_PREFIX):
                    try:
                        if not bitmap.busy:
                            self.remove_bitmap(device.node_name, bitmap.name)
                            removed += 1
                    except QMPError as e:
                        logger.warning(
                            f"Failed to remove bitmap '{bitmap.name}' from "
                            f"{device.node_name}: {e}"
                        )

        return removed

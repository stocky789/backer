"""Unraid API client for VM and container backups.

This module provides integration with Unraid for backing up:
- KVM virtual machines (via libvirt/virsh)
- Docker containers (appdata directories)
- Flash configuration (/boot/config/)

Authentication is done via API keys.
"""

import json
import logging
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class UnraidGuestType(str, Enum):
    """Type of Unraid guest."""
    VM = "vm"  # KVM virtual machine
    DOCKER = "docker"  # Docker container
    FLASH = "flash"  # Flash configuration
    SHARE = "share"  # Array share


@dataclass
class UnraidVM:
    """Represents a VM on Unraid."""
    uuid: str
    name: str
    state: str  # running, shutoff, paused
    autostart: bool = False
    
    @property
    def is_running(self) -> bool:
        """Check if VM is running."""
        return self.state.lower() == "running"


@dataclass
class UnraidContainer:
    """Represents a Docker container on Unraid."""
    id: str
    name: str
    image: str
    state: str  # running, exited, paused
    status: str  # e.g., "Up 2 hours"
    autostart: bool = False
    
    @property
    def is_running(self) -> bool:
        """Check if container is running."""
        return self.state.lower() == "running"


@dataclass
class UnraidShare:
    """Represents a share on Unraid."""
    name: str
    path: str = ""
    free_bytes: int = 0
    used_bytes: int = 0
    total_bytes: int = 0


@dataclass
class UnraidSystemInfo:
    """System information from Unraid."""
    hostname: str = ""
    version: str = ""
    platform: str = ""
    uptime: int = 0
    cpu_model: str = ""
    cpu_cores: int = 0
    memory_total: int = 0
    memory_used: int = 0
    array_state: str = ""


class UnraidAPIError(Exception):
    """Exception raised for Unraid API errors."""

    def __init__(self, message: str, status_code: int = 0, errors: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


class UnraidAPI:
    """Client for Unraid GraphQL API.

    Example usage:
        api = UnraidAPI(
            host="192.168.1.100",
            api_key="your-api-key-here",
        )
        success, msg = api.test_connection()
        vms = api.list_vms()
        containers = api.list_containers()
    """

    def __init__(
        self,
        host: str,
        api_key: str,
        port: int = 443,
        use_https: bool = True,
        verify_ssl: bool = False,
        timeout: int = 30,
    ):
        """Initialize Unraid API client.

        Args:
            host: Unraid server hostname or IP
            api_key: API key for authentication
            port: API port (default 443 for HTTPS, 80 for HTTP)
            use_https: Whether to use HTTPS (default True)
            verify_ssl: Whether to verify SSL certificates
            timeout: Request timeout in seconds
        """
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.port = port
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        # Build base URL
        protocol = "https" if use_https else "http"
        # Don't include port in URL if it's the default
        if (use_https and port == 443) or (not use_https and port == 80):
            self.base_url = f"{protocol}://{self.host}/graphql"
        else:
            self.base_url = f"{protocol}://{self.host}:{self.port}/graphql"

        # Cached version info
        self._version: str | None = None

        # SSL context
        self._ssl_context = ssl.create_default_context()
        if not verify_ssl:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

    def _make_request(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        """Make a GraphQL request to the Unraid API.

        Args:
            query: GraphQL query string
            variables: Optional query variables

        Returns:
            Response data from the 'data' field

        Raises:
            UnraidAPIError: If request fails
        """
        # Build request body
        body = {"query": query}
        if variables:
            body["variables"] = variables

        body_bytes = json.dumps(body).encode("utf-8")

        # Prepare headers
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }

        # Create request
        request = urllib.request.Request(
            self.base_url,
            data=body_bytes,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                response_data = response.read().decode("utf-8")
                result = json.loads(response_data)

                # Check for GraphQL errors
                if "errors" in result and result["errors"]:
                    error_messages = [e.get("message", str(e)) for e in result["errors"]]
                    raise UnraidAPIError(
                        f"GraphQL errors: {'; '.join(error_messages)}",
                        errors=error_messages,
                    )

                return result.get("data")

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            try:
                error_data = json.loads(error_body)
                if "errors" in error_data:
                    error_messages = [err.get("message", str(err)) for err in error_data["errors"]]
                    message = "; ".join(error_messages)
                else:
                    message = error_data.get("message", str(e))
            except json.JSONDecodeError:
                message = error_body or str(e)

            logger.error(f"Unraid API error: {e.code}: {message}")
            raise UnraidAPIError(message, status_code=e.code)

        except urllib.error.URLError as e:
            logger.error(f"Unraid connection failed: {e.reason}")
            raise UnraidAPIError(f"Connection failed: {e.reason}")

        except Exception as e:
            logger.exception("Unexpected error in Unraid API request")
            raise UnraidAPIError(f"Request failed: {e}")

    @property
    def version(self) -> str | None:
        """Get cached Unraid version string."""
        return self._version

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Unraid server.

        Returns:
            Tuple of (success, message)
        """
        try:
            info = self.get_system_info()
            version = info.version or "Unknown"
            self._version = version
            return True, f"Unraid {version}"
        except UnraidAPIError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Connection failed: {e}"

    def get_system_info(self) -> UnraidSystemInfo:
        """Get system information from Unraid.

        Returns:
            UnraidSystemInfo object
        """
        query = """
        query {
            info {
                os {
                    platform
                    distro
                    release
                    uptime
                    hostname
                }
                cpu {
                    manufacturer
                    brand
                    cores
                    threads
                }
                memory {
                    total
                    used
                    free
                }
            }
            array {
                state
            }
        }
        """
        data = self._make_request(query)

        if not data:
            return UnraidSystemInfo()

        info = data.get("info", {})
        os_info = info.get("os", {})
        cpu_info = info.get("cpu", {})
        mem_info = info.get("memory", {})
        array_info = data.get("array", {})

        # Cache version
        version = os_info.get("release", "")
        self._version = version

        return UnraidSystemInfo(
            hostname=os_info.get("hostname", ""),
            version=version,
            platform=os_info.get("platform", ""),
            uptime=os_info.get("uptime", 0),
            cpu_model=f"{cpu_info.get('manufacturer', '')} {cpu_info.get('brand', '')}".strip(),
            cpu_cores=cpu_info.get("cores", 0),
            memory_total=mem_info.get("total", 0),
            memory_used=mem_info.get("used", 0),
            array_state=array_info.get("state", ""),
        )

    def list_vms(self) -> list[UnraidVM]:
        """List all VMs on the Unraid server.

        Returns:
            List of UnraidVM objects
        """
        query = """
        query {
            vms {
                domain {
                    uuid
                    name
                    state
                }
            }
        }
        """
        try:
            data = self._make_request(query)
            if not data or not data.get("vms"):
                return []

            vms = []
            for vm_data in data["vms"]:
                domain = vm_data.get("domain", {})
                if domain:
                    vms.append(UnraidVM(
                        uuid=domain.get("uuid", ""),
                        name=domain.get("name", ""),
                        state=domain.get("state", "unknown"),
                    ))

            return sorted(vms, key=lambda v: v.name.lower())

        except UnraidAPIError as e:
            logger.warning(f"Failed to list VMs: {e}")
            return []

    def list_containers(self) -> list[UnraidContainer]:
        """List all Docker containers on the Unraid server.

        Returns:
            List of UnraidContainer objects
        """
        query = """
        query {
            dockerContainers {
                id
                names
                image
                state
                status
                autoStart
            }
        }
        """
        try:
            data = self._make_request(query)
            if not data or not data.get("dockerContainers"):
                return []

            containers = []
            for container_data in data["dockerContainers"]:
                # Names is an array, get first one
                names = container_data.get("names", [])
                name = names[0] if names else container_data.get("id", "")[:12]
                # Remove leading slash from container name if present
                if name.startswith("/"):
                    name = name[1:]

                containers.append(UnraidContainer(
                    id=container_data.get("id", ""),
                    name=name,
                    image=container_data.get("image", ""),
                    state=container_data.get("state", "unknown"),
                    status=container_data.get("status", ""),
                    autostart=container_data.get("autoStart", False),
                ))

            return sorted(containers, key=lambda c: c.name.lower())

        except UnraidAPIError as e:
            logger.warning(f"Failed to list containers: {e}")
            return []

    def list_shares(self) -> list[UnraidShare]:
        """List all shares on the Unraid server.

        Returns:
            List of UnraidShare objects
        """
        query = """
        query {
            shares {
                name
            }
        }
        """
        try:
            data = self._make_request(query)
            if not data or not data.get("shares"):
                return []

            shares = []
            for share_data in data["shares"]:
                shares.append(UnraidShare(
                    name=share_data.get("name", ""),
                ))

            return sorted(shares, key=lambda s: s.name.lower())

        except UnraidAPIError as e:
            logger.warning(f"Failed to list shares: {e}")
            return []

    def get_array_status(self) -> dict[str, Any]:
        """Get array status information.

        Returns:
            Dict with array state and disk information
        """
        query = """
        query {
            array {
                state
                capacity {
                    disks {
                        free
                        used
                        total
                    }
                }
                disks {
                    name
                    size
                    status
                    temp
                }
            }
        }
        """
        try:
            data = self._make_request(query)
            return data.get("array", {}) if data else {}
        except UnraidAPIError as e:
            logger.warning(f"Failed to get array status: {e}")
            return {}


class UnraidBackupManager:
    """High-level backup manager for Unraid.

    Provides backup operations for:
    - VMs (via SSH + virsh)
    - Docker containers (appdata backup)
    - Flash configuration

    Backups are performed via SSH to the Unraid server.
    """

    def __init__(
        self,
        api: UnraidAPI,
        ssh_host: str | None = None,
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
    ):
        """Initialize backup manager.

        Args:
            api: Configured UnraidAPI instance
            ssh_host: SSH hostname (defaults to API host)
            ssh_user: SSH username (default: root)
            ssh_port: SSH port (default: 22)
            ssh_key_path: Path to SSH private key
            ssh_password: SSH password (if not using key)
        """
        self.api = api
        self.ssh_host = ssh_host or api.host
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.ssh_key_path = ssh_key_path
        self.ssh_password = ssh_password

    def _run_ssh_command(
        self,
        command: str,
        timeout: int = 60,
    ) -> tuple[int, str, str]:
        """Run a command on the Unraid server via SSH.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        import subprocess

        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes" if self.ssh_key_path else "BatchMode=no",
        ]

        if self.ssh_key_path:
            ssh_cmd.extend(["-i", self.ssh_key_path])
        if self.ssh_port != 22:
            ssh_cmd.extend(["-p", str(self.ssh_port)])

        ssh_cmd.append(f"{self.ssh_user}@{self.ssh_host}")
        ssh_cmd.append(command)

        # Use sshpass for password auth if needed
        if self.ssh_password and not self.ssh_key_path:
            ssh_cmd = ["sshpass", "-p", self.ssh_password] + ssh_cmd
            # Remove BatchMode=no since sshpass handles it
            ssh_cmd = [x for x in ssh_cmd if x != "BatchMode=no"]

        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                timeout=timeout,
                text=True,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "Command timed out"
        except FileNotFoundError as e:
            if "sshpass" in str(e):
                return -1, "", "sshpass not installed - required for password auth"
            return -1, "", f"SSH command not found: {e}"
        except Exception as e:
            return -1, "", str(e)

    def list_vm_disks(self, vm_name: str) -> list[dict[str, str]]:
        """List disk files for a VM using virsh.

        Args:
            vm_name: Name of the VM

        Returns:
            List of dicts with 'target' and 'source' keys
        """
        rc, stdout, stderr = self._run_ssh_command(
            f"virsh domblklist '{vm_name}' --details"
        )
        if rc != 0:
            logger.error(f"Failed to list VM disks: {stderr}")
            return []

        disks = []
        for line in stdout.strip().split("\n"):
            parts = line.split()
            # Skip header and non-file entries
            if len(parts) >= 4 and parts[0] == "file" and parts[1] == "disk":
                disks.append({
                    "target": parts[2],  # e.g., vda, sda
                    "source": parts[3],  # e.g., /mnt/user/domains/vm/vdisk1.qcow2
                })
        return disks

    def get_vm_xml(self, vm_name: str) -> str | None:
        """Get VM XML configuration.

        Args:
            vm_name: Name of the VM

        Returns:
            XML configuration string or None
        """
        rc, stdout, stderr = self._run_ssh_command(
            f"virsh dumpxml '{vm_name}'"
        )
        if rc != 0:
            logger.error(f"Failed to get VM XML: {stderr}")
            return None
        return stdout

    def backup_vm(
        self,
        vm_name: str,
        backup_path: str,
        use_snapshot: bool = True,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Backup a VM to the specified path.

        Uses virsh snapshot for live backup if VM is running.

        Args:
            vm_name: Name of the VM to backup
            backup_path: Destination path for backup files
            use_snapshot: Use live snapshot for running VMs
            progress_callback: Optional progress callback

        Returns:
            Dict with backup result info
        """
        import time
        from datetime import datetime

        started_at = datetime.now()
        result = {
            "success": False,
            "vm_name": vm_name,
            "backup_path": backup_path,
            "files": [],
            "errors": [],
        }

        # Get VM state
        vms = self.api.list_vms()
        vm = next((v for v in vms if v.name == vm_name), None)
        if not vm:
            result["errors"].append(f"VM '{vm_name}' not found")
            return result

        is_running = vm.is_running
        snapshot_created = False

        try:
            # Get disk list
            disks = self.list_vm_disks(vm_name)
            if not disks:
                result["errors"].append("No disks found for VM")
                return result

            # Create backup directory
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            vm_backup_dir = f"{backup_path}/{vm_name}_{timestamp}"
            rc, _, stderr = self._run_ssh_command(f"mkdir -p '{vm_backup_dir}'")
            if rc != 0:
                result["errors"].append(f"Failed to create backup dir: {stderr}")
                return result

            # If running and using snapshots, create external snapshot
            if is_running and use_snapshot:
                if progress_callback:
                    progress_callback({"status": "creating_snapshot", "vm": vm_name})

                # Build snapshot command with disk specs
                snapshot_name = f"backer_snap_{timestamp}"
                diskspec_args = " ".join(
                    f"--diskspec {d['target']},snapshot=external"
                    for d in disks
                )

                rc, stdout, stderr = self._run_ssh_command(
                    f"virsh snapshot-create-as --domain '{vm_name}' "
                    f"--name '{snapshot_name}' --disk-only --atomic --quiesce {diskspec_args}",
                    timeout=300,
                )

                # If quiesce fails (no guest agent), try without it
                if rc != 0 and "quiesce" in stderr.lower():
                    logger.warning("Quiesce failed, trying without guest agent")
                    rc, stdout, stderr = self._run_ssh_command(
                        f"virsh snapshot-create-as --domain '{vm_name}' "
                        f"--name '{snapshot_name}' --disk-only --atomic {diskspec_args}",
                        timeout=300,
                    )

                if rc != 0:
                    result["errors"].append(f"Failed to create snapshot: {stderr}")
                    return result

                snapshot_created = True
                logger.info(f"Created snapshot '{snapshot_name}' for VM '{vm_name}'")

                # After snapshot, the original disks are now read-only backing files
                # We need to get the updated disk list to find the new overlay files
                time.sleep(1)  # Brief pause for snapshot to settle

            # Copy disk files
            for i, disk in enumerate(disks):
                source = disk["source"]
                if not source or source == "-":
                    continue

                if progress_callback:
                    progress_callback({
                        "status": "copying",
                        "vm": vm_name,
                        "disk": disk["target"],
                        "progress": int((i / len(disks)) * 100),
                    })

                # Copy the disk file
                filename = source.split("/")[-1]
                dest_file = f"{vm_backup_dir}/{filename}"

                rc, _, stderr = self._run_ssh_command(
                    f"cp '{source}' '{dest_file}'",
                    timeout=3600,  # 1 hour for large disks
                )
                if rc != 0:
                    result["errors"].append(f"Failed to copy {source}: {stderr}")
                else:
                    result["files"].append(dest_file)
                    logger.info(f"Copied {source} to {dest_file}")

            # Save VM XML configuration
            xml_content = self.get_vm_xml(vm_name)
            if xml_content:
                xml_file = f"{vm_backup_dir}/{vm_name}.xml"
                # Write XML via SSH
                rc, _, stderr = self._run_ssh_command(
                    f"cat > '{xml_file}' << 'XMLEOF'\n{xml_content}\nXMLEOF"
                )
                if rc == 0:
                    result["files"].append(xml_file)

            # Merge snapshot back if we created one
            if snapshot_created:
                if progress_callback:
                    progress_callback({"status": "merging_snapshot", "vm": vm_name})

                # Get current disk list (overlay files)
                current_disks = self.list_vm_disks(vm_name)
                for disk in current_disks:
                    target = disk["target"]
                    # Blockcommit merges overlay into backing file
                    rc, _, stderr = self._run_ssh_command(
                        f"virsh blockcommit '{vm_name}' {target} --active --pivot --wait",
                        timeout=3600,
                    )
                    if rc != 0:
                        logger.warning(f"Failed to merge snapshot for {target}: {stderr}")

                # Delete snapshot metadata
                self._run_ssh_command(
                    f"virsh snapshot-delete '{vm_name}' --metadata --current"
                )

            result["success"] = len(result["errors"]) == 0
            result["duration_seconds"] = (datetime.now() - started_at).total_seconds()

        except Exception as e:
            logger.exception(f"VM backup failed: {e}")
            result["errors"].append(str(e))

            # Try to clean up snapshot on error
            if snapshot_created:
                try:
                    self._run_ssh_command(
                        f"virsh snapshot-delete '{vm_name}' --metadata --current"
                    )
                except Exception:
                    pass

        return result

    def backup_container_appdata(
        self,
        container_name: str,
        backup_path: str,
        appdata_path: str = "/mnt/user/appdata",
        stop_container: bool = True,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Backup a Docker container's appdata.

        Args:
            container_name: Name of the container
            backup_path: Destination path for backup
            appdata_path: Base path for appdata (default: /mnt/user/appdata)
            stop_container: Whether to stop container during backup
            progress_callback: Optional progress callback

        Returns:
            Dict with backup result info
        """
        from datetime import datetime

        started_at = datetime.now()
        result = {
            "success": False,
            "container_name": container_name,
            "backup_path": backup_path,
            "backup_file": None,
            "errors": [],
            "was_running": False,
        }

        try:
            # Check if container exists and get state
            containers = self.api.list_containers()
            container = next((c for c in containers if c.name == container_name), None)
            if not container:
                result["errors"].append(f"Container '{container_name}' not found")
                return result

            was_running = container.is_running
            result["was_running"] = was_running

            # Stop container if requested and running
            if stop_container and was_running:
                if progress_callback:
                    progress_callback({"status": "stopping", "container": container_name})

                rc, _, stderr = self._run_ssh_command(
                    f"docker stop '{container_name}'",
                    timeout=120,
                )
                if rc != 0:
                    result["errors"].append(f"Failed to stop container: {stderr}")
                    return result
                logger.info(f"Stopped container '{container_name}'")

            # Create backup
            if progress_callback:
                progress_callback({"status": "backing_up", "container": container_name})

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            container_appdata = f"{appdata_path}/{container_name}"
            backup_file = f"{backup_path}/{container_name}_{timestamp}.tar.gz"

            # Create backup directory
            rc, _, stderr = self._run_ssh_command(f"mkdir -p '{backup_path}'")
            if rc != 0:
                result["errors"].append(f"Failed to create backup dir: {stderr}")
            else:
                # Create tar archive
                rc, _, stderr = self._run_ssh_command(
                    f"tar -czf '{backup_file}' -C '{appdata_path}' '{container_name}'",
                    timeout=3600,
                )
                if rc != 0:
                    result["errors"].append(f"Failed to create backup: {stderr}")
                else:
                    result["backup_file"] = backup_file
                    logger.info(f"Created backup: {backup_file}")

            # Restart container if it was running
            if stop_container and was_running:
                if progress_callback:
                    progress_callback({"status": "starting", "container": container_name})

                rc, _, stderr = self._run_ssh_command(
                    f"docker start '{container_name}'",
                    timeout=60,
                )
                if rc != 0:
                    result["errors"].append(f"Failed to restart container: {stderr}")
                else:
                    logger.info(f"Restarted container '{container_name}'")

            result["success"] = result["backup_file"] is not None
            result["duration_seconds"] = (datetime.now() - started_at).total_seconds()

        except Exception as e:
            logger.exception(f"Container backup failed: {e}")
            result["errors"].append(str(e))

        return result

    def backup_flash_config(
        self,
        backup_path: str,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Backup the Unraid flash configuration.

        Backs up /boot/config/ which contains:
        - Disk assignments
        - Network configuration
        - Docker templates
        - Plugin configs
        - Share settings

        Args:
            backup_path: Destination path for backup
            progress_callback: Optional progress callback

        Returns:
            Dict with backup result info
        """
        from datetime import datetime

        started_at = datetime.now()
        result = {
            "success": False,
            "backup_path": backup_path,
            "backup_file": None,
            "errors": [],
        }

        try:
            if progress_callback:
                progress_callback({"status": "backing_up_flash"})

            # Create backup directory
            rc, _, stderr = self._run_ssh_command(f"mkdir -p '{backup_path}'")
            if rc != 0:
                result["errors"].append(f"Failed to create backup dir: {stderr}")
                return result

            # Get hostname for filename
            rc, hostname, _ = self._run_ssh_command("hostname")
            hostname = hostname.strip() or "unraid"

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = f"{backup_path}/flash_config_{hostname}_{timestamp}.tar.gz"

            # Create tar archive of /boot/config
            # Exclude some transient files
            rc, _, stderr = self._run_ssh_command(
                f"tar -czf '{backup_file}' "
                f"--exclude='*.log' "
                f"--exclude='super.dat.bak' "
                f"-C /boot config",
                timeout=300,
            )
            if rc != 0:
                result["errors"].append(f"Failed to create flash backup: {stderr}")
            else:
                result["backup_file"] = backup_file
                result["success"] = True
                logger.info(f"Created flash backup: {backup_file}")

            result["duration_seconds"] = (datetime.now() - started_at).total_seconds()

        except Exception as e:
            logger.exception(f"Flash backup failed: {e}")
            result["errors"].append(str(e))

        return result

    def backup_share(
        self,
        share_name: str,
        backup_path: str,
        excludes: list[str] | None = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Backup an Unraid user share.

        Uses rsync to backup the share to the destination path.

        Args:
            share_name: Name of the share to backup
            backup_path: Destination path for backup
            excludes: List of patterns to exclude
            progress_callback: Optional progress callback

        Returns:
            Dict with backup result info
        """
        from datetime import datetime

        started_at = datetime.now()
        result = {
            "success": False,
            "share_name": share_name,
            "backup_path": backup_path,
            "errors": [],
            "bytes_transferred": 0,
        }

        try:
            # Verify share exists
            shares = self.api.list_shares()
            share = next((s for s in shares if s.name == share_name), None)
            if not share:
                result["errors"].append(f"Share '{share_name}' not found")
                return result

            if progress_callback:
                progress_callback({"status": "backing_up_share", "share": share_name})

            # Create backup directory
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            share_backup_dir = f"{backup_path}/{share_name}_{timestamp}"
            rc, _, stderr = self._run_ssh_command(f"mkdir -p '{share_backup_dir}'")
            if rc != 0:
                result["errors"].append(f"Failed to create backup dir: {stderr}")
                return result

            # Build rsync command
            share_path = f"/mnt/user/{share_name}"
            exclude_args = ""
            if excludes:
                exclude_args = " ".join(f"--exclude='{e}'" for e in excludes)

            # Use rsync for efficient file copying with progress
            rsync_cmd = (
                f"rsync -av --info=progress2 {exclude_args} "
                f"'{share_path}/' '{share_backup_dir}/'"
            )

            rc, stdout, stderr = self._run_ssh_command(
                rsync_cmd,
                timeout=7200,  # 2 hours for large shares
            )
            if rc != 0:
                result["errors"].append(f"rsync failed: {stderr}")
            else:
                result["success"] = True
                result["backup_dir"] = share_backup_dir
                logger.info(f"Created share backup: {share_backup_dir}")

                # Try to parse bytes transferred from rsync output
                import re
                bytes_match = re.search(r"sent\s+([\d,]+)\s+bytes", stdout)
                if bytes_match:
                    result["bytes_transferred"] = int(bytes_match.group(1).replace(",", ""))

            result["duration_seconds"] = (datetime.now() - started_at).total_seconds()

        except Exception as e:
            logger.exception(f"Share backup failed: {e}")
            result["errors"].append(str(e))

        return result

    def list_all_guests(self) -> list[dict[str, Any]]:
        """List all backupable items on Unraid.

        Returns a unified list of VMs, containers, shares, and flash config
        in a format compatible with the hypervisor guests API.

        Returns:
            List of guest dicts with type, id, name, status fields
        """
        guests = []

        # Add VMs
        try:
            vms = self.api.list_vms()
            for vm in vms:
                guests.append({
                    "vmid": vm.uuid,
                    "name": vm.name,
                    "type": "vm",
                    "status": vm.state,
                    "node": "unraid",
                    "guest_type": UnraidGuestType.VM.value,
                })
        except Exception as e:
            logger.warning(f"Failed to list VMs: {e}")

        # Add Docker containers
        try:
            containers = self.api.list_containers()
            for container in containers:
                guests.append({
                    "vmid": container.id[:12],  # Short container ID
                    "name": container.name,
                    "type": "docker",
                    "status": container.state,
                    "node": "unraid",
                    "guest_type": UnraidGuestType.DOCKER.value,
                    "image": container.image,
                })
        except Exception as e:
            logger.warning(f"Failed to list containers: {e}")

        # Add shares
        try:
            shares = self.api.list_shares()
            for share in shares:
                guests.append({
                    "vmid": f"share_{share.name}",
                    "name": share.name,
                    "type": "share",
                    "status": "available",
                    "node": "unraid",
                    "guest_type": UnraidGuestType.SHARE.value,
                    "path": f"/mnt/user/{share.name}",
                })
        except Exception as e:
            logger.warning(f"Failed to list shares: {e}")

        # Add flash/USB config as a special "guest"
        guests.append({
            "vmid": "flash_config",
            "name": "Flash/USB Configuration",
            "type": "flash",
            "status": "available",
            "node": "unraid",
            "guest_type": UnraidGuestType.FLASH.value,
        })

        return guests

    def run_backup(
        self,
        guest_type: str,
        guest_id: str,
        backup_path: str,
        options: dict[str, Any] | None = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Run a backup based on guest type.

        This is a unified backup method that dispatches to the appropriate
        backup method based on the guest type.

        Args:
            guest_type: Type of guest (vm, docker, share, flash)
            guest_id: ID or name of the guest
            backup_path: Destination path for backup
            options: Additional backup options
            progress_callback: Optional progress callback

        Returns:
            Dict with backup result info
        """
        options = options or {}

        if guest_type == UnraidGuestType.VM.value or guest_type == "vm":
            # For VMs, guest_id is the VM name
            return self.backup_vm(
                vm_name=guest_id,
                backup_path=backup_path,
                use_snapshot=options.get("use_snapshot", True),
                progress_callback=progress_callback,
            )

        elif guest_type == UnraidGuestType.DOCKER.value or guest_type == "docker":
            # For containers, guest_id is the container name
            return self.backup_container_appdata(
                container_name=guest_id,
                backup_path=backup_path,
                appdata_path=options.get("appdata_path", "/mnt/user/appdata"),
                stop_container=options.get("stop_container", True),
                progress_callback=progress_callback,
            )

        elif guest_type == UnraidGuestType.SHARE.value or guest_type == "share":
            # For shares, guest_id is the share name (may have share_ prefix)
            share_name = guest_id
            if share_name.startswith("share_"):
                share_name = share_name[6:]
            return self.backup_share(
                share_name=share_name,
                backup_path=backup_path,
                excludes=options.get("excludes"),
                progress_callback=progress_callback,
            )

        elif guest_type == UnraidGuestType.FLASH.value or guest_type == "flash":
            return self.backup_flash_config(
                backup_path=backup_path,
                progress_callback=progress_callback,
            )

        else:
            return {
                "success": False,
                "errors": [f"Unknown guest type: {guest_type}"],
            }

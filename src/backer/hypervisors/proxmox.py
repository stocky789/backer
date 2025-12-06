"""Proxmox VE API client for VM and container backups.

This module provides integration with Proxmox VE for backing up:
- QEMU/KVM virtual machines
- LXC containers

Authentication is done via API tokens (recommended) or username/password tickets.
"""

import json
import logging
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ProxmoxAuthMethod(str, Enum):
    """Authentication method for Proxmox API."""
    TOKEN = "token"
    PASSWORD = "password"


class ProxmoxBackupMode(str, Enum):
    """Backup mode for vzdump."""
    SNAPSHOT = "snapshot"  # Live backup, minimal downtime
    STOP = "stop"  # Stop VM/CT, highest consistency
    SUSPEND = "suspend"  # Suspend before backup


class ProxmoxCompression(str, Enum):
    """Compression algorithm for backups."""
    NONE = "0"
    LZO = "lzo"
    GZIP = "gzip"
    ZSTD = "zstd"


class ProxmoxGuestType(str, Enum):
    """Type of Proxmox guest."""
    QEMU = "qemu"  # KVM virtual machine
    LXC = "lxc"  # Linux container


@dataclass
class ProxmoxGuest:
    """Represents a VM or container on Proxmox."""
    vmid: int
    name: str
    node: str
    guest_type: ProxmoxGuestType
    status: str  # running, stopped, paused
    cpus: int = 0
    maxmem: int = 0  # bytes
    maxdisk: int = 0  # bytes
    uptime: int = 0  # seconds
    template: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def maxmem_gb(self) -> float:
        """Memory in GB."""
        return self.maxmem / (1024**3)

    @property
    def maxdisk_gb(self) -> float:
        """Disk in GB."""
        return self.maxdisk / (1024**3)


@dataclass
class ProxmoxBackup:
    """Represents a backup on Proxmox storage."""
    volid: str  # e.g., "local:backup/vzdump-qemu-100-2024_01_15-12_00_00.vma.zst"
    vmid: int
    node: str
    ctime: datetime  # creation time
    size: int  # bytes
    format: str  # vma, tar
    notes: str = ""
    protected: bool = False

    @property
    def size_gb(self) -> float:
        """Size in GB."""
        return self.size / (1024**3)

    @property
    def guest_type(self) -> str:
        """Detect guest type (qemu/lxc) from backup volid.

        Backup naming convention: vzdump-{qemu|lxc}-{vmid}-{timestamp}
        Example: local:backup/vzdump-qemu-100-2024_01_15-12_00_00.vma.zst
        """
        # Extract filename from volid (format: "storage:path/filename")
        if "vzdump-lxc-" in self.volid:
            return "lxc"
        return "qemu"  # Default to qemu


@dataclass
class ProxmoxStorage:
    """Represents a Proxmox storage location."""
    storage: str  # Storage ID/name
    type: str  # dir, nfs, cifs, pbs, etc.
    content: list[str]  # backup, images, iso, etc.
    node: str | None = None  # Node this storage belongs to (None = shared)
    path: str = ""
    active: bool = True
    enabled: bool = True
    shared: bool = False
    total: int = 0  # bytes
    used: int = 0  # bytes
    avail: int = 0  # bytes

    @property
    def supports_backup(self) -> bool:
        """Check if storage supports backups."""
        return "backup" in self.content


@dataclass
class ProxmoxNode:
    """Represents a Proxmox cluster node."""
    node: str
    status: str  # online, offline
    cpu: float = 0.0  # usage percentage
    maxcpu: int = 0
    mem: int = 0  # used bytes
    maxmem: int = 0  # total bytes
    uptime: int = 0


@dataclass
class ProxmoxTaskStatus:
    """Status of a Proxmox task (backup, restore, etc.)."""
    upid: str
    node: str
    status: str  # running, stopped
    exitstatus: str = ""  # OK, error message
    type: str = ""  # vzdump, qmrestore, etc.
    user: str = ""
    starttime: datetime | None = None
    endtime: datetime | None = None
    pid: int = 0

    @property
    def is_running(self) -> bool:
        """Check if task is still running."""
        return self.status == "running"

    @property
    def is_success(self) -> bool:
        """Check if task completed successfully."""
        return self.status == "stopped" and self.exitstatus == "OK"


class ProxmoxAPIError(Exception):
    """Exception raised for Proxmox API errors."""

    def __init__(self, message: str, status_code: int = 0, errors: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


class ProxmoxAPI:
    """Client for Proxmox VE REST API.

    Supports both API token authentication (recommended) and password/ticket auth.

    Example usage with API token:
        api = ProxmoxAPI(
            host="192.168.1.100",
            token_id="root@pam!backup",
            token_secret="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        )
        vms = api.list_guests()

    Example usage with password:
        api = ProxmoxAPI(
            host="192.168.1.100",
            username="root@pam",
            password="mypassword",
            auth_method=ProxmoxAuthMethod.PASSWORD,
        )
        api.authenticate()
        vms = api.list_guests()
    """

    def __init__(
        self,
        host: str,
        port: int = 8006,
        token_id: str | None = None,
        token_secret: str | None = None,
        username: str | None = None,
        password: str | None = None,
        auth_method: ProxmoxAuthMethod = ProxmoxAuthMethod.TOKEN,
        verify_ssl: bool = False,
        timeout: int = 30,
    ):
        """Initialize Proxmox API client.

        Args:
            host: Proxmox server hostname or IP
            port: API port (default 8006)
            token_id: API token ID (e.g., "root@pam!backup")
            token_secret: API token secret UUID
            username: Username for password auth (e.g., "root@pam")
            password: Password for password auth
            auth_method: Authentication method to use
            verify_ssl: Whether to verify SSL certificates
            timeout: Request timeout in seconds
        """
        self.host = host.rstrip("/")
        self.port = port
        self.base_url = f"https://{self.host}:{self.port}/api2/json"

        self.auth_method = auth_method
        self.token_id = token_id
        self.token_secret = token_secret
        self.username = username
        self.password = password

        self.verify_ssl = verify_ssl
        self.timeout = timeout

        # Ticket-based auth state
        self._ticket: str | None = None
        self._csrf_token: str | None = None
        self._ticket_expires: datetime | None = None

        # Cached version info
        self._version: str | None = None

        # SSL context
        self._ssl_context = ssl.create_default_context()
        if not verify_ssl:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Proxmox API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (e.g., "/nodes")
            data: Request body for POST/PUT
            params: Query parameters

        Returns:
            JSON response data (can be dict, list, or None)

        Raises:
            ProxmoxAPIError: If request fails
        """
        url = f"{self.base_url}{endpoint}"

        # Add query parameters
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        # Prepare headers
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        # Add authentication
        if self.auth_method == ProxmoxAuthMethod.TOKEN:
            if not self.token_id or not self.token_secret:
                raise ProxmoxAPIError("API token credentials not configured")
            headers["Authorization"] = f"PVEAPIToken={self.token_id}={self.token_secret}"
        else:
            # Ticket-based auth
            if not self._ticket:
                raise ProxmoxAPIError("Not authenticated. Call authenticate() first.")
            headers["Cookie"] = f"PVEAuthCookie={self._ticket}"
            if method in ("POST", "PUT", "DELETE") and self._csrf_token:
                headers["CSRFPreventionToken"] = self._csrf_token

        # Prepare request body
        body = None
        if data:
            body = urllib.parse.urlencode(data).encode("utf-8")

        # Create request
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                response_data = response.read().decode("utf-8")
                result = json.loads(response_data)
                data = result.get("data", result)
                if data is None:
                    logger.debug(f"Proxmox API returned null data for {endpoint}: {result}")
                return data

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            try:
                error_data = json.loads(error_body)
                errors = error_data.get("errors", {})
                error_list = [f"{k}: {v}" for k, v in errors.items()] if errors else []
                message = error_data.get("message", str(e))
                # Include field-specific errors in the message for clarity
                if error_list:
                    message = f"{message} ({'; '.join(error_list)})"
            except json.JSONDecodeError:
                message = error_body or str(e)
                error_list = []

            # Log at appropriate level - "does not exist" is often expected (e.g., checking if storage exists)
            if e.code in (404, 500) and "does not exist" in message:
                logger.debug(
                    f"Proxmox API: {method} {endpoint} returned {e.code}: {message}"
                )
            else:
                logger.error(
                    f"Proxmox API error: {method} {endpoint} returned {e.code}: {message}"
                )
            raise ProxmoxAPIError(message, status_code=e.code, errors=error_list)

        except urllib.error.URLError as e:
            logger.error(f"Proxmox connection failed for {endpoint}: {e.reason}")
            raise ProxmoxAPIError(f"Connection failed: {e.reason}")

        except Exception as e:
            logger.exception(f"Unexpected error in Proxmox API request: {method} {endpoint}")
            raise ProxmoxAPIError(f"Request failed: {e}")

    def authenticate(self) -> bool:
        """Authenticate with username/password to get a ticket.

        Only needed for password-based authentication.
        Token-based auth doesn't require this.

        Returns:
            True if authentication successful

        Raises:
            ProxmoxAPIError: If authentication fails
        """
        if self.auth_method == ProxmoxAuthMethod.TOKEN:
            # Test token by making a request
            try:
                self.get_version()
                return True
            except ProxmoxAPIError:
                raise

        if not self.username or not self.password:
            raise ProxmoxAPIError("Username and password required for password auth")

        url = f"{self.base_url}/access/ticket"
        data = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
        }).encode("utf-8")

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                response_data = response.read().decode("utf-8")
                result = json.loads(response_data)
                auth_data = result.get("data", {})

                self._ticket = auth_data.get("ticket")
                self._csrf_token = auth_data.get("CSRFPreventionToken")
                # Tickets are valid for 2 hours
                self._ticket_expires = datetime.now()

                if not self._ticket:
                    raise ProxmoxAPIError("No ticket in authentication response")

                return True

        except urllib.error.HTTPError as e:
            raise ProxmoxAPIError(f"Authentication failed: {e}", status_code=e.code)

        except Exception as e:
            raise ProxmoxAPIError(f"Authentication failed: {e}")

    @property
    def version(self) -> str | None:
        """Get cached Proxmox version string."""
        return self._version

    def get_version(self) -> dict[str, Any]:
        """Get Proxmox VE version information.

        Returns:
            Version info dict with keys: version, release, repoid
        """
        data = self._make_request("GET", "/version")
        if not data:
            return {"version": "unknown", "release": ""}
        # Cache the version
        ver = data.get("version", "")
        release = data.get("release", "")
        self._version = f"{ver}-{release}" if release else ver
        return data

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Proxmox server.

        Returns:
            Tuple of (success, message)
        """
        try:
            if self.auth_method == ProxmoxAuthMethod.PASSWORD:
                self.authenticate()
            version = self.get_version()
            if not version:
                return False, "No version information returned"
            ver = version.get("version", "unknown")
            release = version.get("release", "")
            return True, f"Proxmox VE {ver}-{release}"
        except ProxmoxAPIError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Connection failed: {e}"

    # =========================================================================
    # Node Operations
    # =========================================================================

    def list_nodes(self) -> list[ProxmoxNode]:
        """List all nodes in the cluster.

        Returns:
            List of ProxmoxNode objects
        """
        data = self._make_request("GET", "/nodes") or []
        nodes = []

        for item in data:
            nodes.append(ProxmoxNode(
                node=item.get("node", ""),
                status=item.get("status", "unknown"),
                cpu=item.get("cpu", 0) * 100,  # Convert to percentage
                maxcpu=item.get("maxcpu", 0),
                mem=item.get("mem", 0),
                maxmem=item.get("maxmem", 0),
                uptime=item.get("uptime", 0),
            ))

        return nodes

    # =========================================================================
    # Guest (VM/Container) Operations
    # =========================================================================

    def list_guests(self, node: str | None = None) -> list[ProxmoxGuest]:
        """List all VMs and containers.

        Args:
            node: Optional node name to filter by. If None, lists from all nodes.

        Returns:
            List of ProxmoxGuest objects
        """
        guests = []

        if node:
            nodes = [node]
        else:
            # Get all nodes - include those with status "online" or "unknown"
            # (unknown often means the node is reachable but status not fully reported)
            all_nodes = self.list_nodes()
            nodes = [n.node for n in all_nodes if n.status != "offline"]
            if not nodes:
                logger.warning("No online nodes found in cluster")
                return []

        for node_name in nodes:
            # Get QEMU VMs
            try:
                vms = self._make_request("GET", f"/nodes/{node_name}/qemu") or []
                for vm in vms:
                    guests.append(ProxmoxGuest(
                        vmid=vm.get("vmid", 0),
                        name=vm.get("name", f"VM {vm.get('vmid')}"),
                        node=node_name,
                        guest_type=ProxmoxGuestType.QEMU,
                        status=vm.get("status", "unknown"),
                        cpus=vm.get("cpus", 0),
                        maxmem=vm.get("maxmem", 0),
                        maxdisk=vm.get("maxdisk", 0),
                        uptime=vm.get("uptime", 0),
                        template=vm.get("template", 0) == 1,
                        tags=vm.get("tags", "").split(";") if vm.get("tags") else [],
                    ))
            except ProxmoxAPIError as e:
                logger.warning(f"Failed to list VMs on {node_name}: {e}")

            # Get LXC containers
            try:
                cts = self._make_request("GET", f"/nodes/{node_name}/lxc") or []
                for ct in cts:
                    guests.append(ProxmoxGuest(
                        vmid=ct.get("vmid", 0),
                        name=ct.get("name", f"CT {ct.get('vmid')}"),
                        node=node_name,
                        guest_type=ProxmoxGuestType.LXC,
                        status=ct.get("status", "unknown"),
                        cpus=ct.get("cpus", 0),
                        maxmem=ct.get("maxmem", 0),
                        maxdisk=ct.get("maxdisk", 0),
                        uptime=ct.get("uptime", 0),
                        template=ct.get("template", 0) == 1,
                        tags=ct.get("tags", "").split(";") if ct.get("tags") else [],
                    ))
            except ProxmoxAPIError as e:
                logger.warning(f"Failed to list containers on {node_name}: {e}")

        return sorted(guests, key=lambda g: g.vmid)

    def get_guest(self, node: str, vmid: int, guest_type: ProxmoxGuestType) -> ProxmoxGuest | None:
        """Get details for a specific guest.

        Args:
            node: Node name
            vmid: VM/container ID
            guest_type: QEMU or LXC

        Returns:
            ProxmoxGuest or None if not found
        """
        endpoint = f"/nodes/{node}/{guest_type.value}/{vmid}/status/current"
        try:
            data = self._make_request("GET", endpoint)
            if not data:
                return None
            return ProxmoxGuest(
                vmid=vmid,
                name=data.get("name", f"{guest_type.value.upper()} {vmid}"),
                node=node,
                guest_type=guest_type,
                status=data.get("status", "unknown"),
                cpus=data.get("cpus", 0),
                maxmem=data.get("maxmem", 0),
                maxdisk=data.get("maxdisk", 0),
                uptime=data.get("uptime", 0),
            )
        except ProxmoxAPIError:
            return None

    # =========================================================================
    # Storage Operations
    # =========================================================================

    def list_storages(self, node: str | None = None) -> list[ProxmoxStorage]:
        """List available storage locations.

        Args:
            node: Optional node name. If provided, shows storage status for that node.

        Returns:
            List of ProxmoxStorage objects
        """
        if node:
            endpoint = f"/nodes/{node}/storage"
        else:
            endpoint = "/storage"

        data = self._make_request("GET", endpoint) or []
        storages = []

        for item in data:
            content = item.get("content", "")
            if isinstance(content, str):
                content = content.split(",") if content else []

            storages.append(ProxmoxStorage(
                storage=item.get("storage", ""),
                type=item.get("type", ""),
                content=content,
                node=node,  # Track which node this is from
                path=item.get("path", ""),
                active=item.get("active", 1) == 1,
                enabled=item.get("enabled", 1) == 1,
                shared=item.get("shared", 0) == 1,
                total=item.get("total", 0),
                used=item.get("used", 0),
                avail=item.get("avail", 0),
            ))

        return [s for s in storages if s.supports_backup]

    def get_storage(self, storage_id: str) -> dict[str, Any] | None:
        """Get a specific storage by ID.

        Args:
            storage_id: Storage identifier

        Returns:
            Storage configuration dict or None if storage doesn't exist
        """
        try:
            data = self._make_request("GET", f"/storage/{storage_id}")
            return data
        except ProxmoxAPIError as e:
            # Proxmox may return 404 or 500 with "does not exist" message
            if e.status_code == 404:
                return None
            if e.status_code == 500 and "does not exist" in str(e):
                return None
            raise

    def get_storage_status(self, storage_id: str, node: str | None = None) -> dict[str, Any] | None:
        """Get storage status including mount state.

        Args:
            storage_id: Storage identifier
            node: Node to check status on (default: first available node)

        Returns:
            Storage status dict with 'active' field, or None if not found
        """
        try:
            # Get the node to query
            if not node:
                nodes = self.list_nodes()
                if not nodes:
                    return None
                node = nodes[0]["node"]

            # Query storage status on the node
            data = self._make_request("GET", f"/nodes/{node}/storage/{storage_id}/status")
            return data
        except ProxmoxAPIError:
            # Any error checking status - assume not ready yet
            return None
        except Exception as e:
            logger.debug(f"Error checking storage status: {e}")
            return None

    def _ensure_smb_directory(
        self,
        server: str,
        share: str,
        path: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
    ) -> None:
        """Ensure a directory exists on an SMB share using smbclient.

        Creates the directory and all parent directories if they don't exist.
        This is needed because Proxmox CIFS mount requires the subdir to exist.

        Args:
            server: SMB server hostname/IP
            share: SMB share name
            path: Directory path to create (without leading slash)
            username: SMB username
            password: SMB password
            domain: SMB domain
        """
        import subprocess

        # Build smbclient auth arguments
        auth_parts = []
        if username:
            if domain:
                auth_parts.extend(["-U", f"{domain}\\{username}%{password or ''}"])
            else:
                auth_parts.extend(["-U", f"{username}%{password or ''}"])
        else:
            auth_parts.extend(["-N"])  # No password (guest)

        # Create each directory level
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            partial_path = "/".join(parts[:i])
            if not partial_path:
                continue

            mkdir_cmd = [
                "smbclient",
                f"//{server}/{share}",
                *auth_parts,
                "-c", f"mkdir {partial_path}",
            ]

            try:
                result = subprocess.run(
                    mkdir_cmd,
                    capture_output=True,
                    timeout=30,
                )
                # mkdir may fail if directory exists - that's OK
                if result.returncode == 0:
                    logger.debug(f"Created SMB directory: //{server}/{share}/{partial_path}")
                else:
                    # Check if it's just "already exists" error
                    stderr = result.stderr.decode() if result.stderr else ""
                    if "NT_STATUS_OBJECT_NAME_COLLISION" in stderr:
                        logger.debug(f"SMB directory already exists: //{server}/{share}/{partial_path}")
                    else:
                        logger.debug(f"smbclient mkdir {partial_path}: {stderr}")
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout creating SMB directory: {partial_path}")
            except Exception as e:
                logger.warning(f"Error creating SMB directory {partial_path}: {e}")

        logger.info(f"Ensured SMB directory exists: //{server}/{share}/{path}")

    def create_nfs_storage(
        self,
        storage_id: str,
        server: str,
        export: str,
        content: list[str] | None = None,
        nodes: list[str] | None = None,
        disable: bool = False,
        max_protected_backups: int = 5,
    ) -> dict[str, Any]:
        """Create an NFS storage in Proxmox.

        Args:
            storage_id: Unique storage identifier (e.g., "backer-myrepo")
            server: NFS server hostname or IP
            export: NFS export path (e.g., "/share/backups")
            content: Content types (default: ["backup"])
            nodes: List of nodes to enable storage on (None = all)
            disable: Whether to create storage disabled
            max_protected_backups: Max protected backups (default 5)

        Returns:
            Created storage info
        """
        data: dict[str, Any] = {
            "storage": storage_id,
            "type": "nfs",
            "server": server,
            "export": export,
            "content": ",".join(content or ["backup"]),
        }

        if nodes:
            data["nodes"] = ",".join(nodes)
        if disable:
            data["disable"] = 1
        if max_protected_backups != 5:
            data["max-protected-backups"] = max_protected_backups

        return self._make_request("POST", "/storage", data=data)

    def create_cifs_storage(
        self,
        storage_id: str,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
        content: list[str] | None = None,
        nodes: list[str] | None = None,
        subdir: str | None = None,
        disable: bool = False,
        max_protected_backups: int = 5,
    ) -> dict[str, Any]:
        """Create a CIFS/SMB storage in Proxmox.

        Args:
            storage_id: Unique storage identifier (e.g., "backer-myrepo")
            server: SMB server hostname or IP
            share: SMB share name
            username: SMB username (optional for guest access)
            password: SMB password
            domain: SMB domain (optional)
            content: Content types (default: ["backup"])
            nodes: List of nodes to enable storage on (None = all)
            subdir: Subdirectory within the share
            disable: Whether to create storage disabled
            max_protected_backups: Max protected backups (default 5)

        Returns:
            Created storage info
        """
        data: dict[str, Any] = {
            "storage": storage_id,
            "type": "cifs",
            "server": server,
            "share": share,
            "content": ",".join(content or ["backup"]),
        }

        if username:
            data["username"] = username
        if password:
            data["password"] = password
        if domain:
            data["domain"] = domain
        if nodes:
            data["nodes"] = ",".join(nodes)
        if subdir:
            data["subdir"] = subdir
        if disable:
            data["disable"] = 1
        if max_protected_backups != 5:
            data["max-protected-backups"] = max_protected_backups

        # Log parameters (without password) for debugging
        log_data = {k: v for k, v in data.items() if k != "password"}
        log_data["password"] = "***" if password else None
        logger.info(f"Creating CIFS storage with params: {log_data}")

        return self._make_request("POST", "/storage", data=data)

    def update_storage(
        self,
        storage_id: str,
        content: list[str] | None = None,
        nodes: list[str] | None = None,
        disable: bool | None = None,
    ) -> dict[str, Any]:
        """Update an existing storage configuration.

        Args:
            storage_id: Storage identifier to update
            content: New content types
            nodes: New node list
            disable: Whether to disable storage

        Returns:
            Updated storage info
        """
        data: dict[str, Any] = {}

        if content is not None:
            data["content"] = ",".join(content)
        if nodes is not None:
            data["nodes"] = ",".join(nodes)
        if disable is not None:
            data["disable"] = 1 if disable else 0

        if not data:
            return {}

        return self._make_request("PUT", f"/storage/{storage_id}", data=data)

    def delete_storage(self, storage_id: str) -> None:
        """Delete a storage configuration from Proxmox.

        Args:
            storage_id: Storage identifier to delete
        """
        self._make_request("DELETE", f"/storage/{storage_id}")

    def ensure_backer_storage(
        self,
        repository: dict[str, Any],
        hypervisor_name: str | None = None,
        storage_id: str | None = None,
    ) -> str:
        """Ensure a Backer repository is configured as Proxmox storage.

        Creates or verifies that a Proxmox storage exists for the given
        Backer repository. This enables vzdump to write directly to the
        repository.

        Backups are organized under: {repo_path}/Hypervisors/{hypervisor_name}/

        Args:
            repository: Backer repository dict with keys:
                - repo_type: "smb" or "nfs"
                - server: Server hostname/IP
                - share: Share name (SMB) or export path (NFS)
                - path: Subdirectory within share (optional)
                - username: SMB username (optional)
                - password: SMB password (optional)
                - domain: SMB domain (optional)
            hypervisor_name: Name of the hypervisor (for folder organization)
            storage_id: Override storage ID (default: "backer-{repo_name}")

        Returns:
            The storage ID to use for backups

        Raises:
            ProxmoxAPIError: If storage creation fails
        """
        repo_type = repository.get("repo_type", "").lower()
        repo_name = repository.get("name", "unknown")

        # Generate storage ID from repo name (sanitized)
        if not storage_id:
            # Proxmox storage IDs: use only alphanumeric, dash, and underscore
            # Avoid dots as they can cause issues in some Proxmox versions
            # Replace dots and other invalid chars with dash
            safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in repo_name)
            # Collapse multiple consecutive dashes into single dash
            while "--" in safe_name:
                safe_name = safe_name.replace("--", "-")
            # Remove leading/trailing dashes
            safe_name = safe_name.strip("-")
            # Ensure we have a valid name
            if not safe_name:
                safe_name = "repo"
            storage_id = f"backer-{safe_name}"
            # Truncate to avoid potential GUI display issues (keep under 40 chars total)
            if len(storage_id) > 40:
                storage_id = storage_id[:40].rstrip("-")

        # Get server from repository
        server = repository.get("server", "")
        if not server:
            raise ProxmoxAPIError(f"Repository '{repo_name}' has no server configured")

        # Check if storage already exists
        existing = self.get_storage(storage_id)
        if existing:
            # Verify the storage points to the same server
            existing_server = existing.get("server", "")
            if existing_server and existing_server != server:
                logger.warning(
                    f"Proxmox storage '{storage_id}' exists but points to different server "
                    f"({existing_server} vs {server}). Using existing storage."
                )
            else:
                logger.info(f"Proxmox storage '{storage_id}' already exists")

            # Still need to wait for it to be active/mounted
            max_wait = 30  # seconds
            poll_interval = 2  # seconds
            waited = 0

            while waited < max_wait:
                status = self.get_storage_status(storage_id)
                if status and status.get("active"):
                    logger.info(f"Storage '{storage_id}' is active and ready")
                    return storage_id
                logger.debug(f"Waiting for existing storage '{storage_id}' to become active...")
                time.sleep(poll_interval)
                waited += poll_interval

            logger.warning(
                f"Existing storage '{storage_id}' not active after {max_wait}s. "
                "Backup may fail if mount is not ready."
            )
            return storage_id

        # Create storage based on repository type
        if repo_type == "nfs":
            base_export = repository.get("share") or repository.get("path", "")
            if not base_export:
                raise ProxmoxAPIError(f"NFS repository '{repo_name}' has no export path")

            # NFS doesn't support subdir like CIFS - the export path must exist on the server
            # Use the base export path directly. Vzdump will create dump/ subfolder automatically.
            # Note: For proper Hypervisors/ organization with NFS, the NFS export should be
            # configured to point to the Hypervisors/{name} folder on the NFS server.
            export = base_export

            logger.info(f"Creating NFS storage '{storage_id}' -> {server}:{export}")
            logger.warning(
                f"NFS storage does not support subdirectories. Backups will go to {export}/dump/. "
                "For Hypervisors/ folder organization, configure NFS export path on the server."
            )
            self.create_nfs_storage(
                storage_id=storage_id,
                server=server,
                export=export,
            )

        elif repo_type == "smb":
            share = repository.get("share", "")
            if not share:
                raise ProxmoxAPIError(f"SMB repository '{repo_name}' has no share name")

            username = repository.get("username")
            password = repository.get("password")  # Already decrypted by caller
            domain = repository.get("domain")

            # Build subdir: {repo_path}/Hypervisors/{hypervisor_name}
            base_path = repository.get("path", "").strip("/")
            if hypervisor_name:
                # Sanitize hypervisor name for folder
                safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor_name)
                subdir_parts = [p for p in [base_path, "Hypervisors", safe_hv_name] if p]
                subdir = "/" + "/".join(subdir_parts)
            elif base_path:
                subdir = f"/{base_path}"
            else:
                subdir = None

            # Pre-create the subdir on the SMB share using smbclient
            # Proxmox CIFS mount requires the subdir to exist
            if subdir:
                self._ensure_smb_directory(
                    server=server,
                    share=share,
                    path=subdir.lstrip("/"),
                    username=username,
                    password=password,
                    domain=domain,
                )

            logger.info(
                f"Creating CIFS storage '{storage_id}' -> //{server}/{share} "
                f"(user={username or 'guest'}, domain={domain or 'none'}, subdir={subdir or 'none'})"
            )
            self.create_cifs_storage(
                storage_id=storage_id,
                server=server,
                share=share,
                username=username,
                password=password,
                domain=domain,
                subdir=subdir,
            )

        else:
            raise ProxmoxAPIError(
                f"Repository type '{repo_type}' cannot be configured as Proxmox storage. "
                "Only NFS and SMB repositories are supported for hypervisor backups."
            )

        # Verify storage was created successfully
        created_storage = self.get_storage(storage_id)
        if not created_storage:
            raise ProxmoxAPIError(
                f"Storage '{storage_id}' was not created. Check Proxmox server logs for details."
            )

        logger.info(f"Created Proxmox storage '{storage_id}' for repository '{repo_name}'")

        # Wait for storage to be mounted and active
        # Proxmox mounts storage asynchronously, so we need to poll until ready
        max_wait = 30  # seconds
        poll_interval = 2  # seconds
        waited = 0

        while waited < max_wait:
            status = self.get_storage_status(storage_id)
            logger.info(f"Storage status check: active={status.get('active') if status else 'N/A'}, status={status}")
            if status and status.get("active"):
                logger.info(f"Storage '{storage_id}' is active and ready")
                break
            logger.info(f"Waiting for storage '{storage_id}' to become active (waited {waited}s)...")
            time.sleep(poll_interval)
            waited += poll_interval
        else:
            logger.warning(
                f"Storage '{storage_id}' not active after {max_wait}s. "
                "Backup may fail if mount is not ready."
            )
        return storage_id

    # =========================================================================
    # Backup Operations
    # =========================================================================

    def list_backups(
        self,
        node: str,
        storage: str,
        vmid: int | None = None,
    ) -> list[ProxmoxBackup]:
        """List backups on a storage.

        Args:
            node: Node name
            storage: Storage ID
            vmid: Optional VM ID to filter by

        Returns:
            List of ProxmoxBackup objects
        """
        params: dict[str, Any] = {"content": "backup"}
        if vmid is not None:
            params["vmid"] = str(vmid)

        endpoint = f"/nodes/{node}/storage/{storage}/content"
        data = self._make_request("GET", endpoint, params=params) or []
        backups = []

        for item in data:
            # Only include backup content
            if item.get("content") != "backup":
                continue

            # Parse creation time
            ctime = item.get("ctime", 0)
            ctime_dt = datetime.fromtimestamp(ctime) if ctime else datetime.now()

            backups.append(ProxmoxBackup(
                volid=item.get("volid", ""),
                vmid=item.get("vmid", 0),
                node=node,
                ctime=ctime_dt,
                size=item.get("size", 0),
                format=item.get("format", ""),
                notes=item.get("notes", ""),
                protected=item.get("protected", False),
            ))

        return sorted(backups, key=lambda b: b.ctime, reverse=True)

    def create_backup(
        self,
        node: str,
        vmid: int,
        storage: str,
        mode: ProxmoxBackupMode = ProxmoxBackupMode.SNAPSHOT,
        compress: ProxmoxCompression = ProxmoxCompression.ZSTD,
        notes_template: str | None = None,
        bwlimit: int | None = None,
        ionice: int | None = None,
        protected: bool = False,
        remove: bool = True,
        prune_backups: str | None = None,
    ) -> str:
        """Create a backup of a VM or container.

        Args:
            node: Node where the guest runs
            vmid: VM/container ID to backup
            storage: Storage ID for backup destination
            mode: Backup mode (snapshot, stop, suspend)
            compress: Compression algorithm
            notes_template: Notes template with variables like {{guestname}}
            bwlimit: Bandwidth limit in KiB/s (0 = unlimited)
            ionice: I/O priority (0-8)
            protected: Mark backup as protected
            remove: Apply retention policy after backup
            prune_backups: Retention policy (e.g., "keep-last=3,keep-daily=7")

        Returns:
            UPID of the backup task
        """
        endpoint = f"/nodes/{node}/vzdump"

        data: dict[str, Any] = {
            "vmid": vmid,
            "storage": storage,
            "mode": mode.value,
            "compress": compress.value,
        }

        if notes_template:
            data["notes-template"] = notes_template
        if bwlimit is not None:
            data["bwlimit"] = bwlimit
        if ionice is not None:
            data["ionice"] = ionice
        if protected:
            data["protected"] = 1
        if remove:
            data["remove"] = 1
        if prune_backups:
            data["prune-backups"] = prune_backups

        result = self._make_request("POST", endpoint, data=data)

        # Result is the UPID string
        if not result:
            raise ProxmoxAPIError(f"No UPID returned for backup task on VMID {vmid}")
        if isinstance(result, str):
            return result
        upid = result.get("upid")
        if not upid:
            raise ProxmoxAPIError(f"No UPID in response for backup task on VMID {vmid}")
        return upid

    def restore_guest(
        self,
        node: str,
        vmid: int,
        archive: str,
        guest_type: ProxmoxGuestType,
        storage: str | None = None,
        force: bool = False,
        unique: bool = False,
        start: bool = False,
        bwlimit: int | None = None,
    ) -> str:
        """Restore a VM or container from backup.

        Args:
            node: Target node for restore
            vmid: Target VM/container ID
            archive: Backup volume ID (e.g., "local:backup/vzdump-qemu-100-...")
            guest_type: QEMU or LXC
            storage: Target storage for disks (optional, uses original if not set)
            force: Overwrite existing guest with same ID
            unique: Assign unique random MAC addresses
            start: Start guest after restore
            bwlimit: Bandwidth limit in KiB/s

        Returns:
            UPID of the restore task
        """
        endpoint = f"/nodes/{node}/{guest_type.value}"

        data: dict[str, Any] = {
            "vmid": vmid,
        }

        if guest_type == ProxmoxGuestType.LXC:
            # LXC restore uses ostemplate parameter pointing to the backup archive
            data["ostemplate"] = archive
            data["restore"] = 1
        else:
            # QEMU uses archive parameter
            data["archive"] = archive

        if storage:
            data["storage"] = storage
        if force:
            data["force"] = 1
        if unique:
            data["unique"] = 1
        if start:
            data["start"] = 1
        if bwlimit is not None:
            data["bwlimit"] = bwlimit

        result = self._make_request("POST", endpoint, data=data)

        if not result:
            raise ProxmoxAPIError(f"No UPID returned for restore task on VMID {vmid}")
        if isinstance(result, str):
            return result
        upid = result.get("upid")
        if not upid:
            raise ProxmoxAPIError(f"No UPID in response for restore task on VMID {vmid}")
        return upid

    def delete_backup(self, node: str, storage: str, volid: str) -> str:
        """Delete a backup.

        Args:
            node: Node name
            storage: Storage ID
            volid: Volume ID of the backup

        Returns:
            UPID of the delete task
        """
        # Volume ID needs to be URL-encoded
        encoded_volid = urllib.parse.quote(volid, safe="")
        endpoint = f"/nodes/{node}/storage/{storage}/content/{encoded_volid}"

        result = self._make_request("DELETE", endpoint)

        if isinstance(result, str):
            return result
        return result.get("upid", result) if result else ""

    def download_backup(
        self,
        node: str,
        storage: str,
        volid: str,
        dest_path: str,
        progress_callback: Any | None = None,
    ) -> str:
        """Download a backup file from Proxmox storage.

        Uses the Proxmox download-url API to get a temporary download URL,
        then streams the file to the destination.

        Args:
            node: Node name
            storage: Storage ID
            volid: Volume ID of the backup (e.g., "local:backup/vzdump-qemu-100-...")
            dest_path: Local destination path for the downloaded file
            progress_callback: Optional callback(bytes_downloaded, total_bytes)

        Returns:
            Path to the downloaded file
        """
        # Note: Proxmox doesn't have a direct download API for backup files
        # The backup files need to be accessed via shared storage or SSH/SCP
        # For now, we'll raise an informative error
        raise ProxmoxAPIError(
            f"Direct download of backup {volid} is not supported via Proxmox API. "
            "Please ensure the Backer server has direct access to the Proxmox storage "
            "(e.g., via NFS mount or shared storage) or use PBS (Proxmox Backup Server)."
        )

    def get_backup_filename(self, volid: str) -> str | None:
        """Extract the filename from a volume ID.

        Args:
            volid: Volume ID (e.g., "local:backup/vzdump-qemu-100-...")

        Returns:
            Filename or None
        """
        # Parse volid to get the filename
        # Format: "storage:backup/filename" e.g., "local:backup/vzdump-qemu-100-..."
        if ":" in volid:
            _, path_part = volid.split(":", 1)
            if path_part.startswith("backup/"):
                return path_part[7:]  # Remove "backup/" prefix
        return None

    # =========================================================================
    # Task Operations
    # =========================================================================

    def get_task_status(self, node: str, upid: str) -> ProxmoxTaskStatus:
        """Get status of a task.

        Args:
            node: Node where task is running
            upid: Task UPID

        Returns:
            ProxmoxTaskStatus object
        """
        encoded_upid = urllib.parse.quote(upid, safe="")
        endpoint = f"/nodes/{node}/tasks/{encoded_upid}/status"

        data = self._make_request("GET", endpoint)
        if not data:
            raise ProxmoxAPIError(f"No status data returned for task {upid}")

        starttime = data.get("starttime")
        endtime = data.get("endtime")

        return ProxmoxTaskStatus(
            upid=upid,
            node=node,
            status=data.get("status", "unknown"),
            exitstatus=data.get("exitstatus", ""),
            type=data.get("type", ""),
            user=data.get("user", ""),
            starttime=datetime.fromtimestamp(starttime) if starttime else None,
            endtime=datetime.fromtimestamp(endtime) if endtime else None,
            pid=data.get("pid", 0),
        )

    def get_task_log(
        self,
        node: str,
        upid: str,
        start: int = 0,
        limit: int = 500,
    ) -> list[str]:
        """Get task log output.

        Args:
            node: Node where task is running
            upid: Task UPID
            start: Start line number
            limit: Maximum lines to return

        Returns:
            List of log lines
        """
        encoded_upid = urllib.parse.quote(upid, safe="")
        endpoint = f"/nodes/{node}/tasks/{encoded_upid}/log"

        data = self._make_request("GET", endpoint, params={
            "start": start,
            "limit": limit,
        })

        if isinstance(data, list):
            return [line.get("t", "") for line in data]
        return []

    def wait_for_task(
        self,
        node: str,
        upid: str,
        timeout: int = 3600,
        poll_interval: int = 5,
        progress_callback: Any | None = None,
    ) -> ProxmoxTaskStatus:
        """Wait for a task to complete.

        Args:
            node: Node where task is running
            upid: Task UPID
            timeout: Maximum wait time in seconds
            poll_interval: Seconds between status checks
            progress_callback: Optional callback(status, log_lines) for progress updates

        Returns:
            Final ProxmoxTaskStatus

        Raises:
            ProxmoxAPIError: If timeout exceeded
        """
        start_time = time.time()
        last_log_line = 0

        while True:
            status = self.get_task_status(node, upid)

            # Get new log lines for progress callback
            if progress_callback:
                log_lines = self.get_task_log(node, upid, start=last_log_line)
                if log_lines:
                    last_log_line += len(log_lines)
                    progress_callback(status, log_lines)

            if not status.is_running:
                return status

            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise ProxmoxAPIError(f"Task timeout after {timeout}s: {upid}")

            time.sleep(poll_interval)

    def stop_task(self, node: str, upid: str) -> None:
        """Stop a running task.

        Args:
            node: Node where task is running
            upid: Task UPID
        """
        encoded_upid = urllib.parse.quote(upid, safe="")
        endpoint = f"/nodes/{node}/tasks/{encoded_upid}"
        self._make_request("DELETE", endpoint)


class ProxmoxBackupManager:
    """High-level backup manager for Proxmox VE.

    Provides simplified backup/restore operations with progress tracking
    and error handling.
    """

    def __init__(self, api: ProxmoxAPI):
        """Initialize backup manager.

        Args:
            api: Configured ProxmoxAPI instance
        """
        self.api = api

    def backup_guest(
        self,
        vmid: int,
        storage: str,
        node: str | None = None,
        mode: ProxmoxBackupMode = ProxmoxBackupMode.SNAPSHOT,
        compress: ProxmoxCompression = ProxmoxCompression.ZSTD,
        retention: dict[str, int] | None = None,
        progress_callback: Any | None = None,
        timeout: int = 7200,
    ) -> dict[str, Any]:
        """Backup a VM or container with progress tracking.

        Args:
            vmid: VM/container ID to backup
            storage: Target storage for backup
            node: Node name (auto-detected if None)
            mode: Backup mode
            compress: Compression algorithm
            retention: Retention policy dict (e.g., {"keep_last": 3, "keep_daily": 7})
            progress_callback: Optional callback for progress updates
            timeout: Maximum backup time in seconds

        Returns:
            Dict with backup result info
        """
        # Find guest if node not specified
        if not node:
            guests = self.api.list_guests()
            guest = next((g for g in guests if g.vmid == vmid), None)
            if not guest:
                raise ProxmoxAPIError(f"Guest {vmid} not found")
            node = guest.node

        # Build retention string
        prune_backups = None
        if retention:
            parts = []
            if "keep_last" in retention:
                parts.append(f"keep-last={retention['keep_last']}")
            if "keep_daily" in retention:
                parts.append(f"keep-daily={retention['keep_daily']}")
            if "keep_weekly" in retention:
                parts.append(f"keep-weekly={retention['keep_weekly']}")
            if "keep_monthly" in retention:
                parts.append(f"keep-monthly={retention['keep_monthly']}")
            if "keep_yearly" in retention:
                parts.append(f"keep-yearly={retention['keep_yearly']}")
            if parts:
                prune_backups = ",".join(parts)

        # Start backup
        started_at = datetime.now()
        notes_template = "{{guestname}} - Backup by Backer"

        logger.info(f"Starting backup of VMID {vmid} on {node} to {storage}")

        upid = self.api.create_backup(
            node=node,
            vmid=vmid,
            storage=storage,
            mode=mode,
            compress=compress,
            notes_template=notes_template,
            prune_backups=prune_backups,
        )

        logger.info(f"Backup task started: {upid}")

        # Wait for completion
        final_status = self.api.wait_for_task(
            node=node,
            upid=upid,
            timeout=timeout,
            progress_callback=progress_callback,
        )

        finished_at = datetime.now()
        duration = (finished_at - started_at).total_seconds()

        result = {
            "success": final_status.is_success,
            "vmid": vmid,
            "node": node,
            "storage": storage,
            "upid": upid,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": duration,
            "exit_status": final_status.exitstatus,
        }

        if not final_status.is_success:
            # Get error from log
            log_lines = self.api.get_task_log(node, upid)
            error_lines = [
                line for line in log_lines
                if "error" in line.lower() or "failed" in line.lower()
            ]
            result["errors"] = error_lines or [final_status.exitstatus]

        return result

    def backup_to_storage(
        self,
        vmid: int,
        storage: str,
        node: str | None = None,
        mode: ProxmoxBackupMode = ProxmoxBackupMode.SNAPSHOT,
        compress: ProxmoxCompression = ProxmoxCompression.ZSTD,
        progress_callback: Any | None = None,
        timeout: int = 7200,
    ) -> dict[str, Any]:
        """Backup a VM/container to a Proxmox storage.

        This is a simple wrapper around backup_guest that finds the backup
        file info after completion.

        Args:
            vmid: VM/container ID to backup
            storage: Proxmox storage ID to save backup (must be configured in Proxmox)
            node: Node name (auto-detected if None)
            mode: Backup mode
            compress: Compression algorithm
            progress_callback: Optional callback for progress updates
            timeout: Maximum backup time in seconds

        Returns:
            Dict with backup result info including volid and file path
        """
        # Find guest if node not specified
        if not node:
            guests = self.api.list_guests()
            guest = next((g for g in guests if g.vmid == vmid), None)
            if not guest:
                raise ProxmoxAPIError(f"Guest {vmid} not found")
            node = guest.node

        logger.info(f"Backup VMID {vmid} on {node} to storage {storage}")

        # Create backup on Proxmox storage
        backup_result = self.backup_guest(
            vmid=vmid,
            storage=storage,
            node=node,
            mode=mode,
            compress=compress,
            progress_callback=progress_callback,
            timeout=timeout,
        )

        if not backup_result.get("success"):
            return backup_result

        # Find the created backup
        backups = self.api.list_backups(node, storage, vmid=vmid)
        if backups:
            # Get the most recent backup (should be the one we just created)
            created_backup = backups[0]
            backup_result["volid"] = created_backup.volid
            backup_result["backup_size"] = created_backup.size
            backup_result["backup_filename"] = self.api.get_backup_filename(created_backup.volid)
            logger.info(f"Backup created: {created_backup.volid} ({created_backup.size} bytes)")

        return backup_result

    def restore_guest(
        self,
        vmid: int,
        archive: str,
        node: str,
        guest_type: ProxmoxGuestType,
        target_vmid: int | None = None,
        storage: str | None = None,
        force: bool = False,
        start_after: bool = False,
        progress_callback: Any | None = None,
        timeout: int = 7200,
    ) -> dict[str, Any]:
        """Restore a VM or container from backup.

        Args:
            vmid: Original VM/container ID (for finding backup)
            archive: Backup volume ID
            node: Target node for restore
            guest_type: QEMU or LXC
            target_vmid: Target VM ID (defaults to original)
            storage: Target storage for disks
            force: Overwrite existing guest
            start_after: Start guest after restore
            progress_callback: Optional callback for progress updates
            timeout: Maximum restore time in seconds

        Returns:
            Dict with restore result info
        """
        target_vmid = target_vmid or vmid

        started_at = datetime.now()

        logger.info(f"Starting restore of {archive} to VMID {target_vmid} on {node}")

        upid = self.api.restore_guest(
            node=node,
            vmid=target_vmid,
            archive=archive,
            guest_type=guest_type,
            storage=storage,
            force=force,
            start=start_after,
        )

        logger.info(f"Restore task started: {upid}")

        # Wait for completion
        final_status = self.api.wait_for_task(
            node=node,
            upid=upid,
            timeout=timeout,
            progress_callback=progress_callback,
        )

        finished_at = datetime.now()
        duration = (finished_at - started_at).total_seconds()

        result = {
            "success": final_status.is_success,
            "vmid": target_vmid,
            "node": node,
            "archive": archive,
            "upid": upid,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": duration,
            "exit_status": final_status.exitstatus,
        }

        if not final_status.is_success:
            log_lines = self.api.get_task_log(node, upid)
            error_lines = [
                line for line in log_lines
                if "error" in line.lower() or "failed" in line.lower()
            ]
            result["errors"] = error_lines or [final_status.exitstatus]

        return result

    def get_backup_schedule_info(self, vmid: int) -> dict[str, Any]:
        """Get backup information for a guest.

        Args:
            vmid: VM/container ID

        Returns:
            Dict with backup info (latest backup, count, total size)
        """
        guests = self.api.list_guests()
        guest = next((g for g in guests if g.vmid == vmid), None)

        if not guest:
            return {"error": f"Guest {vmid} not found"}

        # Find backups across all storage
        all_backups = []
        storages = self.api.list_storages(guest.node)

        for storage in storages:
            try:
                backups = self.api.list_backups(guest.node, storage.storage, vmid=vmid)
                all_backups.extend(backups)
            except ProxmoxAPIError:
                pass

        total_size = sum(b.size for b in all_backups)
        latest = all_backups[0] if all_backups else None

        return {
            "vmid": vmid,
            "name": guest.name,
            "node": guest.node,
            "backup_count": len(all_backups),
            "total_size_bytes": total_size,
            "latest_backup": {
                "volid": latest.volid,
                "ctime": latest.ctime.isoformat(),
                "size": latest.size,
            } if latest else None,
        }

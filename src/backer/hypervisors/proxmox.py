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

    def list_guests_cluster(self) -> list[ProxmoxGuest]:
        """List all VMs and containers using the cluster resources endpoint.

        This is more efficient for clusters as it uses a single API call
        instead of querying each node separately. Use this method when
        you need to list guests across an entire cluster.

        Returns:
            List of ProxmoxGuest objects
        """
        try:
            # Use cluster/resources endpoint for efficiency
            data = self._make_request("GET", "/cluster/resources", params={"type": "vm"}) or []

            guests = []
            for item in data:
                # Determine guest type from resource type
                resource_type = item.get("type", "")
                if resource_type == "qemu":
                    guest_type = ProxmoxGuestType.QEMU
                elif resource_type == "lxc":
                    guest_type = ProxmoxGuestType.LXC
                else:
                    continue  # Skip non-VM/CT resources

                guests.append(ProxmoxGuest(
                    vmid=item.get("vmid", 0),
                    name=item.get("name", f"{resource_type.upper()} {item.get('vmid')}"),
                    node=item.get("node", ""),
                    guest_type=guest_type,
                    status=item.get("status", "unknown"),
                    cpus=item.get("maxcpu", 0),
                    maxmem=item.get("maxmem", 0),
                    maxdisk=item.get("maxdisk", 0),
                    uptime=item.get("uptime", 0),
                    template=item.get("template", 0) == 1,
                    tags=item.get("tags", "").split(";") if item.get("tags") else [],
                ))

            return sorted(guests, key=lambda g: g.vmid)

        except ProxmoxAPIError as e:
            # Fall back to per-node query if cluster resources fails
            # (e.g., insufficient permissions)
            logger.warning(f"Cluster resources query failed, falling back to per-node: {e}")
            return self.list_guests()

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

    def find_guest(self, vmid: int) -> ProxmoxGuest | None:
        """Find a guest by VMID across the cluster.

        Uses the cluster/resources endpoint for efficiency. This is the
        recommended way to find a guest when you don't know which node it's on,
        especially in cluster environments where VMs may migrate.

        Args:
            vmid: VM/container ID to find

        Returns:
            ProxmoxGuest or None if not found
        """
        try:
            # Query cluster resources for this specific VMID
            data = self._make_request("GET", "/cluster/resources", params={"type": "vm"}) or []

            for item in data:
                if item.get("vmid") == vmid:
                    resource_type = item.get("type", "")
                    if resource_type == "qemu":
                        guest_type = ProxmoxGuestType.QEMU
                    elif resource_type == "lxc":
                        guest_type = ProxmoxGuestType.LXC
                    else:
                        continue

                    return ProxmoxGuest(
                        vmid=vmid,
                        name=item.get("name", f"{resource_type.upper()} {vmid}"),
                        node=item.get("node", ""),
                        guest_type=guest_type,
                        status=item.get("status", "unknown"),
                        cpus=item.get("maxcpu", 0),
                        maxmem=item.get("maxmem", 0),
                        maxdisk=item.get("maxdisk", 0),
                        uptime=item.get("uptime", 0),
                        template=item.get("template", 0) == 1,
                        tags=item.get("tags", "").split(";") if item.get("tags") else [],
                    )

            return None

        except ProxmoxAPIError as e:
            # Fall back to per-node search
            logger.debug(f"Cluster resource query failed, searching nodes: {e}")
            guests = self.list_guests()
            return next((g for g in guests if g.vmid == vmid), None)

    def stop_guest(
        self,
        node: str,
        vmid: int,
        guest_type: ProxmoxGuestType,
        timeout: int = 60,
    ) -> str:
        """Stop a VM or container immediately (hard stop).

        Args:
            node: Node where the guest is running
            vmid: VM/container ID
            guest_type: QEMU or LXC
            timeout: Timeout in seconds

        Returns:
            UPID of the stop task
        """
        endpoint = f"/nodes/{node}/{guest_type.value}/{vmid}/status/stop"
        data = {"timeout": timeout}

        result = self._make_request("POST", endpoint, data=data)
        upid = result.get("upid") if isinstance(result, dict) else result
        if not upid:
            raise ProxmoxAPIError(f"No UPID in response for stop task on VMID {vmid}")
        return upid

    def shutdown_guest(
        self,
        node: str,
        vmid: int,
        guest_type: ProxmoxGuestType,
        timeout: int = 180,
        force_stop: bool = False,
    ) -> str:
        """Gracefully shutdown a VM or container.

        Args:
            node: Node where the guest is running
            vmid: VM/container ID
            guest_type: QEMU or LXC
            timeout: Timeout in seconds for graceful shutdown
            force_stop: Force stop if graceful shutdown fails

        Returns:
            UPID of the shutdown task
        """
        endpoint = f"/nodes/{node}/{guest_type.value}/{vmid}/status/shutdown"
        data: dict[str, Any] = {"timeout": timeout}
        if force_stop:
            data["forceStop"] = 1

        result = self._make_request("POST", endpoint, data=data)
        upid = result.get("upid") if isinstance(result, dict) else result
        if not upid:
            raise ProxmoxAPIError(f"No UPID in response for shutdown task on VMID {vmid}")
        return upid

    def get_guest_status(
        self,
        node: str,
        vmid: int,
        guest_type: ProxmoxGuestType,
    ) -> str:
        """Get the current status of a VM or container.

        Args:
            node: Node where the guest is located
            vmid: VM/container ID
            guest_type: QEMU or LXC

        Returns:
            Status string: "running", "stopped", "paused", etc.
        """
        endpoint = f"/nodes/{node}/{guest_type.value}/{vmid}/status/current"
        result = self._make_request("GET", endpoint)
        return result.get("status", "unknown") if isinstance(result, dict) else "unknown"

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
                node = nodes[0].node  # ProxmoxNode is a dataclass, access .node attribute

            # Query storage status on the node
            data = self._make_request("GET", f"/nodes/{node}/storage/{storage_id}/status")
            return data
        except ProxmoxAPIError as e:
            # Log the actual error for debugging
            logger.debug(f"Storage status check failed: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error checking storage status: {e}")
            return None

    def get_storage_status_all_nodes(self, storage_id: str) -> dict[str, dict[str, Any] | None]:
        """Get storage status on all cluster nodes.

        Useful for cluster environments to verify storage is mounted everywhere.

        Args:
            storage_id: Storage identifier

        Returns:
            Dict mapping node name to storage status (or None if not available on that node)
        """
        result = {}
        try:
            nodes = self.list_nodes()
            for pve_node in nodes:
                if pve_node.status == "offline":
                    result[pve_node.node] = None
                    continue
                try:
                    data = self._make_request(
                        "GET", f"/nodes/{pve_node.node}/storage/{storage_id}/status"
                    )
                    result[pve_node.node] = data
                except ProxmoxAPIError:
                    result[pve_node.node] = None
        except Exception as e:
            logger.debug(f"Error checking storage status on all nodes: {e}")
        return result

    def is_storage_active_on_any_node(self, storage_id: str) -> tuple[bool, str | None]:
        """Check if storage is active on at least one node.

        Args:
            storage_id: Storage identifier

        Returns:
            Tuple of (is_active, node_name) - node_name is the first node where storage is active
        """
        try:
            nodes = self.list_nodes()
            for pve_node in nodes:
                if pve_node.status == "offline":
                    continue
                try:
                    data = self._make_request(
                        "GET", f"/nodes/{pve_node.node}/storage/{storage_id}/status"
                    )
                    if data and data.get("active"):
                        return True, pve_node.node
                except ProxmoxAPIError:
                    continue
            return False, None
        except Exception as e:
            logger.debug(f"Error checking storage status: {e}")
            return False, None

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

        Raises:
            ProxmoxAPIError: If unable to create or verify directory
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
        last_error = None
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
                    logger.info(f"Created SMB directory: //{server}/{share}/{partial_path}")
                else:
                    # Check if it's just "already exists" error
                    stderr = result.stderr.decode() if result.stderr else ""
                    if "NT_STATUS_OBJECT_NAME_COLLISION" in stderr:
                        logger.debug(f"SMB directory already exists: //{server}/{share}/{partial_path}")
                    elif "NT_STATUS_LOGON_FAILURE" in stderr:
                        raise ProxmoxAPIError(
                            f"SMB authentication failed for //{server}/{share}. "
                            "Check username, password, and domain settings."
                        )
                    elif "NT_STATUS_BAD_NETWORK_NAME" in stderr:
                        raise ProxmoxAPIError(
                            f"SMB share not found: //{server}/{share}. "
                            "Check the share name is correct."
                        )
                    elif "NT_STATUS_HOST_UNREACHABLE" in stderr or "NT_STATUS_CONNECTION_REFUSED" in stderr:
                        raise ProxmoxAPIError(
                            f"Cannot connect to SMB server {server}. "
                            "Check network connectivity and firewall settings."
                        )
                    else:
                        logger.warning(f"smbclient mkdir {partial_path}: {stderr}")
                        last_error = stderr
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout creating SMB directory: {partial_path}")
                last_error = "timeout"
            except ProxmoxAPIError:
                raise
            except Exception as e:
                logger.warning(f"Error creating SMB directory {partial_path}: {e}")
                last_error = str(e)

        # Verify the final directory exists by listing it
        verify_cmd = [
            "smbclient",
            f"//{server}/{share}",
            *auth_parts,
            "-c", f"cd {path}",
        ]
        try:
            result = subprocess.run(verify_cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                stderr = result.stderr.decode() if result.stderr else ""
                if "NT_STATUS_OBJECT_NAME_NOT_FOUND" in stderr or "NT_STATUS_OBJECT_PATH_NOT_FOUND" in stderr:
                    raise ProxmoxAPIError(
                        f"Failed to create SMB directory //{server}/{share}/{path}. "
                        f"Last error: {last_error or stderr}"
                    )
        except ProxmoxAPIError:
            raise
        except Exception as e:
            logger.warning(f"Could not verify SMB directory: {e}")

        logger.info(f"Ensured SMB directory exists: //{server}/{share}/{path}")

        # Add a delay to allow the NAS to fully commit the directory
        # This helps with race conditions where Proxmox CIFS mount doesn't
        # see the newly created directory immediately. TrueNAS and other NAS
        # devices may have slight delays in making new directories visible
        # to other clients. Increased from 2s to 5s for better reliability.
        import time
        time.sleep(5)

    def _ensure_nfs_directory_exists(
        self,
        server: str,
        base_export: str,
        subdir: str,
    ) -> None:
        """Ensure a directory exists on an NFS export by mounting and creating it.

        NFS doesn't have a remote directory creation protocol like SMB's smbclient,
        so we need to temporarily mount the base export and create the directory.

        Args:
            server: NFS server hostname/IP
            base_export: Base NFS export path (e.g., /mnt/tank/Backups)
            subdir: Subdirectory to create within the export (e.g., Hypervisors/MyProxmox)

        Raises:
            ProxmoxAPIError: If unable to mount NFS or create directory
        """
        import os
        import subprocess
        import tempfile

        # Create a temporary mount point
        mount_point = tempfile.mkdtemp(prefix="backer_nfs_mkdir_")

        try:
            # Mount the NFS export
            # Use soft mount with timeout to avoid hangs
            mount_cmd = [
                "sudo", "-n", "mount", "-t", "nfs",
                "-o", "soft,timeo=50,retrans=2",
                f"{server}:{base_export}",
                mount_point,
            ]

            result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                error_msg = result.stderr.strip()
                raise ProxmoxAPIError(
                    f"Failed to mount NFS {server}:{base_export} for directory creation: {error_msg}"
                )

            # Create the subdirectory structure
            full_path = os.path.join(mount_point, subdir)
            try:
                os.makedirs(full_path, exist_ok=True)
                logger.info(f"Created NFS directory: {server}:{base_export}/{subdir}")
            except OSError as e:
                raise ProxmoxAPIError(
                    f"Failed to create directory {subdir} on NFS {server}:{base_export}: {e}"
                )

        except subprocess.TimeoutExpired:
            raise ProxmoxAPIError(
                f"Timeout mounting NFS {server}:{base_export}"
            )
        finally:
            # Always try to unmount
            try:
                subprocess.run(
                    ["sudo", "-n", "umount", mount_point],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                # Try lazy unmount if normal unmount fails
                try:
                    subprocess.run(
                        ["sudo", "-n", "umount", "-l", mount_point],
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    pass

            # Remove the temp mount point
            try:
                os.rmdir(mount_point)
            except Exception:
                pass

        # Small delay for NFS directory visibility
        import time
        time.sleep(1)

    def _cleanup_stale_mount_point(
        self,
        storage_id: str,
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_key: str | None = None,
        ssh_password: str | None = None,
    ) -> bool:
        """Remove a stale mount point directory on the Proxmox host via SSH.

        When Proxmox storage is deleted but the mount point directory remains,
        subsequent storage creation fails. This cleans up the stale directory.

        This handles several cases:
        - Empty directory: rmdir works directly
        - Still mounted: umount first, then rmdir
        - Stale/corrupted mount: force umount with lazy flag, then rmdir
        - Transport endpoint not connected: umount -l, then rmdir

        Args:
            storage_id: The storage ID (mount point is at /mnt/pve/{storage_id})
            ssh_user: SSH username (default: root)
            ssh_port: SSH port (default: 22)
            ssh_key: Path to SSH private key file
            ssh_password: SSH password (used if no key)

        Returns:
            True if cleanup succeeded, False otherwise
        """
        import subprocess
        import time as time_module

        mount_point = f"/mnt/pve/{storage_id}"

        # Check if we have any SSH credentials
        if not ssh_key and not ssh_password:
            logger.debug(
                f"No SSH credentials configured - cannot clean up mount point {mount_point}. "
                "Configure SSH key or password on the hypervisor for automatic cleanup."
            )
            return False

        logger.info(f"Attempting to clean up stale mount point: {mount_point}")

        # Build SSH command with connection timeout
        # Note: BatchMode=yes disables password prompts, so we only use it with key auth
        # For password auth via sshpass, we need to allow password prompts
        use_password = ssh_password and not ssh_key
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
        ]

        # Only use BatchMode when not using password auth
        # BatchMode=yes disables password prompts which breaks sshpass
        if not use_password:
            ssh_cmd.extend(["-o", "BatchMode=yes"])

        if ssh_key:
            ssh_cmd.extend(["-i", ssh_key])
        if ssh_port != 22:
            ssh_cmd.extend(["-p", str(ssh_port)])

        ssh_cmd.append(f"{ssh_user}@{self.host}")

        def run_ssh(cmd_str: str, timeout: int = 15) -> tuple[int, str, str]:
            """Run SSH command and return (returncode, stdout, stderr)."""
            full_ssh = ssh_cmd + [cmd_str]
            if use_password:
                full_ssh = ["sshpass", "-p", ssh_password] + full_ssh
            result = subprocess.run(full_ssh, capture_output=True, timeout=timeout, text=True)
            return (
                result.returncode,
                result.stdout or "",
                result.stderr or "",
            )

        try:
            # Step 1: Check if mount point directory exists using multiple methods
            # The directory can exist in several states:
            # - Normal directory (empty or with contents)
            # - Mounted filesystem (active CIFS/NFS mount)
            # - Stale mount (transport endpoint disconnected, hangs on access)
            # - Empty directory left after unmount
            #
            # We use ls -la on the parent directory which won't hang even if
            # the mount point itself is stale
            rc, stdout, stderr = run_ssh(
                f"ls -la /mnt/pve/ 2>/dev/null | grep -E '^d.*{storage_id}$' && echo PATH_EXISTS || echo PATH_MISSING",
                timeout=10
            )

            # If SSH itself fails, we can't proceed
            if "Permission denied" in stderr or "Connection refused" in stderr:
                logger.warning(f"SSH connection failed: {stderr.strip()}")
                return False

            path_exists = "PATH_EXISTS" in stdout

            # If directory doesn't exist in parent listing, nothing to clean up
            if not path_exists:
                logger.debug(f"Mount point {mount_point} does not exist - nothing to clean up")
                return True

            logger.info(f"Found existing mount point {mount_point}, attempting cleanup...")

            # Step 2: Always try lazy unmount first - this handles stale mounts
            # that would cause mountpoint command to hang
            logger.info(f"Attempting lazy unmount of {mount_point}...")
            run_ssh(f"umount -l {mount_point} 2>/dev/null || true", timeout=10)

            # Small delay to let unmount take effect
            time_module.sleep(0.5)

            # Step 3: Try to remove the directory
            rc, _, stderr = run_ssh(f"rmdir {mount_point} 2>&1")
            if rc == 0:
                logger.info(f"Successfully removed stale mount point: {mount_point}")
                return True

            # Step 4: If rmdir failed due to "not empty", check what's there
            if "not empty" in stderr.lower() or "Directory not empty" in stderr:
                logger.warning("Mount point not empty, checking contents...")
                rc, stdout, _ = run_ssh(f"ls -A {mount_point} 2>/dev/null | head -5")

                # If empty or only contains expected backup dirs, force remove
                contents = stdout.strip()
                if not contents or contents in ("dump", ".backer"):
                    logger.info("Mount point contains only backup data, force removing...")
                    rc, _, _ = run_ssh(f"rm -rf {mount_point}")
                    if rc == 0:
                        logger.info("Force removed mount point with contents")
                        return True
                else:
                    logger.warning(f"Mount point has unexpected contents: {contents[:100]}")

            # Step 5: Handle "Transport endpoint is not connected"
            if "Transport endpoint" in stderr or "Stale file handle" in stderr:
                logger.warning("Stale mount detected, forcing lazy unmount...")
                run_ssh(f"umount -l -f {mount_point} 2>/dev/null || true")
                time_module.sleep(1)
                rc, _, stderr = run_ssh(f"rmdir {mount_point} 2>&1")
                if rc == 0:
                    logger.info("Successfully removed stale mount point after force unmount")
                    return True

            # Step 6: Last resort - verify directory state
            rc, _, _ = run_ssh(f"test -d {mount_point}")
            if rc != 0:
                logger.info(f"Mount point {mount_point} no longer exists")
                return True

            logger.warning(f"Failed to clean up mount point {mount_point}: {stderr.strip()}")
            return False

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout cleaning up mount point {mount_point} - mount may be stuck")
            return False
        except FileNotFoundError as e:
            if "sshpass" in str(e):
                logger.warning(
                    "sshpass not installed - cannot use password auth for SSH cleanup. "
                    "Install sshpass or configure SSH key authentication."
                )
            else:
                logger.warning(f"SSH command not found: {e}")
            return False
        except Exception as e:
            logger.warning(f"Error cleaning up mount point {mount_point}: {e}")
            return False

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
            storage_id: Unique storage identifier (e.g., "backer-nfs-myrepo")
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
        smbversion: str | None = None,
        disable: bool = False,
        max_protected_backups: int = 5,
    ) -> dict[str, Any]:
        """Create a CIFS/SMB storage in Proxmox.

        Args:
            storage_id: Unique storage identifier (e.g., "backer-nfs-myrepo")
            server: SMB server hostname or IP
            share: SMB share name
            username: SMB username (optional for guest access)
            password: SMB password
            domain: SMB domain (optional)
            content: Content types (default: ["backup"])
            nodes: List of nodes to enable storage on (None = all)
            subdir: Subdirectory within the share
            smbversion: SMB protocol version (2.0, 2.1, 3, 3.0, 3.11, or default)
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
        if smbversion:
            data["smbversion"] = smbversion
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
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_key: str | None = None,
        ssh_password: str | None = None,
        smbversion: str | None = None,
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
            ssh_user: SSH username for cleanup operations (default: root)
            ssh_port: SSH port (default: 22)
            ssh_key: Path to SSH private key
            ssh_password: SSH password (used if no key provided)
            storage_id: Override storage ID (default: "backer-{type}-{repo_name}")
            smbversion: SMB protocol version (2.0, 2.1, 3, 3.0, 3.11, or default)
                       If not specified, defaults to "3.0" for better NAS compatibility.

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
            # Include storage type prefix to avoid collision between NFS and SMB repos
            # with the same name pointing to the same physical storage
            type_prefix = "cifs" if repo_type == "smb" else repo_type
            storage_id = f"backer-{type_prefix}-{safe_name}"
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
            # Verify the storage type matches (nfs vs cifs)
            existing_type = existing.get("type", "").lower()
            expected_type = "cifs" if repo_type == "smb" else repo_type
            if existing_type and existing_type != expected_type:
                raise ProxmoxAPIError(
                    f"Storage '{storage_id}' already exists with type '{existing_type}' "
                    f"but repository requires type '{expected_type}'. "
                    "Delete the existing storage or use a different repository name."
                )

            # Verify the storage points to the same server
            existing_server = existing.get("server", "")
            if existing_server and existing_server != server:
                logger.warning(
                    f"Proxmox storage '{storage_id}' exists but points to different server "
                    f"({existing_server} vs {server}). Using existing storage."
                )
            else:
                logger.info(f"Proxmox storage '{storage_id}' already exists")

            # Still need to wait for it to be active/mounted on at least one node
            max_wait = 30  # seconds
            poll_interval = 2  # seconds
            waited = 0

            while waited < max_wait:
                is_active, active_node = self.is_storage_active_on_any_node(storage_id)
                if is_active:
                    logger.info(f"Storage '{storage_id}' is active on node '{active_node}'")
                    return storage_id
                logger.debug(f"Waiting for existing storage '{storage_id}' to become active...")
                time.sleep(poll_interval)
                waited += poll_interval

            logger.warning(
                f"Existing storage '{storage_id}' not active on any node after {max_wait}s. "
                "Backup may fail if mount is not ready."
            )
            return storage_id

        # Storage doesn't exist - but there might be a stale mount point from a previous
        # failed attempt or after storage deletion. Proactively clean it up before creating.
        logger.info(f"Proactively checking for stale mount point at /mnt/pve/{storage_id}")
        self._cleanup_stale_mount_point(
            storage_id=storage_id,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            ssh_key=ssh_key,
            ssh_password=ssh_password,
        )

        # Create storage based on repository type
        if repo_type == "nfs":
            base_export = repository.get("share") or repository.get("path", "")
            if not base_export:
                raise ProxmoxAPIError(f"NFS repository '{repo_name}' has no export path")

            # NFS storage in Proxmox mounts the exact export path from the NFS server.
            # Unlike CIFS, NFS does not support mounting subdirectories within an export.
            # We use the base export path - vzdump creates dump/ automatically.
            # Hypervisors/{name}/ folder structure is used for metadata only.
            export = base_export

            # Pre-create the Hypervisors/{name} directory for metadata storage
            # The actual backups will go to dump/ at the export root
            if hypervisor_name:
                safe_hv_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in hypervisor_name)
                self._ensure_nfs_directory_exists(
                    server=server,
                    base_export=base_export,
                    subdir=f"Hypervisors/{safe_hv_name}",
                )

            logger.info(f"Creating NFS storage '{storage_id}' -> {server}:{export}")
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
            # Note: Proxmox requires subdir to be an absolute path (with leading slash)
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

            # Use provided SMB version or default to 3.0 for modern NAS compatibility
            # SMB 3.0 is widely supported by TrueNAS, Synology, QNAP, and other modern NAS
            effective_smbversion = smbversion or repository.get("smb_version") or "3.0"

            logger.info(
                f"Creating CIFS storage '{storage_id}' -> //{server}/{share} "
                f"(user={username or 'guest'}, domain={domain or 'none'}, "
                f"subdir={subdir or 'none'}, smbversion={effective_smbversion})"
            )

            # Retry logic for storage creation - handles race condition where
            # newly created SMB directories aren't immediately visible to Proxmox mount
            max_retries = 3
            retry_delay = 2  # seconds

            for attempt in range(max_retries):
                try:
                    self.create_cifs_storage(
                        storage_id=storage_id,
                        server=server,
                        share=share,
                        username=username,
                        password=password,
                        domain=domain,
                        subdir=subdir,
                        smbversion=effective_smbversion,
                    )
                    break  # Success
                except ProxmoxAPIError as e:
                    error_str = str(e)
                    error_lower = error_str.lower()

                    # Log the actual Proxmox error for debugging
                    logger.error(f"Proxmox storage creation error: {error_str}")

                    # Handle "directory does not exist" or "unreachable" - may be stale mount point
                    if "does not exist" in error_lower or "unreachable" in error_lower:
                        if attempt < max_retries - 1:
                            logger.warning(
                                f"Storage activation failed (attempt {attempt + 1}/{max_retries}): "
                                f"{error_str}. Trying cleanup and retry in {retry_delay}s..."
                            )
                            # Try to clean up stale mount point - this is often the root cause
                            self._cleanup_stale_mount_point(
                                storage_id=storage_id,
                                ssh_user=ssh_user,
                                ssh_port=ssh_port,
                                ssh_key=ssh_key,
                                ssh_password=ssh_password,
                            )
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                            continue
                        else:
                            # Build a helpful error message
                            hint_msg = (
                                f"Failed to activate storage '{storage_id}' after {max_retries} attempts.\n"
                                f"Proxmox returned: {error_str}\n\n"
                                f"Storage config: server={server}, share={share}, subdir={subdir}, "
                                f"smbversion={effective_smbversion}\n\n"
                                f"Possible causes:\n"
                                f"1. Proxmox host ({self.host}) cannot reach SMB server ({server})\n"
                                f"2. The subdir '{subdir}' does not exist on the share\n"
                                f"3. SMB credentials are incorrect or insufficient permissions\n"
                                f"4. SMB version mismatch - try a different version\n"
                                f"5. Stale mount point exists at /mnt/pve/{storage_id}\n\n"
                                f"To fix stale mount:\n"
                                f"  ssh root@{self.host}\n"
                                f"  umount -l /mnt/pve/{storage_id}\n"
                                f"  rmdir /mnt/pve/{storage_id}\n\n"
                                f"To test SMB from Proxmox:\n"
                                f"  ssh root@{self.host}\n"
                                f"  smbclient //{server}/{share} -U {username or 'guest'} "
                                f"-m SMB{effective_smbversion.replace('.', '')} -c 'cd {subdir}'"
                            )
                            raise ProxmoxAPIError(hint_msg) from e

                    # Handle "mkdir: File exists" error - stale mount point from previous run
                    elif "file exists" in error_lower or "already exists" in error_lower:
                        logger.warning(
                            f"Storage creation failed due to stale mount point. "
                            f"Attempting to clean up /mnt/pve/{storage_id}..."
                        )
                        # Check if storage exists now (race condition or partial creation)
                        check_storage = self.get_storage(storage_id)
                        if check_storage:
                            logger.info(f"Storage '{storage_id}' exists despite error, continuing...")
                            break
                        else:
                            # Mount point exists but storage doesn't - try to clean up via SSH
                            cleanup_success = self._cleanup_stale_mount_point(
                                storage_id=storage_id,
                                ssh_user=ssh_user,
                                ssh_port=ssh_port,
                                ssh_key=ssh_key,
                                ssh_password=ssh_password,
                            )
                            if cleanup_success:
                                # Retry storage creation
                                logger.info("Retrying storage creation after cleanup...")
                                self.create_cifs_storage(
                                    storage_id=storage_id,
                                    server=server,
                                    share=share,
                                    username=username,
                                    password=password,
                                    domain=domain,
                                    subdir=subdir,
                                    smbversion=effective_smbversion,
                                )
                                break
                            else:
                                raise ProxmoxAPIError(
                                    f"Failed to create storage '{storage_id}': stale mount point exists at "
                                    f"/mnt/pve/{storage_id} and automatic cleanup failed. "
                                    f"Please remove it manually on the Proxmox host: "
                                    f"sudo rmdir /mnt/pve/{storage_id}"
                                ) from e
                    else:
                        raise

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

        # Wait for storage to be mounted and active on at least one node
        # In a cluster, storage mounts asynchronously on each node
        max_wait = 30  # seconds
        poll_interval = 2  # seconds
        waited = 0

        while waited < max_wait:
            # Check if storage is active on any cluster node
            is_active, active_node = self.is_storage_active_on_any_node(storage_id)
            if is_active:
                logger.info(f"Storage '{storage_id}' is active on node '{active_node}'")
                break
            logger.info(f"Waiting for storage '{storage_id}' to become active (waited {waited}s)...")
            time.sleep(poll_interval)
            waited += poll_interval
        else:
            logger.warning(
                f"Storage '{storage_id}' not active on any node after {max_wait}s. "
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

    def list_backups_all_nodes(
        self,
        storage: str,
        vmid: int | None = None,
    ) -> list[ProxmoxBackup]:
        """List backups from a storage across all cluster nodes.

        In a cluster, shared storage content is accessible from any node,
        but we query from the first available node. For non-shared storage,
        this aggregates backups from all nodes.

        Args:
            storage: Storage ID
            vmid: Optional VM ID to filter by

        Returns:
            List of ProxmoxBackup objects sorted by creation time (newest first)
        """
        all_backups: list[ProxmoxBackup] = []
        seen_volids: set[str] = set()  # Deduplicate for shared storage

        try:
            nodes = self.list_nodes()
            for pve_node in nodes:
                if pve_node.status == "offline":
                    continue
                try:
                    backups = self.list_backups(pve_node.node, storage, vmid)
                    for backup in backups:
                        # Deduplicate - shared storage shows same backups on all nodes
                        if backup.volid not in seen_volids:
                            seen_volids.add(backup.volid)
                            all_backups.append(backup)
                except ProxmoxAPIError as e:
                    # Storage might not be available on this node
                    logger.debug(f"Could not list backups on {pve_node.node}: {e}")
                    continue
        except Exception as e:
            logger.warning(f"Error listing backups across cluster: {e}")

        return sorted(all_backups, key=lambda b: b.ctime, reverse=True)

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
        live_restore: bool = False,
    ) -> str:
        """Restore a VM or container from backup.

        This method handles all VM configurations including:
        - Standard BIOS VMs
        - UEFI VMs with EFI disk (efidisk0)
        - VMs with TPM state (tpmstate0) for Windows 11/Server 2022
        - VMs with multiple disks on different storage

        The vzdump backup archive contains the complete VM configuration
        and all disk data, so restoring recreates the VM exactly as it was.

        Args:
            node: Target node for restore
            vmid: Target VM/container ID
            archive: Backup volume ID (e.g., "local:backup/vzdump-qemu-100-...")
            guest_type: QEMU or LXC
            storage: Target storage for disks (optional, uses original if not set).
                    This applies to all disks including efidisk0 and tpmstate0.
            force: Overwrite existing guest with same ID
            unique: Assign unique random MAC addresses (useful for restoring
                   deleted VMs to avoid network conflicts)
            start: Start guest after restore
            bwlimit: Bandwidth limit in KiB/s
            live_restore: Start VM immediately and restore data in background
                         (only works with Proxmox Backup Server archives)

        Returns:
            UPID of the restore task

        Note:
            For UEFI VMs, ensure the target storage supports the EFI disk format.
            For TPM-enabled VMs, the target storage must support raw format.
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
        if live_restore and guest_type == ProxmoxGuestType.QEMU:
            # Live restore only works with PBS backups for QEMU VMs
            data["live-restore"] = 1

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
        # Find guest if node not specified - use find_guest for cluster efficiency
        if not node:
            guest = self.api.find_guest(vmid)
            if not guest:
                raise ProxmoxAPIError(f"Guest {vmid} not found in cluster")
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
        retention: dict[str, int] | None = None,
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
            retention: Retention policy dict (e.g., {"keep_last": 3, "keep_daily": 7}).
                       When provided, Proxmox will automatically prune old backups.
            progress_callback: Optional callback for progress updates
            timeout: Maximum backup time in seconds

        Returns:
            Dict with backup result info including volid and file path
        """
        # Find guest if node not specified - use find_guest for cluster efficiency
        if not node:
            guest = self.api.find_guest(vmid)
            if not guest:
                raise ProxmoxAPIError(f"Guest {vmid} not found in cluster")
            node = guest.node

        logger.info(f"Backup VMID {vmid} on {node} to storage {storage}")

        # Create backup on Proxmox storage
        backup_result = self.backup_guest(
            vmid=vmid,
            storage=storage,
            node=node,
            mode=mode,
            compress=compress,
            retention=retention,
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
        unique: bool = False,
        progress_callback: Any | None = None,
        timeout: int = 7200,
    ) -> dict[str, Any]:
        """Restore a VM or container from backup.

        This method fully restores VMs including:
        - All disk data (system disks, data disks)
        - EFI disk (efidisk0) for UEFI VMs
        - TPM state (tpmstate0) for Windows 11/Server 2022
        - Complete VM configuration (CPU, RAM, network, etc.)

        Args:
            vmid: Original VM/container ID (for finding backup)
            archive: Backup volume ID
            node: Target node for restore
            guest_type: QEMU or LXC
            target_vmid: Target VM ID (defaults to original)
            storage: Target storage for disks (applies to all disks)
            force: Overwrite existing guest
            start_after: Start guest after restore
            unique: Assign unique random MAC addresses (recommended when
                   restoring a deleted VM to avoid network conflicts)
            progress_callback: Optional callback for progress updates
            timeout: Maximum restore time in seconds

        Returns:
            Dict with restore result info
        """
        target_vmid = target_vmid or vmid

        started_at = datetime.now()

        logger.info(f"Starting restore of {archive} to VMID {target_vmid} on {node}")

        # Check if target VM exists and is running - must stop it first
        if force:
            try:
                status = self.api.get_guest_status(node, target_vmid, guest_type)
                if status == "running":
                    logger.info(f"VM {target_vmid} is running, stopping before restore...")
                    stop_upid = self.api.stop_guest(node, target_vmid, guest_type, timeout=120)

                    # Wait for stop to complete
                    stop_status = self.api.wait_for_task(node, stop_upid, timeout=120)
                    if not stop_status.is_success:
                        raise ProxmoxAPIError(
                            f"Failed to stop VM {target_vmid} before restore: {stop_status.exitstatus}"
                        )
                    logger.info(f"VM {target_vmid} stopped successfully")

                    # Small delay to ensure VM is fully stopped
                    import time
                    time.sleep(2)
            except ProxmoxAPIError as e:
                # If guest doesn't exist, that's fine - we're restoring fresh
                if "does not exist" not in str(e).lower() and "not found" not in str(e).lower():
                    raise

        upid = self.api.restore_guest(
            node=node,
            vmid=target_vmid,
            archive=archive,
            guest_type=guest_type,
            storage=storage,
            force=force,
            unique=unique,
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

            # Provide helpful hints for common EFI/TPM restore issues
            error_text = " ".join(str(e) for e in result["errors"]).lower()
            hints = []

            if "efidisk" in error_text or "efi" in error_text:
                if "unexpected size" in error_text:
                    hints.append(
                        "EFI disk size mismatch: The backup may have been created with a different "
                        "OVMF format (2M vs 4M). Try restoring to a storage that supports the "
                        "original EFI disk size."
                    )
                else:
                    hints.append(
                        "EFI disk restore issue: Ensure the target storage supports EFI disk format. "
                        "Some storage types may require specific configuration for UEFI VMs."
                    )

            if "tpmstate" in error_text or "tpm" in error_text:
                hints.append(
                    "TPM state restore issue: TPM state requires raw format storage. "
                    "Ensure the target storage supports raw disk format. "
                    "Note: TPM state cannot be cloned, only restored from backup."
                )

            if "storage" in error_text and "not found" in error_text:
                hints.append(
                    "The original storage used by this VM doesn't exist on the target node. "
                    "Specify a different target storage using the 'storage' parameter."
                )

            if hints:
                result["hints"] = hints

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

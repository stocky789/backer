"""Hyper-V integration for backing up VMs.

This module provides integration with Microsoft Hyper-V for backing up:
- Virtual machines via Export-VM or checkpoints
- Supports live exports for running VMs (if guest services enabled)
- Uses WinRM + PowerShell for remote management via pywinrm

Authentication is done via WinRM using NTLM, Basic, or Kerberos authentication.
No additional software required on the Windows host - just WinRM (enabled by default).
"""

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

try:
    import winrm
    from winrm.protocol import Protocol

    WINRM_AVAILABLE = True
except ImportError:
    WINRM_AVAILABLE = False
    winrm = None  # type: ignore
    Protocol = None  # type: ignore

logger = logging.getLogger(__name__)


class HyperVAuthMethod(str, Enum):
    """Authentication method for Hyper-V WinRM."""

    BASIC = "basic"
    NTLM = "ntlm"
    KERBEROS = "kerberos"


class HyperVBackupMode(str, Enum):
    """Backup mode for Hyper-V VMs."""

    ONLINE = "online"  # Live export (requires guest services)
    OFFLINE = "offline"  # Shutdown VM, export, restart
    CHECKPOINT = "checkpoint"  # Create checkpoint then export


class HyperVGuestType(str, Enum):
    """Type of Hyper-V guest."""

    VM = "vm"


class HyperVVMState(str, Enum):
    """Hyper-V VM states."""

    RUNNING = "Running"
    OFF = "Off"
    SAVED = "Saved"
    PAUSED = "Paused"
    STARTING = "Starting"
    STOPPING = "Stopping"
    SAVING = "Saving"
    RESUMING = "Resuming"
    RESET = "Reset"
    OTHER = "Other"


@dataclass
class HyperVGuest:
    """Represents a VM on Hyper-V."""

    vmid: str  # VM GUID
    name: str
    state: str
    cpus: int = 0
    memory_mb: int = 0  # Assigned memory in MB
    uptime: int = 0  # seconds
    generation: int = 1  # VM generation (1 or 2)
    version: str = ""  # Configuration version
    path: str = ""  # VM configuration path
    dynamic_memory: bool = False
    checkpoints_enabled: bool = True

    @property
    def is_running(self) -> bool:
        """Check if VM is running."""
        return self.state.lower() == "running"

    @property
    def memory_gb(self) -> float:
        """Memory in GB."""
        return self.memory_mb / 1024


class HyperVAPIError(Exception):
    """Exception raised for Hyper-V API errors."""

    def __init__(
        self, message: str, status_code: int = 0, errors: list[str] | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


class HyperVAPI:
    """Client for Hyper-V management via WinRM + PowerShell.

    Connects to a Windows Server running Hyper-V and executes PowerShell
    commands to manage and backup VMs.

    Example usage:
        api = HyperVAPI(
            host="192.168.1.100",
            username="Administrator",
            password="mypassword",
        )
        vms = api.list_guests()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 5985,
        use_ssl: bool = False,
        auth_method: HyperVAuthMethod = HyperVAuthMethod.NTLM,
        verify_ssl: bool = False,
        timeout: int = 60,
        domain: str | None = None,
    ):
        """Initialize Hyper-V API client.

        Args:
            host: Hyper-V server hostname or IP
            port: WinRM port (5985 for HTTP, 5986 for HTTPS)
            username: Windows username
            password: Windows password
            use_ssl: Use HTTPS for WinRM
            auth_method: Authentication method (basic, ntlm, kerberos)
            verify_ssl: Whether to verify SSL certificates
            timeout: Command timeout in seconds
            domain: Windows domain (optional, for domain accounts)
        """
        self.host = host.rstrip("/")
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.auth_method = auth_method
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.domain = domain

        # Build WinRM URL
        protocol = "https" if use_ssl else "http"
        self.winrm_url = f"{protocol}://{self.host}:{self.port}/wsman"

        # Cached version info
        self._version: str | None = None
        self._hostname: str | None = None

    @property
    def version(self) -> str | None:
        """Get cached version info, or None if not yet connected."""
        return self._version

    def _get_full_username(self) -> str:
        """Build the full username for authentication.

        Supports multiple AD authentication formats:
        - DOMAIN\\username (traditional NetBIOS)
        - username@domain.com (UPN format)
        - username (local accounts)
        """
        # If username already contains domain info, use as-is
        if "\\" in self.username or "@" in self.username:
            logger.debug(f"Using provided username format: {self.username}")
            return self.username

        # If domain is specified, build DOMAIN\\username format
        if self.domain:
            # Handle both NetBIOS (DOMAIN) and FQDN (domain.com) formats
            # Use the domain as-is - it could be either format
            full_username = f"{self.domain}\\{self.username}"
            logger.debug(f"Built domain username: {full_username}")
            return full_username

        # Local account
        logger.debug(f"Using local username: {self.username}")
        return self.username

    def _run_powershell(
        self,
        script: str,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Execute a PowerShell command via WinRM using pywinrm.

        Uses the pywinrm library to connect to Windows via WinRM protocol.
        This is the native Windows remote management protocol - no additional
        software needed on the Windows host (WinRM is built into Windows).

        Supports:
        - Local accounts: username
        - Domain accounts (NetBIOS): DOMAIN\\username
        - Domain accounts (UPN): username@domain.com
        - NTLM, Basic, and Kerberos authentication

        Args:
            script: PowerShell script to execute
            timeout: Optional command timeout

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        if not WINRM_AVAILABLE:
            return (
                1,
                "",
                "pywinrm library not installed. Install with: pip install pywinrm",
            )

        effective_timeout = timeout or self.timeout
        full_username = self._get_full_username()

        logger.debug(f"Executing PowerShell on {self.host} as {full_username} via WinRM")

        # Map auth method to pywinrm transport
        if self.auth_method == HyperVAuthMethod.BASIC:
            transport = "basic"
        elif self.auth_method == HyperVAuthMethod.KERBEROS:
            transport = "kerberos"
        else:
            # NTLM is the default and most compatible
            transport = "ntlm"

        logger.debug(f"Using WinRM transport: {transport}")

        try:
            # Create WinRM session
            session = winrm.Session(
                target=self.winrm_url,
                auth=(full_username, self.password),
                transport=transport,
                server_cert_validation="ignore" if not self.verify_ssl else "validate",
                operation_timeout_sec=effective_timeout,
                read_timeout_sec=effective_timeout + 10,
            )

            # Run the PowerShell script
            result = session.run_ps(script)

            # pywinrm returns status_code, std_out (bytes), std_err (bytes)
            stdout = result.std_out.decode("utf-8", errors="replace") if result.std_out else ""
            stderr = result.std_err.decode("utf-8", errors="replace") if result.std_err else ""

            if result.status_code != 0:
                logger.debug(f"WinRM execution returned code {result.status_code}: {stderr[:200]}")
            else:
                logger.debug("WinRM execution succeeded")

            return result.status_code, stdout, stderr

        except Exception as e:
            error_msg = str(e)

            # Provide helpful error messages for common issues
            if "401" in error_msg or "Unauthorized" in error_msg:
                logger.error(f"WinRM authentication failed for {self.host}: {error_msg}")
                return 1, "", f"Authentication failed. Check username/password. Error: {error_msg}"

            if "connection" in error_msg.lower() or "refused" in error_msg.lower():
                logger.error(f"WinRM connection to {self.host} failed: {error_msg}")
                return (
                    1,
                    "",
                    f"Connection failed. Ensure WinRM is enabled on the Hyper-V host. "
                    f"Run 'Enable-PSRemoting -Force' on the Windows host. Error: {error_msg}",
                )

            if "timeout" in error_msg.lower():
                logger.error(f"WinRM command timed out after {effective_timeout}s")
                return 1, "", f"Command timed out after {effective_timeout} seconds"

            logger.exception(f"WinRM execution failed: {e}")
            return 1, "", f"WinRM execution failed: {error_msg}"

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Hyper-V server.

        Returns:
            Tuple of (success: bool, message: str with version info or error)
        """
        full_username = self._get_full_username()
        logger.info(
            f"Testing Hyper-V connection to {self.host} as {full_username} "
            f"(auth: {self.auth_method.value}, port: {self.port}, ssl: {self.use_ssl})"
        )

        try:
            # Try to get Hyper-V version info
            script = """
$vmHost = Get-VMHost -ErrorAction Stop
$os = Get-CimInstance Win32_OperatingSystem
$hvVersion = "Unknown"
try {
    $vmmsPath = Join-Path $env:SystemRoot "System32\vmms.exe"
    if (Test-Path $vmmsPath) {
        $hvVersion = (Get-Item $vmmsPath).VersionInfo.ProductVersion
    }
} catch {}
@{
    Hostname = $vmHost.ComputerName
    HyperVVersion = $hvVersion
    OSVersion = $os.Caption
    OSBuild = $os.BuildNumber
    VirtualMachinePath = $vmHost.VirtualMachinePath
    VirtualHardDiskPath = $vmHost.VirtualHardDiskPath
} | ConvertTo-Json
"""
            rc, stdout, stderr = self._run_powershell(script)

            if rc != 0:
                error_msg = stderr.strip() or "Connection failed"
                # Check for common errors
                if "Access is denied" in error_msg:
                    logger.error(f"Authentication failed for {self.host}: Access denied")
                    return False, "Authentication failed: Access denied. Check username/password."
                if "WinRM" in error_msg or "cannot connect" in error_msg.lower():
                    logger.error(f"WinRM connection to {self.host} failed: {error_msg}")
                    return False, f"WinRM connection failed: {error_msg}"
                if "Kerberos" in error_msg:
                    logger.error(f"Kerberos authentication failed for {self.host}: {error_msg}")
                    return False, f"Kerberos authentication failed: {error_msg}"
                logger.error(f"Connection to {self.host} failed: {error_msg}")
                return False, f"Connection failed: {error_msg}"

            # Parse the JSON response
            try:
                # Find JSON in output (may have other text before/after)
                json_match = re.search(r"\{.*\}", stdout, re.DOTALL)
                if json_match:
                    info = json.loads(json_match.group())
                    self._hostname = info.get("Hostname", self.host)
                    self._version = info.get("OSVersion", "Unknown")
                    hv_version = info.get("HyperVVersion", "Unknown")
                    logger.info(
                        f"Successfully connected to Hyper-V host {self._hostname} "
                        f"(OS: {self._version}, Hyper-V: {hv_version})"
                    )
                    return True, f"Connected to {self._hostname} ({self._version})"
                else:
                    logger.info(f"Connected to Hyper-V host {self.host}")
                    return True, f"Connected to {self.host}"
            except json.JSONDecodeError:
                # Connection worked even if we couldn't parse the response
                logger.info(f"Connected to Hyper-V host {self.host} (version info unavailable)")
                return True, f"Connected to {self.host}"

        except Exception as e:
            logger.exception(f"Hyper-V connection test to {self.host} failed")
            return False, f"Connection failed: {e}"

    def get_version(self) -> str:
        """Get Hyper-V/Windows version info.

        Returns:
            Version string
        """
        if self._version:
            return self._version

        script = """
$os = Get-CimInstance Win32_OperatingSystem
Write-Output "$($os.Caption) (Build $($os.BuildNumber))"
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc == 0:
            self._version = stdout.strip()
        else:
            self._version = "Unknown"

        return self._version

    def list_guests(self) -> list[HyperVGuest]:
        """List all VMs on the Hyper-V server.

        Returns:
            List of HyperVGuest objects
        """
        script = """
Get-VM | Select-Object `
    Id, Name, State, ProcessorCount, MemoryAssigned, Uptime, `
    Generation, Version, Path, DynamicMemoryEnabled, `
    @{N='CheckpointsEnabled';E={$_.CheckpointType -ne 'Disabled'}} |
ConvertTo-Json -Compress
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0:
            raise HyperVAPIError(f"Failed to list VMs: {stderr}")

        guests = []
        try:
            # Handle empty response or single VM
            stdout = stdout.strip()
            if not stdout or stdout == "null":
                return []

            data = json.loads(stdout)

            # Ensure data is a list
            if isinstance(data, dict):
                data = [data]

            for vm in data:
                # Convert memory from bytes to MB
                memory_bytes = vm.get("MemoryAssigned", 0) or 0
                memory_mb = memory_bytes // (1024 * 1024)

                # Parse uptime (TimeSpan object comes as dict or string)
                uptime_seconds = 0
                uptime = vm.get("Uptime")
                if isinstance(uptime, dict):
                    uptime_seconds = int(
                        uptime.get("TotalSeconds", 0)
                        or uptime.get("Ticks", 0) / 10_000_000
                    )
                elif isinstance(uptime, (int, float)):
                    uptime_seconds = int(uptime)

                guest = HyperVGuest(
                    vmid=str(vm.get("Id", "")),
                    name=vm.get("Name", "Unknown"),
                    state=str(vm.get("State", "Unknown")),
                    cpus=vm.get("ProcessorCount", 0) or 0,
                    memory_mb=memory_mb,
                    uptime=uptime_seconds,
                    generation=vm.get("Generation", 1) or 1,
                    version=str(vm.get("Version", "")),
                    path=vm.get("Path", "") or "",
                    dynamic_memory=bool(vm.get("DynamicMemoryEnabled", False)),
                    checkpoints_enabled=bool(vm.get("CheckpointsEnabled", True)),
                )
                guests.append(guest)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse VM list JSON: {e}")
            logger.debug(f"Raw output: {stdout}")

        return guests

    def get_guest(self, vm_name: str) -> HyperVGuest | None:
        """Get a specific VM by name.

        Args:
            vm_name: VM name

        Returns:
            HyperVGuest object or None if not found
        """
        script = f"""
Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue | Select-Object `
    Id, Name, State, ProcessorCount, MemoryAssigned, Uptime, `
    Generation, Version, Path, DynamicMemoryEnabled, `
    @{{N='CheckpointsEnabled';E={{$_.CheckpointType -ne 'Disabled'}}}} |
ConvertTo-Json -Compress
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0 or not stdout.strip() or stdout.strip() == "null":
            return None

        try:
            vm = json.loads(stdout.strip())

            memory_bytes = vm.get("MemoryAssigned", 0) or 0
            memory_mb = memory_bytes // (1024 * 1024)

            uptime_seconds = 0
            uptime = vm.get("Uptime")
            if isinstance(uptime, dict):
                uptime_seconds = int(
                    uptime.get("TotalSeconds", 0)
                    or uptime.get("Ticks", 0) / 10_000_000
                )

            return HyperVGuest(
                vmid=str(vm.get("Id", "")),
                name=vm.get("Name", "Unknown"),
                state=str(vm.get("State", "Unknown")),
                cpus=vm.get("ProcessorCount", 0) or 0,
                memory_mb=memory_mb,
                uptime=uptime_seconds,
                generation=vm.get("Generation", 1) or 1,
                version=str(vm.get("Version", "")),
                path=vm.get("Path", "") or "",
                dynamic_memory=bool(vm.get("DynamicMemoryEnabled", False)),
                checkpoints_enabled=bool(vm.get("CheckpointsEnabled", True)),
            )
        except json.JSONDecodeError:
            return None

    def get_guest_by_id(self, vmid: str) -> HyperVGuest | None:
        """Get a specific VM by ID (GUID).

        Args:
            vmid: VM GUID

        Returns:
            HyperVGuest object or None if not found
        """
        script = f"""
Get-VM | Where-Object {{ $_.Id -eq '{vmid}' }} | Select-Object `
    Id, Name, State, ProcessorCount, MemoryAssigned, Uptime, `
    Generation, Version, Path, DynamicMemoryEnabled, `
    @{{N='CheckpointsEnabled';E={{$_.CheckpointType -ne 'Disabled'}}}} |
ConvertTo-Json -Compress
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0 or not stdout.strip() or stdout.strip() == "null":
            return None

        try:
            vm = json.loads(stdout.strip())

            memory_bytes = vm.get("MemoryAssigned", 0) or 0
            memory_mb = memory_bytes // (1024 * 1024)

            uptime_seconds = 0
            uptime = vm.get("Uptime")
            if isinstance(uptime, dict):
                uptime_seconds = int(
                    uptime.get("TotalSeconds", 0)
                    or uptime.get("Ticks", 0) / 10_000_000
                )

            return HyperVGuest(
                vmid=str(vm.get("Id", "")),
                name=vm.get("Name", "Unknown"),
                state=str(vm.get("State", "Unknown")),
                cpus=vm.get("ProcessorCount", 0) or 0,
                memory_mb=memory_mb,
                uptime=uptime_seconds,
                generation=vm.get("Generation", 1) or 1,
                version=str(vm.get("Version", "")),
                path=vm.get("Path", "") or "",
                dynamic_memory=bool(vm.get("DynamicMemoryEnabled", False)),
                checkpoints_enabled=bool(vm.get("CheckpointsEnabled", True)),
            )
        except json.JSONDecodeError:
            return None

    def start_vm(self, vm_name: str) -> bool:
        """Start a VM.

        Args:
            vm_name: VM name

        Returns:
            True if started successfully
        """
        script = f"Start-VM -Name '{vm_name}' -ErrorAction Stop"
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0:
            logger.error(f"Failed to start VM {vm_name}: {stderr}")
            return False

        logger.info(f"Started VM: {vm_name}")
        return True

    def stop_vm(self, vm_name: str, force: bool = False) -> bool:
        """Stop a VM.

        Args:
            vm_name: VM name
            force: Force stop (turn off) vs graceful shutdown

        Returns:
            True if stopped successfully
        """
        if force:
            script = f"Stop-VM -Name '{vm_name}' -TurnOff -Force -ErrorAction Stop"
        else:
            script = f"Stop-VM -Name '{vm_name}' -Force -ErrorAction Stop"

        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0:
            logger.error(f"Failed to stop VM {vm_name}: {stderr}")
            return False

        logger.info(f"Stopped VM: {vm_name}")
        return True

    def shutdown_vm(self, vm_name: str, timeout: int = 300) -> bool:
        """Gracefully shutdown a VM (requires guest integration services).

        Args:
            vm_name: VM name
            timeout: Shutdown timeout in seconds

        Returns:
            True if shutdown successfully
        """
        # First check if integration services are available
        script = f"""
$vm = Get-VM -Name '{vm_name}' -ErrorAction Stop
if ($vm.State -ne 'Running') {{
    Write-Output "VM is not running"
    exit 0
}}

# Try graceful shutdown
Stop-VM -Name '{vm_name}' -ErrorAction Stop

# Wait for shutdown
$waited = 0
while ((Get-VM -Name '{vm_name}').State -ne 'Off' -and $waited -lt {timeout}) {{
    Start-Sleep -Seconds 5
    $waited += 5
}}

$finalState = (Get-VM -Name '{vm_name}').State
if ($finalState -eq 'Off') {{
    Write-Output "Shutdown complete"
}} else {{
    Write-Error "Shutdown timeout - VM state: $finalState"
    exit 1
}}
"""
        rc, stdout, stderr = self._run_powershell(script, timeout=timeout + 30)

        if rc != 0:
            logger.error(f"Failed to shutdown VM {vm_name}: {stderr}")
            return False

        logger.info(f"Shutdown VM: {vm_name}")
        return True

    def create_checkpoint(
        self, vm_name: str, checkpoint_name: str | None = None
    ) -> str | None:
        """Create a VM checkpoint (snapshot).

        Args:
            vm_name: VM name
            checkpoint_name: Optional checkpoint name

        Returns:
            Checkpoint ID if successful, None otherwise
        """
        if checkpoint_name:
            script = f"""
$cp = Checkpoint-VM -Name '{vm_name}' -SnapshotName '{checkpoint_name}' -PassThru -ErrorAction Stop
Write-Output $cp.Id
"""
        else:
            script = f"""
$cp = Checkpoint-VM -Name '{vm_name}' -PassThru -ErrorAction Stop
Write-Output $cp.Id
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0:
            logger.error(f"Failed to create checkpoint for {vm_name}: {stderr}")
            return None

        checkpoint_id = stdout.strip()
        logger.info(f"Created checkpoint for VM {vm_name}: {checkpoint_id}")
        return checkpoint_id

    def remove_checkpoint(self, vm_name: str, checkpoint_id: str) -> bool:
        """Remove a VM checkpoint.

        Args:
            vm_name: VM name
            checkpoint_id: Checkpoint ID (GUID)

        Returns:
            True if removed successfully
        """
        script = f"""
Get-VMSnapshot -VMName '{vm_name}' | Where-Object {{ $_.Id -eq '{checkpoint_id}' }} |
Remove-VMSnapshot -ErrorAction Stop
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0:
            logger.error(f"Failed to remove checkpoint {checkpoint_id}: {stderr}")
            return False

        logger.info(f"Removed checkpoint: {checkpoint_id}")
        return True

    def export_snapshot(
        self,
        vm_name: str,
        snapshot_name: str,
        export_path: str,
        timeout: int = 3600,
    ) -> tuple[bool, str]:
        """Export a VM snapshot/checkpoint to a path.

        Per Microsoft Export-VMSnapshot documentation, this exports a checkpoint
        as a full VM that can be imported independently.

        Args:
            vm_name: VM name
            snapshot_name: Name of the checkpoint to export
            export_path: Destination path for export
            timeout: Export timeout in seconds

        Returns:
            Tuple of (success, message/error)
        """
        script = f"""
$exportPath = '{export_path}'
if (-not (Test-Path $exportPath)) {{
    New-Item -ItemType Directory -Path $exportPath -Force | Out-Null
}}

# Export the snapshot
Export-VMSnapshot -VMName '{vm_name}' -Name '{snapshot_name}' -Path $exportPath -ErrorAction Stop

# Verify export
$vmExportPath = Join-Path $exportPath '{vm_name}'
if (Test-Path $vmExportPath) {{
    @{{ Success = $true; Path = $vmExportPath }} | ConvertTo-Json
}} else {{
    @{{ Success = $false; Error = "Export completed but folder not found" }} | ConvertTo-Json
}}
"""
        rc, stdout, stderr = self._run_powershell(script, timeout=timeout)

        if rc != 0:
            error = stderr.strip() or "Snapshot export failed"
            logger.error(f"Failed to export snapshot {snapshot_name}: {error}")
            return False, error

        try:
            result = json.loads(stdout.strip())
            if result.get("Success"):
                return True, result.get("Path", export_path)
            return False, result.get("Error", "Export failed")
        except json.JSONDecodeError:
            return True, f"{export_path}/{vm_name}"

    def wait_for_vm_state(
        self,
        vm_name: str,
        target_state: str,
        timeout: int = 300,
        interval: int = 5,
    ) -> bool:
        """Wait for a VM to reach a specific state.

        Args:
            vm_name: VM name
            target_state: Target state (Running, Off, Saved, Paused)
            timeout: Maximum wait time in seconds
            interval: Check interval in seconds

        Returns:
            True if target state reached, False if timeout
        """
        script = f"""
$waited = 0
$timeout = {timeout}
$interval = {interval}
$targetState = '{target_state}'

while ($waited -lt $timeout) {{
    $vm = Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue
    if ($vm -and $vm.State -eq $targetState) {{
        Write-Output "SUCCESS"
        exit 0
    }}
    Start-Sleep -Seconds $interval
    $waited += $interval
}}
Write-Output "TIMEOUT"
exit 1
"""
        rc, stdout, stderr = self._run_powershell(script, timeout=timeout + 30)
        return rc == 0 and "SUCCESS" in stdout

    def export_vm(
        self,
        vm_name: str,
        export_path: str,
        capture_live_state: str | None = None,
        timeout: int = 3600,
        smb_username: str | None = None,
        smb_password: str | None = None,
    ) -> tuple[bool, str]:
        """Export a VM to a path.

        This uses Hyper-V's Export-VM cmdlet which creates a complete
        copy of the VM including configuration and virtual disks.

        The export creates a folder structure with:
        - Snapshots: Associated snapshot/checkpoint files
        - Virtual Hard Disks: VM virtual disks
        - Virtual Machines: Configuration files (.vmcx)

        For network (UNC) paths, the export is performed to a local temp
        directory first, then copied to the network share. This is required
        because Export-VM runs under the VMMS service (SYSTEM account) which
        cannot access network shares authenticated via WinRM (double-hop issue).

        Args:
            vm_name: VM name
            export_path: Destination path for export (can be UNC path for SMB)
            capture_live_state: Optional live state capture mode:
                - 'CaptureSavedState': Include memory state
                - 'CaptureDataConsistentState': Use Production Checkpoint technology
                - 'CaptureCrashConsistentState': No memory state handling
            timeout: Export timeout in seconds
            smb_username: Optional SMB username for network path authentication
            smb_password: Optional SMB password for network path authentication

        Returns:
            Tuple of (success, message/error)
        """
        # Build the Export-VM command with proper parameters per Microsoft docs
        export_cmd_base = f"Export-VM -Name '{vm_name}' -Path"
        capture_param = ""
        if capture_live_state:
            capture_param = f" -CaptureLiveState {capture_live_state}"

        # Check if destination is a UNC path (network share)
        is_unc_path = export_path.startswith("\\\\")

        if is_unc_path and smb_username and smb_password:
            # For UNC paths, export to local temp first, then copy to network share
            # This avoids the double-hop problem where Export-VM (running as SYSTEM)
            # cannot access network shares authenticated via WinRM session
            unc_parts = export_path.lstrip("\\").split("\\")
            if len(unc_parts) >= 2:
                smb_server = unc_parts[0]
                smb_share = unc_parts[1]
                smb_unc = f"\\\\{smb_server}\\{smb_share}"
                # Escape single quotes in password
                safe_password = smb_password.replace("'", "''")

                script = f"""
$ErrorActionPreference = 'Stop'
$vmName = '{vm_name}'
$finalPath = '{export_path}'
$smbUnc = '{smb_unc}'
$smbUser = '{smb_username}'
$smbPass = '{safe_password}'

# Create a local temp directory for export
$tempBase = 'C:\Backer\temp'
if (-not (Test-Path $tempBase)) {{
    New-Item -ItemType Directory -Path $tempBase -Force | Out-Null
}}
$localExportPath = Join-Path $tempBase ([System.Guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $localExportPath -Force | Out-Null

try {{
    # Export VM to local temp directory (this works because VMMS can access local paths)
    {export_cmd_base} $localExportPath{capture_param} -ErrorAction Stop

    $vmExportPath = Join-Path $localExportPath $vmName
    if (-not (Test-Path $vmExportPath)) {{
        throw "Export completed but VM folder not found at $vmExportPath"
    }}

    # Calculate size before copy
    $size = (Get-ChildItem $vmExportPath -Recurse -ErrorAction SilentlyContinue |
             Measure-Object -Property Length -Sum).Sum

    # Now authenticate to SMB and copy files
    # Remove any existing connection first
    net use $smbUnc /delete 2>$null | Out-Null
    $netResult = net use $smbUnc /user:$smbUser $smbPass 2>&1
    if ($LASTEXITCODE -ne 0) {{
        throw "Failed to connect to SMB share: $netResult"
    }}

    try {{
        # Create destination directory if needed
        if (-not (Test-Path $finalPath)) {{
            New-Item -ItemType Directory -Path $finalPath -Force | Out-Null
        }}

        # Copy exported VM to network share
        $destVmPath = Join-Path $finalPath $vmName
        # Remove existing backup if present (for clean overwrite)
        if (Test-Path $destVmPath) {{
            Remove-Item -Path $destVmPath -Recurse -Force
        }}
        Copy-Item -Path $vmExportPath -Destination $finalPath -Recurse -Force

        @{{
            Success = $true
            Path = $destVmPath
            SizeBytes = if ($size) {{ $size }} else {{ 0 }}
        }} | ConvertTo-Json
    }} finally {{
        # Cleanup SMB connection
        net use $smbUnc /delete 2>$null | Out-Null
    }}
}} finally {{
    # Always cleanup local temp directory
    if (Test-Path $localExportPath) {{
        Remove-Item -Path $localExportPath -Recurse -Force -ErrorAction SilentlyContinue
    }}
}}
"""
        else:
            # Local path or no SMB credentials - export directly
            script = f"""
$ErrorActionPreference = 'Stop'
$exportPath = '{export_path}'

# Create export directory if it doesn't exist
if (-not (Test-Path $exportPath)) {{
    New-Item -ItemType Directory -Path $exportPath -Force | Out-Null
}}

# Export the VM
{export_cmd_base} $exportPath{capture_param} -ErrorAction Stop

# Get the exported VM folder
$vmExportPath = Join-Path $exportPath '{vm_name}'
if (Test-Path $vmExportPath) {{
    # Calculate total size
    $size = (Get-ChildItem $vmExportPath -Recurse -ErrorAction SilentlyContinue |
             Measure-Object -Property Length -Sum).Sum
    @{{
        Success = $true
        Path = $vmExportPath
        SizeBytes = if ($size) {{ $size }} else {{ 0 }}
    }} | ConvertTo-Json
}} else {{
    @{{
        Success = $false
        Error = "Export completed but folder not found"
    }} | ConvertTo-Json
}}
"""
        rc, stdout, stderr = self._run_powershell(script, timeout=timeout)

        if rc != 0:
            error = stderr.strip() or "Export failed"
            logger.error(f"Failed to export VM {vm_name}: {error}")
            return False, error

        try:
            result = json.loads(stdout.strip())
            if result.get("Success"):
                return True, result.get("Path", export_path)
            else:
                return False, result.get("Error", "Export failed")
        except json.JSONDecodeError:
            # If we can't parse but rc was 0, assume success
            return True, f"{export_path}/{vm_name}"


class HyperVBackupManager:
    """High-level backup orchestration for Hyper-V.

    Manages VM backups using Export-VM or checkpoint-based approaches.
    Supports SMB repositories for storing backups.
    """

    def __init__(
        self,
        api: HyperVAPI,
    ):
        """Initialize backup manager.

        Args:
            api: HyperVAPI instance for server communication
        """
        self.api = api

    def list_all_guests(self) -> list[dict[str, Any]]:
        """List all VMs in a format suitable for the API.

        Returns:
            List of guest info dicts with standardized fields
        """
        guests = []

        try:
            vms = self.api.list_guests()
            for vm in vms:
                guests.append(
                    {
                        "vmid": vm.vmid,
                        "name": vm.name,
                        "node": self.api.host,
                        "type": "vm",
                        "guest_type": HyperVGuestType.VM.value,
                        "status": vm.state.lower(),
                        "cpus": vm.cpus,
                        "maxmem_gb": vm.memory_gb,
                        "maxdisk_gb": 0,  # Would need to query disk info separately
                        "generation": vm.generation,
                    }
                )
        except Exception as e:
            logger.error(f"Failed to list VMs: {e}")

        return guests

    def backup_vm(
        self,
        vm_name: str,
        backup_path: str,
        backup_mode: str = "online",
        progress_callback: Any | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
    ) -> dict[str, Any]:
        """Backup a Hyper-V VM.

        Args:
            vm_name: VM name to backup
            backup_path: Destination path (should be accessible from Hyper-V host)
            backup_mode: Backup mode (online, offline, checkpoint)
            progress_callback: Optional callback for progress updates
            smb_username: SMB username for network share authentication
            smb_password: SMB password for network share authentication

        Returns:
            Dict with backup result info
        """
        from datetime import datetime as dt

        started_at = dt.now()
        result: dict[str, Any] = {
            "success": False,
            "vm_name": vm_name,
            "backup_path": backup_path,
            "backup_mode": backup_mode,
            "files": [],
            "errors": [],
            "size_bytes": 0,
            "duration_seconds": 0,
        }

        try:
            # Get VM info
            vm = self.api.get_guest(vm_name)
            if not vm:
                result["errors"].append(f"VM '{vm_name}' not found")
                return result

            was_running = vm.is_running
            result["was_running"] = was_running

            if progress_callback:
                progress_callback(
                    {"status": "starting", "vm": vm_name, "mode": backup_mode}
                )

            # Handle different backup modes
            checkpoint_id = None

            if backup_mode == "offline":
                # Shutdown VM if running
                if was_running:
                    if progress_callback:
                        progress_callback({"status": "shutting_down", "vm": vm_name})

                    if not self.api.shutdown_vm(vm_name, timeout=300):
                        # Try force stop
                        if not self.api.stop_vm(vm_name, force=True):
                            result["errors"].append("Failed to stop VM for backup")
                            return result

            elif backup_mode == "checkpoint":
                # Create a checkpoint for consistent backup
                if progress_callback:
                    progress_callback({"status": "creating_checkpoint", "vm": vm_name})

                timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
                checkpoint_name = f"backer_backup_{timestamp}"
                checkpoint_id = self.api.create_checkpoint(vm_name, checkpoint_name)

                if not checkpoint_id:
                    result["errors"].append("Failed to create checkpoint")
                    return result

            # Perform export
            if progress_callback:
                progress_callback({"status": "exporting", "vm": vm_name})

            # Create timestamped backup directory
            timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
            vm_backup_path = f"{backup_path}\\{vm_name}_{timestamp}"

            success, export_result = self.api.export_vm(
                vm_name,
                vm_backup_path,
                timeout=7200,  # 2 hour timeout for large VMs
                smb_username=smb_username,
                smb_password=smb_password,
            )

            if success:
                result["success"] = True
                result["files"].append(export_result)
                result["export_path"] = export_result

                # Try to get size info
                size_script = f"""
$path = '{export_result}'
if (Test-Path $path) {{
    (Get-ChildItem $path -Recurse | Measure-Object -Property Length -Sum).Sum
}} else {{
    0
}}
"""
                rc, stdout, stderr = self.api._run_powershell(size_script)
                if rc == 0:
                    try:
                        result["size_bytes"] = int(stdout.strip())
                    except ValueError:
                        pass

            else:
                result["errors"].append(f"Export failed: {export_result}")

            # Cleanup checkpoint if created
            if checkpoint_id:
                if progress_callback:
                    progress_callback({"status": "removing_checkpoint", "vm": vm_name})
                self.api.remove_checkpoint(vm_name, checkpoint_id)

            # Restart VM if it was running and we stopped it
            if backup_mode == "offline" and was_running:
                if progress_callback:
                    progress_callback({"status": "starting_vm", "vm": vm_name})
                self.api.start_vm(vm_name)

            if progress_callback:
                progress_callback(
                    {"status": "completed", "vm": vm_name, "success": result["success"]}
                )

        except Exception as e:
            logger.exception(f"Backup failed for VM {vm_name}")
            result["errors"].append(str(e))

        finally:
            result["duration_seconds"] = (dt.now() - started_at).total_seconds()

        return result

    def restore_vm(
        self,
        import_path: str,
        vm_name: str | None = None,
        restore_path: str | None = None,
        vhd_destination_path: str | None = None,
        generate_new_id: bool = True,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Restore (import) a Hyper-V VM from an export.

        Per Microsoft Import-VM documentation, this supports two modes:
        - Register in-place: Uses -Path only (files stay where they are)
        - Copy: Uses -Copy flag with optional destination paths

        Args:
            import_path: Path to the exported VM configuration file (.vmcx)
                        or the folder containing Virtual Machines subfolder
            vm_name: Optional new name for the VM after import
            restore_path: Optional destination path for VM configuration files
                         (maps to -VirtualMachinePath parameter)
            vhd_destination_path: Optional destination for VHD files
                                 (maps to -VhdDestinationPath parameter)
            generate_new_id: Generate new VM ID (required for duplicate imports)
            progress_callback: Optional callback for progress updates

        Returns:
            Dict with restore result info
        """
        from datetime import datetime as dt

        started_at = dt.now()
        result: dict[str, Any] = {
            "success": False,
            "import_path": import_path,
            "vm_name": vm_name,
            "errors": [],
            "duration_seconds": 0,
        }

        try:
            if progress_callback:
                progress_callback({"status": "importing", "path": import_path})

            # Per Microsoft docs: Import-VM needs the .vmcx file path, not just folder
            # First, find the .vmcx configuration file
            find_vmcx_script = f"""
$importPath = '{import_path}'
# Check if path is already a .vmcx file
if ($importPath -like '*.vmcx') {{
    if (Test-Path $importPath) {{
        $importPath
    }} else {{
        Write-Error "VMCX file not found: $importPath"
        exit 1
    }}
}} else {{
    # Look for .vmcx in Virtual Machines subfolder (standard export structure)
    $vmFolder = Join-Path $importPath 'Virtual Machines'
    if (Test-Path $vmFolder) {{
        $vmcx = Get-ChildItem -Path $vmFolder -Filter '*.vmcx' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($vmcx) {{
            $vmcx.FullName
        }} else {{
            Write-Error "No .vmcx file found in $vmFolder"
            exit 1
        }}
    }} else {{
        # Maybe it's directly in the path
        $vmcx = Get-ChildItem -Path $importPath -Filter '*.vmcx' -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($vmcx) {{
            $vmcx.FullName
        }} else {{
            Write-Error "No .vmcx file found in $importPath"
            exit 1
        }}
    }}
}}
"""
            rc, stdout, stderr = self.api._run_powershell(find_vmcx_script)
            if rc != 0:
                result["errors"].append(f"Failed to find VM configuration: {stderr}")
                return result

            vmcx_path = stdout.strip()

            # Build import command per Microsoft docs
            # -Copy is required when using -GenerateNewId or destination paths
            use_copy = generate_new_id or restore_path or vhd_destination_path
            import_cmd = f"Import-VM -Path '{vmcx_path}'"

            if use_copy:
                import_cmd += " -Copy"
                if generate_new_id:
                    import_cmd += " -GenerateNewId"
                if vhd_destination_path:
                    import_cmd += f" -VhdDestinationPath '{vhd_destination_path}'"
                if restore_path:
                    import_cmd += f" -VirtualMachinePath '{restore_path}'"

            script = f"""
$vm = {import_cmd} -ErrorAction Stop
if ($vm) {{
    @{{
        Success = $true
        VMId = $vm.Id.ToString()
        VMName = $vm.Name
    }} | ConvertTo-Json
}} else {{
    @{{ Success = $false; Error = "Import returned no VM" }} | ConvertTo-Json
}}
"""
            rc, stdout, stderr = self.api._run_powershell(script, timeout=3600)

            if rc != 0:
                result["errors"].append(f"Import failed: {stderr}")
                return result

            try:
                import_result = json.loads(stdout.strip())
                if import_result.get("Success"):
                    result["success"] = True
                    result["vm_id"] = import_result.get("VMId")
                    result["vm_name"] = import_result.get("VMName")

                    # Rename if requested
                    if vm_name and vm_name != import_result.get("VMName"):
                        rename_script = f"""
Rename-VM -VMName '{import_result.get("VMName")}' -NewName '{vm_name}' -ErrorAction Stop
"""
                        self.api._run_powershell(rename_script)
                        result["vm_name"] = vm_name
                else:
                    result["errors"].append(
                        import_result.get("Error", "Import failed")
                    )
            except json.JSONDecodeError:
                result["errors"].append("Failed to parse import result")

            if progress_callback:
                progress_callback(
                    {
                        "status": "completed",
                        "success": result["success"],
                        "vm_name": result.get("vm_name"),
                    }
                )

        except Exception as e:
            logger.exception(f"Restore failed for {import_path}")
            result["errors"].append(str(e))

        finally:
            result["duration_seconds"] = (dt.now() - started_at).total_seconds()

        return result

    def list_backups(
        self,
        backup_path: str,
        vm_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available VM backups in a directory.

        Scans the backup path for exported VMs (folders containing .vmcx files).

        Args:
            backup_path: UNC path to the backup directory
            vm_name: Optional filter by VM name

        Returns:
            List of backup dicts with name, path, timestamp, size info
        """
        logger.info(f"Listing Hyper-V backups in: {backup_path}")

        filter_clause = ""
        if vm_name:
            filter_clause = f"| Where-Object {{ $_.Name -like '{vm_name}*' }}"

        script = f"""
$backupPath = '{backup_path}'
$backups = @()

if (Test-Path $backupPath) {{
    # Look for VM export folders (contain Virtual Machines subfolder with .vmcx)
    Get-ChildItem -Path $backupPath -Directory {filter_clause} | ForEach-Object {{
        $vmFolder = $_
        $vmcxPath = Join-Path $vmFolder.FullName 'Virtual Machines'
        $vmcxFiles = Get-ChildItem -Path $vmcxPath -Filter '*.vmcx' -ErrorAction SilentlyContinue

        if ($vmcxFiles) {{
            $size = (Get-ChildItem $vmFolder.FullName -Recurse -ErrorAction SilentlyContinue |
                     Measure-Object -Property Length -Sum).Sum

            $backups += @{{
                Name = $vmFolder.Name
                Path = $vmFolder.FullName
                CreatedAt = $vmFolder.CreationTime.ToString('o')
                ModifiedAt = $vmFolder.LastWriteTime.ToString('o')
                SizeBytes = if ($size) {{ $size }} else {{ 0 }}
                VmcxFile = $vmcxFiles[0].FullName
            }}
        }}
    }}
}}

$backups | ConvertTo-Json -Compress
"""
        rc, stdout, stderr = self.api._run_powershell(script, timeout=120)

        if rc != 0:
            logger.error(f"Failed to list backups: {stderr}")
            return []

        try:
            stdout = stdout.strip()
            if not stdout or stdout == "null" or stdout == "[]":
                return []

            data = json.loads(stdout)
            if isinstance(data, dict):
                data = [data]

            backups = []
            for item in data:
                backups.append({
                    "name": item.get("Name", ""),
                    "path": item.get("Path", ""),
                    "created_at": item.get("CreatedAt", ""),
                    "modified_at": item.get("ModifiedAt", ""),
                    "size_bytes": item.get("SizeBytes", 0),
                    "vmcx_file": item.get("VmcxFile", ""),
                })

            logger.info(f"Found {len(backups)} Hyper-V backups in {backup_path}")
            return backups

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse backup list: {e}")
            return []

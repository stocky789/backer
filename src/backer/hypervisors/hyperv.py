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
    # Cluster-specific fields
    owner_node: str = ""  # Node currently owning the VM (cluster only)
    is_clustered: bool = False  # Whether VM is part of a failover cluster

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

    def _run_powershell_large(
        self, script: str, timeout: int | None = None
    ) -> tuple[int, str, str]:
        """Execute a large PowerShell script using environment variables.

        This avoids command line length limits by passing the script via
        environment variables when opening the WinRM shell, then executing
        it with Invoke-Expression.

        Based on workaround from: https://github.com/diyan/pywinrm/issues/184

        Args:
            script: PowerShell script to execute
            timeout: Optional command timeout

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        from base64 import b64encode

        if not WINRM_AVAILABLE:
            return 1, "", "pywinrm library not installed"

        effective_timeout = timeout or self.timeout
        full_username = self._get_full_username()

        # Map auth method to pywinrm transport
        if self.auth_method == HyperVAuthMethod.BASIC:
            transport = "basic"
        elif self.auth_method == HyperVAuthMethod.KERBEROS:
            transport = "kerberos"
        else:
            transport = "ntlm"

        try:
            # Use Protocol directly to access open_shell with env_vars
            protocol = winrm.Protocol(
                endpoint=self.winrm_url,
                transport=transport,
                username=full_username,
                password=self.password,
                server_cert_validation="ignore" if not self.verify_ssl else "validate",
                operation_timeout_sec=effective_timeout,
                read_timeout_sec=effective_timeout + 10,
            )

            # Small command that loads and executes the script from env var
            loader_cmd = ". ([ScriptBlock]::Create($Env:BACKER_SCRIPT))"
            encoded_cmd = b64encode(loader_cmd.encode("utf_16_le")).decode("ascii")

            # Open shell with script in environment variable
            shell_id = protocol.open_shell(env_vars={"BACKER_SCRIPT": script})

            try:
                # Run the loader command
                command_id = protocol.run_command(
                    shell_id, f"powershell -EncodedCommand {encoded_cmd}"
                )
                stdout_bytes, stderr_bytes, return_code = protocol.get_command_output(
                    shell_id, command_id
                )
                protocol.cleanup_command(shell_id, command_id)

                stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

                return return_code, stdout, stderr

            finally:
                protocol.close_shell(shell_id)

        except Exception as e:
            error_msg = str(e)
            logger.exception(f"WinRM large script execution failed: {e}")
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

            # Handle PowerShell returning array vs single object
            if isinstance(vm, list):
                if len(vm) == 0:
                    return None
                vm = vm[0]  # Take first VM if multiple returned

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

            # Handle PowerShell returning array vs single object
            if isinstance(vm, list):
                if len(vm) == 0:
                    return None
                vm = vm[0]  # Take first VM if multiple returned

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

    def capture_vm_config(self, vm_name: str) -> dict[str, Any] | None:
        """Capture comprehensive VM configuration for backup/restore.

        Captures ALL VM settings including firmware, processor, memory,
        network adapters, security, GPU assignments, and cluster status.
        This config can be used to fully recreate a VM with identical settings.

        Args:
            vm_name: VM name to capture config from

        Returns:
            Dict with complete VM configuration, or None if capture fails
        """
        script = f"""
$ErrorActionPreference = 'Stop'
$vmName = '{vm_name}'

try {{
    $vm = Get-VM -Name $vmName -ErrorAction Stop

    # Build comprehensive config object
    $config = @{{
        capture_version = "2.0"
        captured_at = (Get-Date).ToString('o')
        source_host = $env:COMPUTERNAME
    }}

    # === VM Base Properties ===
    $config.vm = @{{
        id = $vm.Id.ToString()
        name = $vm.Name
        generation = $vm.Generation
        version = $vm.Version
        state = $vm.State.ToString()
        path = $vm.Path
        configurationLocation = $vm.ConfigurationLocation
        snapshotFileLocation = $vm.SnapshotFileLocation
        smartPagingFilePath = $vm.SmartPagingFilePath
        checkpointType = $vm.CheckpointType.ToString()
        automaticStartAction = $vm.AutomaticStartAction.ToString()
        automaticStartDelay = $vm.AutomaticStartDelay
        automaticStopAction = $vm.AutomaticStopAction.ToString()
        automaticCriticalErrorAction = $vm.AutomaticCriticalErrorAction.ToString()
        automaticCriticalErrorActionTimeout = $vm.AutomaticCriticalErrorActionTimeout
        lockOnDisconnect = $vm.LockOnDisconnect.ToString()
        notes = $vm.Notes
        parentSnapshotId = if ($vm.ParentSnapshotId) {{ $vm.ParentSnapshotId.ToString() }} else {{ $null }}
        parentSnapshotName = $vm.ParentSnapshotName
    }}

    # === Firmware Settings (Gen2 only) ===
    $config.firmware = $null
    if ($vm.Generation -eq 2) {{
        try {{
            $fw = Get-VMFirmware -VM $vm -ErrorAction Stop
            $config.firmware = @{{
                secureBootEnabled = $fw.SecureBoot -eq 'On'
                secureBootTemplate = $fw.SecureBootTemplate
                secureBootTemplateId = if ($fw.SecureBootTemplateId) {{
                    $fw.SecureBootTemplateId.ToString()
                }} else {{ $null }}
                preferredNetworkBootProtocol = $fw.PreferredNetworkBootProtocol.ToString()
                consoleMode = $fw.ConsoleMode.ToString()
                pauseAfterBootFailure = $fw.PauseAfterBootFailure -eq 'On'
                # Capture boot order as array of device types
                bootOrder = @($fw.BootOrder | ForEach-Object {{
                    @{{
                        bootType = $_.BootType.ToString()
                        device = if ($_.Device) {{ $_.Device.ToString() }} else {{ $null }}
                    }}
                }})
            }}
        }} catch {{
            # Firmware query failed, leave as null
        }}
    }}

    # === Processor Settings ===
    $proc = Get-VMProcessor -VM $vm
    $config.processor = @{{
        count = $proc.Count
        reserve = $proc.Reserve
        maximum = $proc.Maximum
        relativeWeight = $proc.RelativeWeight
        compatibilityForMigrationEnabled = $proc.CompatibilityForMigrationEnabled
        compatibilityForOlderOperatingSystemsEnabled = $proc.CompatibilityForOlderOperatingSystemsEnabled
        exposeVirtualizationExtensions = $proc.ExposeVirtualizationExtensions
        enableHostResourceProtection = $proc.EnableHostResourceProtection
        hwThreadCountPerCore = $proc.HwThreadCountPerCore
        maximumCountPerNumaNode = $proc.MaximumCountPerNumaNode
        maximumCountPerNumaSocket = $proc.MaximumCountPerNumaSocket
        resourcePoolName = $proc.ResourcePoolName
    }}

    # === Memory Settings ===
    $mem = Get-VMMemory -VM $vm
    $config.memory = @{{
        dynamicMemoryEnabled = $mem.DynamicMemoryEnabled
        startupBytes = $mem.Startup
        minimumBytes = $mem.Minimum
        maximumBytes = $mem.Maximum
        buffer = $mem.Buffer
        priority = $mem.Priority
        maximumAmountPerNumaNodeBytes = $mem.MaximumAmountPerNumaNode
        resourcePoolName = $mem.ResourcePoolName
    }}

    # === Network Adapters ===
    $config.networkAdapters = @()
    $nics = Get-VMNetworkAdapter -VM $vm
    foreach ($nic in $nics) {{
        $nicConfig = @{{
            name = $nic.Name
            id = $nic.Id
            switchName = $nic.SwitchName
            macAddress = $nic.MacAddress
            dynamicMacAddressEnabled = $nic.DynamicMacAddressEnabled
            isManagementOs = $nic.IsManagementOs
            isLegacy = $nic.IsLegacy

            # VLAN settings
            vlanAccess = @{{
                accessVlanId = $nic.VlanSetting.AccessVlanId
                operationMode = if ($nic.VlanSetting.OperationMode) {{
                    $nic.VlanSetting.OperationMode.ToString()
                }} else {{ "Untagged" }}
                nativeVlanId = $nic.VlanSetting.NativeVlanId
                allowedVlanIdList = $nic.VlanSetting.AllowedVlanIdList
            }}

            # Bandwidth settings
            bandwidth = @{{
                maximumBandwidth = $nic.BandwidthSetting.MaximumBandwidth
                minimumBandwidthAbsolute = $nic.BandwidthSetting.MinimumBandwidthAbsolute
                minimumBandwidthWeight = $nic.BandwidthSetting.MinimumBandwidthWeight
            }}

            # Security settings
            macAddressSpoofing = $nic.MacAddressSpoofing.ToString()
            dhcpGuard = $nic.DhcpGuard.ToString()
            routerGuard = $nic.RouterGuard.ToString()
            allowTeaming = $nic.AllowTeaming.ToString()
            portMirroring = $nic.PortMirroringMode.ToString()

            # VMQ and IOV
            vmqWeight = $nic.VmqWeight
            iovWeight = $nic.IovWeight
            iovInterruptModeration = if ($nic.IovInterruptModeration) {{
                $nic.IovInterruptModeration.ToString()
            }} else {{ $null }}
            iovQueuePairsRequested = $nic.IovQueuePairsRequested

            # Other settings
            ieeePriorityTag = $nic.IeeePriorityTag.ToString()
            deviceNaming = $nic.DeviceNaming.ToString()
        }}
        $config.networkAdapters += $nicConfig
    }}

    # === Hard Disk Drives ===
    $config.hardDrives = @()
    $hdds = Get-VMHardDiskDrive -VM $vm
    foreach ($hdd in $hdds) {{
        $hddConfig = @{{
            controllerType = $hdd.ControllerType.ToString()
            controllerNumber = $hdd.ControllerNumber
            controllerLocation = $hdd.ControllerLocation
            path = $hdd.Path
            diskNumber = $hdd.DiskNumber
            supportPersistentReservations = $hdd.SupportPersistentReservations
            maximumIOPS = $hdd.MaximumIOPS
            minimumIOPS = $hdd.MinimumIOPS
            qosPolicyID = if ($hdd.QosPolicyID) {{ $hdd.QosPolicyID.ToString() }} else {{ $null }}
            # Get VHD info for size
            vhdInfo = $null
        }}
        if ($hdd.Path -and (Test-Path $hdd.Path -ErrorAction SilentlyContinue)) {{
            try {{
                $vhd = Get-VHD -Path $hdd.Path -ErrorAction SilentlyContinue
                if ($vhd) {{
                    $hddConfig.vhdInfo = @{{
                        vhdType = $vhd.VhdType.ToString()
                        fileSize = $vhd.FileSize
                        size = $vhd.Size
                        blockSize = $vhd.BlockSize
                        logicalSectorSize = $vhd.LogicalSectorSize
                        physicalSectorSize = $vhd.PhysicalSectorSize
                        parentPath = $vhd.ParentPath
                    }}
                }}
            }} catch {{ }}
        }}
        $config.hardDrives += $hddConfig
    }}

    # === DVD Drives ===
    $config.dvdDrives = @()
    $dvds = Get-VMDvdDrive -VM $vm
    foreach ($dvd in $dvds) {{
        $config.dvdDrives += @{{
            controllerType = $dvd.ControllerType.ToString()
            controllerNumber = $dvd.ControllerNumber
            controllerLocation = $dvd.ControllerLocation
            path = $dvd.Path
        }}
    }}

    # === SCSI Controllers ===
    $config.scsiControllers = @()
    $scsics = Get-VMScsiController -VM $vm
    foreach ($scsi in $scsics) {{
        $config.scsiControllers += @{{
            controllerNumber = $scsi.ControllerNumber
        }}
    }}

    # === Integration Services ===
    $config.integrationServices = @{{}}
    $intSvcs = Get-VMIntegrationService -VM $vm
    foreach ($svc in $intSvcs) {{
        $config.integrationServices[$svc.Name] = $svc.Enabled
    }}

    # === Security Settings ===
    try {{
        $sec = Get-VMSecurity -VM $vm -ErrorAction SilentlyContinue
        $config.security = @{{
            tpmEnabled = $sec.TpmEnabled
            encryptStateAndVmMigrationTraffic = $sec.EncryptStateAndVmMigrationTraffic
            virtualizationBasedSecurityOptOut = $sec.VirtualizationBasedSecurityOptOut
            shieldingRequested = $sec.Shielded
        }}
    }} catch {{
        $config.security = @{{
            tpmEnabled = $false
            encryptStateAndVmMigrationTraffic = $false
            virtualizationBasedSecurityOptOut = $false
            shieldingRequested = $false
        }}
    }}

    # Check if vTPM is actually enabled
    try {{
        $tpm = Get-VMTPM -VM $vm -ErrorAction SilentlyContinue
        if ($tpm) {{
            $config.security.tpmEnabled = $true
        }}
    }} catch {{ }}

    # === GPU Partition Adapters (GPU-P) ===
    $config.gpuPartitions = @()
    try {{
        $gpus = Get-VMGpuPartitionAdapter -VM $vm -ErrorAction SilentlyContinue
        foreach ($gpu in $gpus) {{
            $config.gpuPartitions += @{{
                instancePath = $gpu.InstancePath
                minPartitionVRAM = $gpu.MinPartitionVRAM
                maxPartitionVRAM = $gpu.MaxPartitionVRAM
                optimalPartitionVRAM = $gpu.OptimalPartitionVRAM
                minPartitionEncode = $gpu.MinPartitionEncode
                maxPartitionEncode = $gpu.MaxPartitionEncode
                optimalPartitionEncode = $gpu.OptimalPartitionEncode
                minPartitionDecode = $gpu.MinPartitionDecode
                maxPartitionDecode = $gpu.MaxPartitionDecode
                optimalPartitionDecode = $gpu.OptimalPartitionDecode
                minPartitionCompute = $gpu.MinPartitionCompute
                maxPartitionCompute = $gpu.MaxPartitionCompute
                optimalPartitionCompute = $gpu.OptimalPartitionCompute
            }}
        }}
    }} catch {{ }}

    # === Assignable Devices (DDA - GPU Passthrough) ===
    $config.assignableDevices = @()
    try {{
        $devices = Get-VMAssignableDevice -VM $vm -ErrorAction SilentlyContinue
        foreach ($dev in $devices) {{
            $config.assignableDevices += @{{
                instancePath = $dev.InstancePath
                locationPath = $dev.LocationPath
                resourcePoolName = $dev.ResourcePoolName
            }}
        }}
    }} catch {{ }}

    # === COM Ports ===
    $config.comPorts = @()
    try {{
        $coms = Get-VMComPort -VM $vm -ErrorAction SilentlyContinue
        foreach ($com in $coms) {{
            $config.comPorts += @{{
                number = $com.Number
                path = $com.Path
            }}
        }}
    }} catch {{ }}

    # === Fibre Channel Adapters ===
    $config.fibreChannelAdapters = @()
    try {{
        $fcas = Get-VMFibreChannelHba -VM $vm -ErrorAction SilentlyContinue
        foreach ($fca in $fcas) {{
            $config.fibreChannelAdapters += @{{
                worldWideNodeName = $fca.WorldWideNodeName
                worldWidePortNameSetA = $fca.WorldWidePortNameSetA
                worldWidePortNameSetB = $fca.WorldWidePortNameSetB
                sanName = $fca.SanName
            }}
        }}
    }} catch {{ }}

    # === Cluster Status ===
    $config.cluster = @{{
        isClustered = $vm.IsClustered
        clusterName = $null
        resourceGroupName = $null
        preferredOwners = @()
        possibleOwners = @()
        antiAffinityClassNames = @()
    }}

    if ($vm.IsClustered) {{
        try {{
            $clusterRes = Get-ClusterResource -Name "Virtual Machine $($vm.Name)" -ErrorAction SilentlyContinue
            if (-not $clusterRes) {{
                $clusterRes = Get-ClusterResource | Where-Object {{
                    $_.ResourceType -eq 'Virtual Machine' -and
                    (Get-ClusterParameter -InputObject $_ -Name VmId -ErrorAction SilentlyContinue
                    ).Value -eq $vm.Id.ToString()
                }} | Select-Object -First 1
            }}

            if ($clusterRes) {{
                $config.cluster.clusterName = (Get-Cluster).Name
                $config.cluster.resourceGroupName = $clusterRes.OwnerGroup.Name
                $config.cluster.preferredOwners = @(
                    $clusterRes.OwnerGroup.PreferredOwnerNodes | ForEach-Object {{ $_.Name }}
                )
                $config.cluster.possibleOwners = @(
                    $clusterRes.OwnerNodes | ForEach-Object {{ $_.Name }}
                )

                # Get anti-affinity
                $aaParam = Get-ClusterParameter -InputObject $clusterRes `
                    -Name AntiAffinityClassNames -ErrorAction SilentlyContinue
                if ($aaParam -and $aaParam.Value) {{
                    $config.cluster.antiAffinityClassNames = @($aaParam.Value -split ',')
                }}
            }}
        }} catch {{ }}
    }}

    # === Replication Settings ===
    $config.replication = @{{
        enabled = $false
        mode = $null
        replicaServerName = $null
        primaryServerName = $null
    }}
    try {{
        $repl = Get-VMReplication -VM $vm -ErrorAction SilentlyContinue
        if ($repl) {{
            $config.replication = @{{
                enabled = $true
                mode = $repl.ReplicationMode.ToString()
                replicaServerName = $repl.ReplicaServerName
                primaryServerName = $repl.PrimaryServerName
                replicationFrequencySec = $repl.ReplicationFrequencySec
                replicationState = $repl.ReplicationState.ToString()
            }}
        }}
    }} catch {{ }}

    # === Resource Metering ===
    $config.resourceMetering = @{{
        enabled = $false
    }}
    try {{
        $meter = Measure-VM -VM $vm -ErrorAction SilentlyContinue
        if ($meter) {{
            $config.resourceMetering.enabled = $true
        }}
    }} catch {{ }}

    # Convert to JSON and output
    $config | ConvertTo-Json -Depth 10 -Compress

}} catch {{
    @{{ Error = $_.Exception.Message }} | ConvertTo-Json -Compress
    exit 1
}}
"""
        rc, stdout, stderr = self._run_powershell_large(script, timeout=120)

        if rc != 0:
            logger.error(f"Failed to capture VM config for {vm_name}: {stderr}")
            return None

        try:
            config = json.loads(stdout.strip())
            if "Error" in config:
                logger.error(f"VM config capture error: {config['Error']}")
                return None
            logger.info(f"Captured comprehensive config for VM: {vm_name}")
            return config
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse VM config JSON: {e}")
            logger.debug(f"Raw output: {stdout[:500]}")
            return None

    def apply_vm_config(
        self,
        vm_name: str,
        config: dict[str, Any],
        network_mapping: dict[str, str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Apply comprehensive configuration to an existing VM.

        This method applies all captured settings from a vm_full_config.json
        to an existing VM. The VM should already exist and be in Off state.

        Settings are applied in the correct order to avoid conflicts.
        Host-specific settings (GPU passthrough, etc.) are skipped with warnings.

        Args:
            vm_name: Name of the VM to configure
            config: Configuration dict from capture_vm_config or vm_full_config.json
            network_mapping: Optional dict mapping old switch names to new ones

        Returns:
            Tuple of (success, list of warnings/info messages)
        """
        if not config:
            return False, ["No configuration provided"]

        # Convert config to JSON for PowerShell
        config_json = json.dumps(config)
        # Escape for PowerShell here-string (replace single quotes)
        safe_config_json = config_json.replace("'", "''")

        # Build network mapping JSON
        network_map_json = json.dumps(network_mapping or {})
        safe_network_map = network_map_json.replace("'", "''")

        script = f"""
$ErrorActionPreference = 'Stop'
$vmName = '{vm_name}'
$configJson = @'
{safe_config_json}
'@
$networkMapJson = @'
{safe_network_map}
'@

$warnings = @()
$config = $configJson | ConvertFrom-Json
$networkMap = $networkMapJson | ConvertFrom-Json

try {{
    $vm = Get-VM -Name $vmName -ErrorAction Stop

    # Verify VM is off (required for most settings)
    if ($vm.State -ne 'Off') {{
        $warnings += "VM must be off to apply all settings. Current state: $($vm.State)"
        # Try to apply what we can while running
    }}

    $isOff = $vm.State -eq 'Off'

    # === Apply VM Base Settings ===
    try {{
        $setVmParams = @{{ VMName = $vmName }}

        if ($config.vm.notes) {{
            $setVmParams.Notes = $config.vm.notes
        }}
        if ($config.vm.checkpointType) {{
            $setVmParams.CheckpointType = $config.vm.checkpointType
        }}
        if ($config.vm.automaticStartAction) {{
            $setVmParams.AutomaticStartAction = $config.vm.automaticStartAction
        }}
        if ($null -ne $config.vm.automaticStartDelay) {{
            $setVmParams.AutomaticStartDelay = $config.vm.automaticStartDelay
        }}
        if ($config.vm.automaticStopAction) {{
            $setVmParams.AutomaticStopAction = $config.vm.automaticStopAction
        }}
        if ($config.vm.automaticCriticalErrorAction) {{
            $setVmParams.AutomaticCriticalErrorAction = $config.vm.automaticCriticalErrorAction
        }}
        if ($null -ne $config.vm.automaticCriticalErrorActionTimeout) {{
            $setVmParams.AutomaticCriticalErrorActionTimeout = $config.vm.automaticCriticalErrorActionTimeout
        }}
        if ($config.vm.lockOnDisconnect) {{
            $setVmParams.LockOnDisconnect = $config.vm.lockOnDisconnect
        }}

        Set-VM @setVmParams -ErrorAction Stop
    }} catch {{
        $warnings += "Failed to apply VM base settings: $_"
    }}

    # === Apply Memory Settings ===
    try {{
        $memParams = @{{ VMName = $vmName }}

        if ($null -ne $config.memory.dynamicMemoryEnabled) {{
            $memParams.DynamicMemoryEnabled = $config.memory.dynamicMemoryEnabled
        }}
        if ($config.memory.startupBytes) {{
            $memParams.StartupBytes = [long]$config.memory.startupBytes
        }}
        if ($config.memory.dynamicMemoryEnabled) {{
            if ($config.memory.minimumBytes) {{
                $memParams.MinimumBytes = [long]$config.memory.minimumBytes
            }}
            if ($config.memory.maximumBytes) {{
                $memParams.MaximumBytes = [long]$config.memory.maximumBytes
            }}
            if ($null -ne $config.memory.buffer) {{
                $memParams.Buffer = $config.memory.buffer
            }}
            if ($null -ne $config.memory.priority) {{
                $memParams.Priority = $config.memory.priority
            }}
        }}

        Set-VMMemory @memParams -ErrorAction Stop
    }} catch {{
        $warnings += "Failed to apply memory settings: $_"
    }}

    # === Apply Processor Settings ===
    try {{
        $procParams = @{{ VMName = $vmName }}

        if ($config.processor.count) {{
            $procParams.Count = $config.processor.count
        }}
        if ($null -ne $config.processor.reserve) {{
            $procParams.Reserve = $config.processor.reserve
        }}
        if ($null -ne $config.processor.maximum) {{
            $procParams.Maximum = $config.processor.maximum
        }}
        if ($null -ne $config.processor.relativeWeight) {{
            $procParams.RelativeWeight = $config.processor.relativeWeight
        }}
        if ($null -ne $config.processor.compatibilityForMigrationEnabled) {{
            $procParams.CompatibilityForMigrationEnabled = $config.processor.compatibilityForMigrationEnabled
        }}
        if ($null -ne $config.processor.compatibilityForOlderOperatingSystemsEnabled) {{
            $procParams.CompatibilityForOlderOperatingSystemsEnabled = `
                $config.processor.compatibilityForOlderOperatingSystemsEnabled
        }}
        # ExposeVirtualizationExtensions requires VM to be off
        if ($isOff -and $null -ne $config.processor.exposeVirtualizationExtensions) {{
            $procParams.ExposeVirtualizationExtensions = $config.processor.exposeVirtualizationExtensions
        }}
        if ($null -ne $config.processor.maximumCountPerNumaNode) {{
            $procParams.MaximumCountPerNumaNode = $config.processor.maximumCountPerNumaNode
        }}
        if ($null -ne $config.processor.maximumCountPerNumaSocket) {{
            $procParams.MaximumCountPerNumaSocket = $config.processor.maximumCountPerNumaSocket
        }}

        Set-VMProcessor @procParams -ErrorAction Stop
    }} catch {{
        $warnings += "Failed to apply processor settings: $_"
    }}

    # === Apply Firmware Settings (Gen2 only) ===
    if ($vm.Generation -eq 2 -and $config.firmware) {{
        try {{
            $fwParams = @{{ VMName = $vmName }}

            # Secure Boot
            if ($null -ne $config.firmware.secureBootEnabled) {{
                if ($config.firmware.secureBootEnabled) {{
                    $fwParams.EnableSecureBoot = 'On'

                    # Apply the correct Secure Boot template
                    if ($config.firmware.secureBootTemplate) {{
                        $fwParams.SecureBootTemplate = $config.firmware.secureBootTemplate
                    }}
                }} else {{
                    $fwParams.EnableSecureBoot = 'Off'
                }}
            }}

            if ($config.firmware.preferredNetworkBootProtocol) {{
                $fwParams.PreferredNetworkBootProtocol = $config.firmware.preferredNetworkBootProtocol
            }}

            if ($config.firmware.consoleMode) {{
                $fwParams.ConsoleMode = $config.firmware.consoleMode
            }}

            if ($null -ne $config.firmware.pauseAfterBootFailure) {{
                if ($config.firmware.pauseAfterBootFailure) {{
                    $fwParams.PauseAfterBootFailure = 'On'
                }} else {{
                    $fwParams.PauseAfterBootFailure = 'Off'
                }}
            }}

            Set-VMFirmware @fwParams -ErrorAction Stop
        }} catch {{
            $warnings += "Failed to apply firmware settings: $_"
        }}
    }}

    # === Configure Network Adapters ===
    if ($config.networkAdapters -and $config.networkAdapters.Count -gt 0) {{
        try {{
            # Get current adapters
            $currentAdapters = @(Get-VMNetworkAdapter -VM $vm)

            # Remove all current adapters if VM is off (we'll recreate them)
            if ($isOff) {{
                foreach ($adapter in $currentAdapters) {{
                    Remove-VMNetworkAdapter -VMNetworkAdapter $adapter -ErrorAction SilentlyContinue
                }}
            }}

            # Add and configure adapters from config
            foreach ($nicConfig in $config.networkAdapters) {{
                $nicName = $nicConfig.name
                if (-not $nicName) {{ $nicName = "Network Adapter" }}

                # Determine switch name (apply mapping if provided)
                $switchName = $nicConfig.switchName
                if ($switchName -and $networkMap.$switchName) {{
                    $switchName = $networkMap.$switchName
                    $warnings += "Mapped switch '$($nicConfig.switchName)' to '$switchName'"
                }}

                # Check if switch exists
                $switchExists = $false
                if ($switchName) {{
                    $switch = Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue
                    $switchExists = $null -ne $switch
                    if (-not $switchExists) {{
                        $warnings += "Virtual switch '$switchName' not found on this host"
                        $switchName = $null
                    }}
                }}

                if ($isOff) {{
                    # Create new adapter
                    $addParams = @{{
                        VMName = $vmName
                        Name = $nicName
                    }}

                    if ($switchName) {{
                        $addParams.SwitchName = $switchName
                    }}

                    # Static MAC address
                    if (-not $nicConfig.dynamicMacAddressEnabled -and $nicConfig.macAddress) {{
                        $addParams.StaticMacAddress = $nicConfig.macAddress
                    }}

                    Add-VMNetworkAdapter @addParams -ErrorAction Stop
                }}

                # Configure adapter settings
                $setParams = @{{
                    VMName = $vmName
                    Name = $nicName
                }}

                # MAC spoofing
                if ($nicConfig.macAddressSpoofing) {{
                    $setParams.MacAddressSpoofing = $nicConfig.macAddressSpoofing
                }}

                # DHCP Guard
                if ($nicConfig.dhcpGuard) {{
                    $setParams.DhcpGuard = $nicConfig.dhcpGuard
                }}

                # Router Guard
                if ($nicConfig.routerGuard) {{
                    $setParams.RouterGuard = $nicConfig.routerGuard
                }}

                # Allow Teaming
                if ($nicConfig.allowTeaming) {{
                    $setParams.AllowTeaming = $nicConfig.allowTeaming
                }}

                # VMQ Weight
                if ($null -ne $nicConfig.vmqWeight) {{
                    $setParams.VmqWeight = $nicConfig.vmqWeight
                }}

                # Bandwidth settings
                if ($nicConfig.bandwidth) {{
                    if ($nicConfig.bandwidth.maximumBandwidth) {{
                        $setParams.MaximumBandwidth = $nicConfig.bandwidth.maximumBandwidth
                    }}
                    if ($nicConfig.bandwidth.minimumBandwidthWeight) {{
                        $setParams.MinimumBandwidthWeight = $nicConfig.bandwidth.minimumBandwidthWeight
                    }}
                }}

                try {{
                    Set-VMNetworkAdapter @setParams -ErrorAction Stop
                }} catch {{
                    $warnings += "Failed to configure adapter '$nicName': $_"
                }}

                # Apply VLAN settings if configured
                if ($nicConfig.vlanAccess -and $nicConfig.vlanAccess.accessVlanId) {{
                    try {{
                        Set-VMNetworkAdapterVlan -VMName $vmName -VMNetworkAdapterName $nicName `
                            -Access -VlanId $nicConfig.vlanAccess.accessVlanId -ErrorAction Stop
                    }} catch {{
                        $warnings += "Failed to set VLAN for '$nicName': $_"
                    }}
                }}
            }}
        }} catch {{
            $warnings += "Failed to configure network adapters: $_"
        }}
    }}

    # === Apply Integration Services ===
    if ($config.integrationServices) {{
        try {{
            $config.integrationServices.PSObject.Properties | ForEach-Object {{
                $svcName = $_.Name
                $enabled = $_.Value
                try {{
                    if ($enabled) {{
                        Enable-VMIntegrationService -VMName $vmName -Name $svcName -ErrorAction Stop
                    }} else {{
                        Disable-VMIntegrationService -VMName $vmName -Name $svcName -ErrorAction Stop
                    }}
                }} catch {{
                    # Service might not exist on this host
                }}
            }}
        }} catch {{
            $warnings += "Failed to apply integration services: $_"
        }}
    }}

    # === Apply Security Settings ===
    if ($config.security) {{
        try {{
            $secParams = @{{ VMName = $vmName }}

            if ($null -ne $config.security.encryptStateAndVmMigrationTraffic) {{
                $secParams.EncryptStateAndVmMigrationTraffic = $config.security.encryptStateAndVmMigrationTraffic
            }}

            # VBS opt-out requires VM to be off
            if ($isOff -and $null -ne $config.security.virtualizationBasedSecurityOptOut) {{
                $secParams.VirtualizationBasedSecurityOptOut = $config.security.virtualizationBasedSecurityOptOut
            }}

            Set-VMSecurity @secParams -ErrorAction SilentlyContinue
        }} catch {{
            $warnings += "Failed to apply security settings: $_"
        }}

        # Enable vTPM if it was enabled in original (with new local key protector)
        if ($config.security.tpmEnabled -and $vm.Generation -eq 2 -and $isOff) {{
            try {{
                Set-VMKeyProtector -VMName $vmName -NewLocalKeyProtector -ErrorAction Stop
                Enable-VMTPM -VMName $vmName -ErrorAction Stop
                $warnings += "vTPM enabled with new local key protector (original keys cannot be migrated)"
            }} catch {{
                $warnings += "Failed to enable vTPM: $_"
            }}
        }}
    }}

    # === Handle GPU Partition Adapters (GPU-P) ===
    if ($config.gpuPartitions -and $config.gpuPartitions.Count -gt 0) {{
        $warnings += "GPU partitions detected in backup - these are host-specific and cannot be automatically restored"
        $warnings += "Original GPU partitions: $($config.gpuPartitions.Count)"
        # We don't attempt to restore these as the GPU instance paths are host-specific
    }}

    # === Handle Assignable Devices (DDA/GPU Passthrough) ===
    if ($config.assignableDevices -and $config.assignableDevices.Count -gt 0) {{
        $warnings += "GPU/Device passthrough (DDA) detected - host-specific, cannot auto-restore"
        $warnings += "Original passthrough devices: $($config.assignableDevices.Count)"
        foreach ($dev in $config.assignableDevices) {{
            $warnings += "  - Device: $($dev.instancePath)"
        }}
        # We don't attempt to restore these as the device instance paths are host-specific
    }}

    # === Handle Cluster Settings ===
    if ($config.cluster -and $config.cluster.isClustered) {{
        $warnings += "Original VM was clustered - cluster configuration must be manually re-applied"
        $warnings += "Original cluster: $($config.cluster.clusterName)"
        $warnings += "Original resource group: $($config.cluster.resourceGroupName)"
    }}

    # === Handle Replication ===
    if ($config.replication -and $config.replication.enabled) {{
        $warnings += "Original VM had replication enabled - replication must be manually reconfigured"
        $warnings += "Original replica server: $($config.replication.replicaServerName)"
    }}

    # Output result
    @{{
        Success = $true
        Warnings = $warnings
    }} | ConvertTo-Json -Compress

}} catch {{
    @{{
        Success = $false
        Error = $_.Exception.Message
        Warnings = $warnings
    }} | ConvertTo-Json -Compress
}}
"""
        rc, stdout, stderr = self._run_powershell_large(script, timeout=300)

        if rc != 0:
            logger.error(f"Failed to apply VM config: {stderr}")
            return False, [f"PowerShell error: {stderr}"]

        try:
            result = json.loads(stdout.strip())
            warnings = result.get("Warnings", [])

            if result.get("Success"):
                logger.info(f"Applied config to VM {vm_name} with {len(warnings)} warnings")
                return True, warnings
            else:
                error = result.get("Error", "Unknown error")
                logger.error(f"Failed to apply VM config: {error}")
                return False, [error] + warnings
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse apply config result: {e}")
            return False, [f"Failed to parse result: {e}"]

    def export_vm(
        self,
        vm_name: str,
        export_path: str,
        capture_live_state: str | None = None,
        timeout: int = 3600,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
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
            smb_domain: Optional SMB domain for network path authentication

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
                # Build username with domain if provided (domain\username format)
                full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username

                script = f"""
$ErrorActionPreference = 'Stop'
$vmName = '{vm_name}'
$finalPath = '{export_path}'
$smbUnc = '{smb_unc}'
$smbUser = '{full_username}'
$smbPass = '{safe_password}'

# Function to test if a path is accessible by SYSTEM/VMMS for export
# Returns: 'local' for drive letter paths, 'smb' for UNC paths that VM uses, $null for inaccessible
function Test-ExportPath {{
    param([string]$Path)
    if ([string]::IsNullOrEmpty($Path)) {{ return $null }}

    # Drive letter paths are local/SAN/iSCSI - always accessible by SYSTEM
    if ($Path -match '^[A-Za-z]:') {{ return 'local' }}

    # UNC paths - if the VM is using this storage, Hyper-V must have persistent access
    # (either via machine account or stored credentials). We can try to use it.
    if ($Path.StartsWith('\\')) {{ return 'smb' }}

    return $null
}}

# Function to test if we can write to a path
function Test-WritablePath {{
    param([string]$Path)
    try {{
        if (-not (Test-Path $Path)) {{
            # Try to create it
            New-Item -ItemType Directory -Path $Path -Force -ErrorAction Stop | Out-Null
        }}
        # Try to create a temp file to verify write access
        $testFile = Join-Path $Path ".backer_write_test_$([System.Guid]::NewGuid().ToString())"
        [System.IO.File]::WriteAllText($testFile, "test")
        Remove-Item $testFile -Force -ErrorAction SilentlyContinue
        return $true
    }} catch {{
        return $false
    }}
}}

# Function to get free space on a path (in bytes)
function Get-PathFreeSpace {{
    param([string]$Path)
    try {{
        # For UNC paths, use .NET to get disk space
        if ($Path.StartsWith('\\')) {{
            $drive = New-Object System.IO.DriveInfo((Get-Item $Path).PSDrive.Root)
            return $drive.AvailableFreeSpace
        }}
        # For local paths, use PSDrive
        $drive = (Get-Item $Path -ErrorAction Stop).PSDrive
        if ($drive -and $drive.Free) {{
            return $drive.Free
        }}
        # Fallback to WMI for local drives
        $driveLetter = $Path.Substring(0, 2)
        $disk = Get-WmiObject Win32_LogicalDisk -Filter "DeviceID='$driveLetter'" -ErrorAction SilentlyContinue
        if ($disk) {{
            return $disk.FreeSpace
        }}
    }} catch {{}}
    return 0
}}

# Get the VM to determine storage locations
$vm = Get-VM -Name $vmName -ErrorAction Stop

# Collect all potential storage paths (prioritized)
# We track both local and SMB paths - SMB paths the VM uses should be accessible
$candidatePaths = @()

# 1. VM's VHD locations (where the actual disk files are)
#    If VM runs from SMB storage, Hyper-V has persistent access to it
$vhds = $vm | Get-VMHardDiskDrive | Select-Object -ExpandProperty Path -ErrorAction SilentlyContinue
foreach ($vhd in $vhds) {{
    if ($vhd) {{
        $vhdDir = Split-Path -Parent $vhd
        $pathType = Test-ExportPath $vhdDir
        if ($pathType) {{
            $candidatePaths += @{{ Path = $vhdDir; Type = $pathType }}
        }}
    }}
}}

# 2. VM's SnapshotFileLocation (where checkpoints are stored)
$snapPath = $vm.SnapshotFileLocation
if ($snapPath) {{
    $pathType = Test-ExportPath $snapPath
    if ($pathType) {{
        $candidatePaths += @{{ Path = $snapPath; Type = $pathType }}
    }}
}}

# 3. VM's ConfigurationLocation
$configPath = $vm.ConfigurationLocation
if ($configPath) {{
    $pathType = Test-ExportPath $configPath
    if ($pathType) {{
        $candidatePaths += @{{ Path = $configPath; Type = $pathType }}
    }}
}}

# 4. Default Hyper-V paths from host settings
$vmHost = Get-VMHost -ErrorAction SilentlyContinue
if ($vmHost) {{
    if ($vmHost.VirtualMachinePath) {{
        $pathType = Test-ExportPath $vmHost.VirtualMachinePath
        if ($pathType) {{
            $candidatePaths += @{{ Path = $vmHost.VirtualMachinePath; Type = $pathType }}
        }}
    }}
    if ($vmHost.VirtualHardDiskPath) {{
        $pathType = Test-ExportPath $vmHost.VirtualHardDiskPath
        if ($pathType) {{
            $candidatePaths += @{{ Path = $vmHost.VirtualHardDiskPath; Type = $pathType }}
        }}
    }}
}}

# 5. Last resort - system drive temp (always local)
$candidatePaths += @{{ Path = "$env:SystemDrive\\BackerTemp"; Type = 'local' }}

# Remove duplicates while preserving order (based on Path)
$seen = @{{}}
$uniquePaths = @()
foreach ($item in $candidatePaths) {{
    if (-not $seen.ContainsKey($item.Path)) {{
        $seen[$item.Path] = $true
        $uniquePaths += $item
    }}
}}
$candidatePaths = $uniquePaths

# Estimate required space (sum of all VHD sizes + 10% buffer)
$requiredSpace = 0
foreach ($vhd in $vhds) {{
    if ($vhd -and (Test-Path $vhd -ErrorAction SilentlyContinue)) {{
        $requiredSpace += (Get-Item $vhd).Length
    }}
}}
$requiredSpace = [math]::Ceiling($requiredSpace * 1.1)  # 10% buffer
if ($requiredSpace -eq 0) {{
    # If we couldn't determine size, assume 50GB minimum
    $requiredSpace = 50GB
}}

# Find first path with enough space and write access
# Prefer local paths over SMB to avoid potential auth issues
$vmStoragePath = $null
$selectedPathType = $null

# First pass: try local paths
foreach ($item in $candidatePaths) {{
    if ($item.Type -ne 'local') {{ continue }}
    $path = $item.Path

    if (-not (Test-WritablePath $path)) {{ continue }}

    $freeSpace = Get-PathFreeSpace $path
    if ($freeSpace -ge $requiredSpace) {{
        $vmStoragePath = $path
        $selectedPathType = 'local'
        break
    }}
}}

# Second pass: try SMB paths if no local path found
if (-not $vmStoragePath) {{
    foreach ($item in $candidatePaths) {{
        if ($item.Type -ne 'smb') {{ continue }}
        $path = $item.Path

        if (-not (Test-WritablePath $path)) {{ continue }}

        $freeSpace = Get-PathFreeSpace $path
        if ($freeSpace -ge $requiredSpace) {{
            $vmStoragePath = $path
            $selectedPathType = 'smb'
            break
        }}
    }}
}}

if (-not $vmStoragePath) {{
    $pathsList = ($candidatePaths | ForEach-Object {{ "$($_.Path) ($($_.Type))" }}) -join ', '
    $reqGB = [math]::Round($requiredSpace / 1GB, 2)
    throw "No suitable storage with $reqGB GB free and write access. Checked: $pathsList"
}}

# Clean up any old BackerExport_* folders from previous failed backups
# This is critical for handling renamed VMs where previous exports may have left
# partial data with the old VM name structure
# We clean up in both the selected storage path AND the default Hyper-V paths
# to handle cases where different paths were used in previous backup attempts
$pathsToClean = @($vmStoragePath)
if ($vmHost.VirtualHardDiskPath -and $vmHost.VirtualHardDiskPath -ne $vmStoragePath) {{
    $pathsToClean += $vmHost.VirtualHardDiskPath
}}
if ($vmHost.VirtualMachinePath -and $vmHost.VirtualMachinePath -ne $vmStoragePath) {{
    $pathsToClean += $vmHost.VirtualMachinePath
}}

foreach ($cleanPath in $pathsToClean) {{
    $oldExportFolders = Get-ChildItem -Path $cleanPath -Directory -Filter 'BackerExport_*' -ErrorAction SilentlyContinue
    foreach ($folder in $oldExportFolders) {{
        try {{
            # Remove the folder and all contents
            Remove-Item -Path $folder.FullName -Recurse -Force -ErrorAction Stop
        }} catch {{
            # If standard removal fails, try robocopy trick to remove stubborn files
            $emptyDir = Join-Path $env:TEMP "EmptyDir_$([Guid]::NewGuid())"
            New-Item -ItemType Directory -Path $emptyDir -Force | Out-Null
            & robocopy $emptyDir $folder.FullName /MIR /NFL /NDL /NJH /NJS /nc /ns /np 2>&1 | Out-Null
            Remove-Item -Path $folder.FullName -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -Path $emptyDir -Force -ErrorAction SilentlyContinue
        }}
    }}
}}

# Create temp export directory with a fresh GUID
$tempExportId = [System.Guid]::NewGuid().ToString()
$localExportPath = Join-Path $vmStoragePath "BackerExport_$tempExportId"

# Ensure the export path is completely clean (remove if exists from failed run)
if (Test-Path $localExportPath) {{
    Remove-Item -Path $localExportPath -Recurse -Force -ErrorAction SilentlyContinue
}}
New-Item -ItemType Directory -Path $localExportPath -Force | Out-Null

try {{
    # Ensure destination VM folder doesn't exist (handles edge cases with renamed VMs)
    # Export-VM creates: $localExportPath/$vmName/Virtual Hard Disks/...
    # If VM was renamed but VHDX files kept old names, a prior failed export
    # could leave files that conflict with the new export
    $destVmFolder = Join-Path $localExportPath $vmName
    if (Test-Path $destVmFolder) {{
        Remove-Item -Path $destVmFolder -Recurse -Force -ErrorAction SilentlyContinue
    }}

    # Also check for any subfolder in the export path that might contain
    # VHD files with conflicting names (handles VM renames where VHDX names differ)
    Get-ChildItem -Path $localExportPath -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {{
            Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }}

    # Final verification: ensure the export path is completely empty
    # This catches any edge cases where cleanup didn't fully succeed
    $remainingItems = Get-ChildItem -Path $localExportPath -ErrorAction SilentlyContinue
    if ($remainingItems) {{
        # Something is still in the export folder - remove it
        $remainingItems | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }}

    # Check for checkpoints that might cause export issues
    # When a VM has checkpoints, Export-VM can fail with "file exists" if checkpoint
    # chains reference the same base VHD multiple times
    $checkpoints = Get-VMSnapshot -VMName $vmName -ErrorAction SilentlyContinue
    if ($checkpoints) {{
        # Remove all checkpoints before export to avoid duplicate file issues
        # This merges the checkpoint chain into the base VHD
        $checkpoints | Remove-VMSnapshot -IncludeAllChildSnapshots -ErrorAction Stop

        # Wait for merge operation to complete - check for AVHDX files disappearing
        $mergeWait = 0
        $maxWait = 300  # 5 minutes max wait for merge
        while ($mergeWait -lt $maxWait) {{
            Start-Sleep -Seconds 5
            $mergeWait += 5

            # Check if any AVHDX (differencing disks) still exist for this VM
            $vhdPaths = $vm | Get-VMHardDiskDrive | Select-Object -ExpandProperty Path
            $hasAvhdx = $false
            foreach ($path in $vhdPaths) {{
                if ($path -like '*.avhdx') {{
                    $hasAvhdx = $true
                    break
                }}
            }}

            if (-not $hasAvhdx) {{
                # No more differencing disks - merge is complete
                break
            }}
        }}

        # Re-fetch the VM to get updated disk paths after merge
        $vm = Get-VM -Name $vmName -ErrorAction Stop
    }}

    # Wait for VM to be in a stable state before exporting
    # VM might be in a transitional state (Starting, Stopping, etc.) which prevents export
    # Also check for ongoing export operations (another backup might be running)
    $stableStates = @('Running', 'Off', 'Saved', 'Paused')
    $stateWait = 0
    $maxStateWait = 120  # Wait up to 2 minutes for stable state
    while ($stateWait -lt $maxStateWait) {{
        $vm = Get-VM -Name $vmName -ErrorAction Stop
        $vmStatus = $vm.Status.ToString()

        # Check if another export is in progress
        if ($vmStatus -like '*Exporting*' -or $vmStatus -like '*export*') {{
            # Another export is running, wait for it to complete
            Start-Sleep -Seconds 5
            $stateWait += 5
            continue
        }}

        if ($stableStates -contains $vm.State.ToString()) {{
            # Also check that no operations are pending
            if ($vmStatus -eq 'Operating normally' -or $vmStatus -eq 'OK') {{
                break
            }}
        }}
        Start-Sleep -Seconds 2
        $stateWait += 2
    }}

    # Final check - if VM is still being exported after waiting, fail with clear message
    $vm = Get-VM -Name $vmName -ErrorAction Stop
    if ($vm.Status -like '*Exporting*' -or $vm.Status -like '*export*') {{
        throw "VM '$vmName' is currently being exported by another process. Please wait for that operation to complete."
    }}

    # Export VM to temp directory on local/SAN storage
    # Note: Export-VM creates $localExportPath/$vmName/... structure
    $exportError = $null
    try {{
        {export_cmd_base} $localExportPath{capture_param} -ErrorAction Stop
    }} catch {{
        $exportError = $_

        # If export failed with "file exists", try manual copy approach
        if ($_.Exception.Message -match '0x80070050|file exists|already exists') {{
            # Clean up failed export attempt
            $failedExportPath = Join-Path $localExportPath $vmName
            if (Test-Path $failedExportPath) {{
                Remove-Item -Path $failedExportPath -Recurse -Force -ErrorAction SilentlyContinue
            }}

            # Manual export: copy VHDs and VM config separately
            $vmExportPath = Join-Path $localExportPath $vmName
            $vhdDestPath = Join-Path $vmExportPath 'Virtual Hard Disks'
            $vmConfigPath = Join-Path $vmExportPath 'Virtual Machines'

            New-Item -ItemType Directory -Path $vhdDestPath -Force | Out-Null
            New-Item -ItemType Directory -Path $vmConfigPath -Force | Out-Null

            # Get all VHD paths (deduplicated), following AVHDX chains to base VHDs
            $vhdPaths = @{{}}
            $vm | Get-VMHardDiskDrive | ForEach-Object {{
                $vhdPath = $_.Path
                if ($vhdPath -and (Test-Path $vhdPath)) {{
                    # Follow the differencing disk chain to find the base VHD
                    $currentPath = $vhdPath
                    while ($currentPath) {{
                        $vhdInfo = Get-VHD -Path $currentPath -ErrorAction SilentlyContinue
                        if ($vhdInfo.ParentPath) {{
                            # This is a differencing disk, follow to parent
                            $currentPath = $vhdInfo.ParentPath
                        }} else {{
                            # This is the base VHD
                            break
                        }}
                    }}

                    # Copy the base VHD (not the differencing disk)
                    if ($currentPath -and (Test-Path $currentPath) -and -not $vhdPaths.ContainsKey($currentPath)) {{
                        $vhdPaths[$currentPath] = $true
                        $vhdName = Split-Path -Leaf $currentPath
                        $destVhd = Join-Path $vhdDestPath $vhdName
                        Copy-Item -Path $currentPath -Destination $destVhd -Force
                    }}
                }}
            }}

            # Export just the VM configuration (without VHDs)
            # This creates the vmcx file needed for import
            $vm | Export-VM -Path $localExportPath -ErrorAction SilentlyContinue

            # If config export also failed, create a minimal config
            if (-not (Test-Path (Join-Path $vmConfigPath '*.vmcx'))) {{
                # Save VM config info as JSON for manual recreation
                $vmInfo = @{{
                    Name = $vm.Name
                    Id = $vm.Id.ToString()
                    Generation = $vm.Generation
                    MemoryStartupBytes = $vm.MemoryStartupBytes
                    ProcessorCount = $vm.ProcessorCount
                    VHDs = @($vhdPaths.Keys)
                }}
                $vmInfo | ConvertTo-Json | Out-File (Join-Path $vmConfigPath 'vm_config.json')
            }}

            $exportError = $null  # Clear error since manual export succeeded
        }}
    }}

    if ($exportError) {{
        throw $exportError
    }}

    $vmExportPath = Join-Path $localExportPath $vmName
    if (-not (Test-Path $vmExportPath)) {{
        throw "Export completed but VM folder not found at $vmExportPath"
    }}

    # Calculate actual export size
    $size = (Get-ChildItem $vmExportPath -Recurse -ErrorAction SilentlyContinue |
             Measure-Object -Property Length -Sum).Sum

    # Now authenticate to SMB and copy files using credential-based PSDrive
    # This is more reliable than net use for PowerShell cmdlets
    $secPass = ConvertTo-SecureString $smbPass -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential($smbUser, $secPass)

    # Create a unique drive letter for the SMB mapping
    $driveName = "BackerSMB"
    # Remove existing drive if present
    if (Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue) {{
        Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
    }}

    # Map the SMB share with credentials
    try {{
        New-PSDrive -Name $driveName -PSProvider FileSystem -Root $smbUnc -Credential $cred -ErrorAction Stop | Out-Null
    }} catch {{
        throw "Failed to connect to SMB share $smbUnc : $_"
    }}

    try {{
        # Build destination path using the mapped drive
        # Extract the subpath after the share name
        $subPath = $finalPath.Substring($smbUnc.Length).TrimStart('\\')
        if ($subPath) {{
            $mappedFinalPath = "${{driveName}}:\\$subPath"
        }} else {{
            $mappedFinalPath = "${{driveName}}:\\"
        }}

        # Create destination directory if needed
        if (-not (Test-Path $mappedFinalPath)) {{
            New-Item -ItemType Directory -Path $mappedFinalPath -Force | Out-Null
        }}

        # Copy exported VM to network share
        $destVmPath = Join-Path $mappedFinalPath $vmName
        # Remove existing backup if present (for clean overwrite)
        if (Test-Path $destVmPath) {{
            Remove-Item -Path $destVmPath -Recurse -Force -ErrorAction SilentlyContinue
        }}

        # Copy with explicit error action to catch failures
        Copy-Item -Path $vmExportPath -Destination $mappedFinalPath -Recurse -Force -ErrorAction Stop

        # Verify the copy succeeded by checking destination exists and has content
        if (-not (Test-Path $destVmPath)) {{
            throw "Copy completed but destination folder not found: $destVmPath"
        }}

        # Verify we copied actual VM files (should have Virtual Hard Disks or Virtual Machines folder)
        $hasVhds = Test-Path (Join-Path $destVmPath 'Virtual Hard Disks')
        $hasVms = Test-Path (Join-Path $destVmPath 'Virtual Machines')
        if (-not $hasVhds -and -not $hasVms) {{
            throw "Copy completed but VM structure not found. Check permissions and disk space."
        }}

        # Calculate size of copied files
        $copiedSize = (Get-ChildItem $destVmPath -Recurse -ErrorAction SilentlyContinue |
                       Measure-Object -Property Length -Sum).Sum

        # Return the original UNC path (not the mapped drive path) for the result
        $uncDestPath = Join-Path $finalPath $vmName

        @{{
            Success = $true
            Path = $uncDestPath
            SizeBytes = if ($copiedSize) {{ $copiedSize }} else {{ 0 }}
            SourceSize = if ($size) {{ $size }} else {{ 0 }}
        }} | ConvertTo-Json
    }} finally {{
        # Cleanup PSDrive
        Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
    }}
}} finally {{
    # Always cleanup temp export directory
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
        # Use _run_powershell_large for UNC paths (large script) to avoid command line limits
        if is_unc_path and smb_username and smb_password:
            rc, stdout, stderr = self._run_powershell_large(script, timeout=timeout)
        else:
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
        smb_domain: str | None = None,
    ) -> dict[str, Any]:
        """Backup a Hyper-V VM.

        Args:
            vm_name: VM name to backup
            backup_path: Destination path (should be accessible from Hyper-V host)
            backup_mode: Backup mode (online, offline, checkpoint)
            progress_callback: Optional callback for progress updates
            smb_username: SMB username for network share authentication
            smb_password: SMB password for network share authentication
            smb_domain: SMB domain for network share authentication

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

            # Capture comprehensive VM configuration BEFORE any changes
            # This ensures we capture the exact state including firmware, GPU, etc.
            if progress_callback:
                progress_callback(
                    {"status": "capturing_config", "vm": vm_name}
                )

            vm_config = self.api.capture_vm_config(vm_name)
            if vm_config:
                result["vm_config"] = vm_config
                logger.info(f"Captured comprehensive config for VM: {vm_name}")
            else:
                logger.warning(
                    f"Failed to capture comprehensive config for VM: {vm_name}, "
                    "backup will continue but restore may be limited"
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
            # For SMB paths, this exports locally then copies to network
            is_smb_backup = backup_path.startswith("\\\\") and smb_username and smb_password
            if progress_callback:
                if is_smb_backup:
                    # For SMB: export to local temp, then copy to share
                    progress_callback({"status": "exporting", "vm": vm_name})
                else:
                    progress_callback({"status": "exporting", "vm": vm_name})

            # Create VM-centric backup directory structure:
            # {backup_path}/{vm_name}/{timestamp}/
            # This keeps all backups for a VM together for easier management
            timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
            vm_backup_path = f"{backup_path}\\{vm_name}\\{timestamp}"

            success, export_result = self.api.export_vm(
                vm_name,
                vm_backup_path,
                timeout=86400,  # 24 hour timeout for large VMs on slow connections
                smb_username=smb_username,
                smb_password=smb_password,
                smb_domain=smb_domain,
            )

            # Report verification phase after export completes
            if progress_callback:
                progress_callback({"status": "verifying", "vm": vm_name})

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

                # Save comprehensive VM config to backup location
                # This is saved alongside the VM export for full restore capability
                if vm_config:
                    if progress_callback:
                        progress_callback({"status": "saving_config", "vm": vm_name})

                    # Config goes in the timestamp folder (parent of VM export folder)
                    # Structure: {backup_path}/{vm_name}/{timestamp}/vm_full_config.json
                    #                                              /{vm_name}/Virtual Machines/...
                    config_save_path = f"{vm_backup_path}\\vm_full_config.json"
                    config_json = json.dumps(vm_config, indent=2)

                    # Save config file via PowerShell (handles SMB auth)
                    if is_smb_backup:
                        # For SMB paths, use authenticated write
                        unc_parts = backup_path.lstrip("\\").split("\\")
                        if len(unc_parts) >= 2:
                            smb_server = unc_parts[0]
                            smb_share = unc_parts[1]
                            smb_unc = f"\\\\{smb_server}\\{smb_share}"
                            safe_password = smb_password.replace("'", "''") if smb_password else ""
                            full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username

                            # Escape the JSON for PowerShell
                            safe_json = config_json.replace("'", "''")

                            save_config_script = f"""
$ErrorActionPreference = 'Stop'
$smbUnc = '{smb_unc}'
$smbUser = '{full_username}'
$smbPass = '{safe_password}'
$configPath = '{config_save_path}'
$configJson = @'
{safe_json}
'@

# Authenticate to SMB
$secPass = ConvertTo-SecureString $smbPass -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($smbUser, $secPass)

$driveName = "BackerCfg"
if (Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue) {{
    Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
}}

try {{
    New-PSDrive -Name $driveName -PSProvider FileSystem -Root $smbUnc -Credential $cred -ErrorAction Stop | Out-Null

    # Convert UNC path to mapped drive path
    $subPath = $configPath.Substring($smbUnc.Length).TrimStart('\\')
    $mappedPath = "${{driveName}}:\\$subPath"

    # Write config file
    $configJson | Out-File -FilePath $mappedPath -Encoding UTF8 -Force
    Write-Output "SUCCESS"
}} finally {{
    Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
}}
"""
                            rc, stdout, stderr = self.api._run_powershell_large(
                                save_config_script, timeout=60
                            )
                            if rc == 0 and "SUCCESS" in stdout:
                                result["config_saved"] = True
                                result["config_path"] = config_save_path
                                logger.info(f"Saved VM config to: {config_save_path}")
                            else:
                                logger.warning(
                                    f"Failed to save VM config to SMB: {stderr}"
                                )
                    else:
                        # Local path - direct write
                        save_config_script = f"""
$configPath = '{config_save_path}'
$configJson = @'
{config_json.replace("'", "''")}
'@
$configJson | Out-File -FilePath $configPath -Encoding UTF8 -Force
Write-Output "SUCCESS"
"""
                        rc, stdout, stderr = self.api._run_powershell(
                            save_config_script, timeout=60
                        )
                        if rc == 0 and "SUCCESS" in stdout:
                            result["config_saved"] = True
                            result["config_path"] = config_save_path
                            logger.info(f"Saved VM config to: {config_save_path}")
                        else:
                            logger.warning(
                                f"Failed to save VM config: {stderr}"
                            )

            else:
                result["errors"].append(f"Export failed: {export_result}")

            # Cleanup checkpoint if created
            if checkpoint_id:
                if progress_callback:
                    progress_callback({"status": "cleanup", "vm": vm_name})
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
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
        restore_mode: str = "auto",
        network_mapping: dict[str, str] | None = None,
        start_after_restore: bool = False,
    ) -> dict[str, Any]:
        """Restore a Hyper-V VM from a backup with comprehensive settings recovery.

        Supports multiple restore modes:
        - auto: Try in-place if VM exists, then import, then rebuild (recommended)
        - inplace: Replace VHD contents only, keep existing VM config (same host only)
        - import: Use Import-VM with .vmcx file (preserves most settings)
        - rebuild: Create new VM from VHDs and apply saved config (fallback)

        When a vm_full_config.json exists in the backup, it will be used to restore
        all VM settings including firmware (Secure Boot), network adapters, memory,
        processor settings, and more.

        For SMB/UNC paths, backup files are copied to local storage before import.

        Args:
            import_path: Path to the exported VM folder (timestamp folder containing VM export)
            vm_name: Optional new name for the VM after import
            restore_path: Optional destination path for VM configuration files
            vhd_destination_path: Optional destination for VHD files
            generate_new_id: Generate new VM ID (required for duplicate imports)
            progress_callback: Optional callback for progress updates
            smb_username: SMB username for network path authentication
            smb_password: SMB password for network path authentication
            smb_domain: SMB domain for network path authentication
            restore_mode: Restore strategy - auto, inplace, import, or rebuild
            network_mapping: Dict mapping old virtual switch names to new ones
            start_after_restore: Start the VM after successful restore

        Returns:
            Dict with restore result info including warnings about settings
        """
        from datetime import datetime as dt

        # Helper to extract single value from potential array (PowerShell JSON quirk)
        # When PowerShell's ConvertTo-Json gets a single-item array, it may serialize
        # inconsistently, and cluster configs can have duplicated values
        def _single_value(val: Any, default: Any = None) -> Any:
            if isinstance(val, list):
                return val[0] if val else default
            return val if val is not None else default

        started_at = dt.now()
        result: dict[str, Any] = {
            "success": False,
            "import_path": import_path,
            "vm_name": vm_name,
            "restore_mode": restore_mode,
            "errors": [],
            "warnings": [],
            "duration_seconds": 0,
        }

        try:
            if progress_callback:
                progress_callback({"status": "initializing", "path": import_path})

            # Check if source is a UNC path (network share)
            is_unc_path = import_path.startswith("\\\\")

            # Build SMB connection parameters
            smb_unc = ""
            full_username = ""
            safe_password = ""
            if is_unc_path and smb_username and smb_password:
                unc_parts = import_path.lstrip("\\").split("\\")
                if len(unc_parts) >= 2:
                    smb_server = unc_parts[0]
                    smb_share = unc_parts[1]
                    smb_unc = f"\\\\{smb_server}\\{smb_share}"
                    safe_password = smb_password.replace("'", "''")
                    full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username
                else:
                    result["errors"].append("Invalid UNC path format")
                    return result

            # Step 1: Load comprehensive config if available
            if progress_callback:
                progress_callback({"status": "loading_config", "path": import_path})

            full_config = None
            full_config = self._load_backup_config(
                import_path, smb_unc, full_username, safe_password
            )
            if full_config:
                cfg_ver = full_config.get('capture_version', '1.0')
                logger.info(f"Loaded comprehensive config from backup (version {cfg_ver})")
                result["config_loaded"] = True
            else:
                logger.info("No vm_full_config.json found, will use limited restore")
                result["config_loaded"] = False

            # Get VM name from config or folder
            target_vm_name = vm_name
            if not target_vm_name:
                if full_config and full_config.get("vm", {}).get("name"):
                    # Use _single_value in case config has array (PowerShell JSON quirk)
                    target_vm_name = _single_value(full_config["vm"]["name"])
                else:
                    # Extract from path - import_path is timestamp folder, VM name is subfolder
                    target_vm_name = self._get_vm_name_from_path(import_path)

            result["vm_name"] = target_vm_name

            # Step 2: Check if VM already exists (for in-place restore)
            existing_vm = self.api.get_guest(target_vm_name) if target_vm_name else None

            # Determine actual restore mode
            actual_mode = restore_mode
            if restore_mode == "auto":
                if existing_vm:
                    # VM exists - try in-place first
                    actual_mode = "inplace"
                    logger.info(f"Auto mode: VM '{target_vm_name}' exists, trying in-place restore")
                else:
                    # VM doesn't exist - try import
                    actual_mode = "import"
                    logger.info(f"Auto mode: VM '{target_vm_name}' doesn't exist, trying import")

            result["actual_mode"] = actual_mode

            # Step 3: Execute restore based on mode
            if actual_mode == "inplace":
                # In-place restore: replace VHDs only, keep VM config
                if not existing_vm:
                    if restore_mode == "auto":
                        # Fall back to import mode
                        actual_mode = "import"
                        result["actual_mode"] = actual_mode
                        logger.info("In-place not possible (VM doesn't exist), falling back to import")
                    else:
                        result["errors"].append(f"VM '{target_vm_name}' not found for in-place restore")
                        return result

            if actual_mode == "inplace":
                success, message = self._restore_inplace(
                    target_vm_name,
                    import_path,
                    smb_unc,
                    full_username,
                    safe_password,
                    progress_callback,
                )
                if success:
                    result["success"] = True
                    result["vm_id"] = existing_vm.vmid if existing_vm else None
                    result["warnings"].append("In-place restore completed - VM configuration unchanged")
                else:
                    if restore_mode == "auto":
                        # Fall back to rebuild
                        actual_mode = "rebuild"
                        result["actual_mode"] = actual_mode
                        result["warnings"].append(f"In-place restore failed ({message}), falling back to rebuild")
                    else:
                        result["errors"].append(f"In-place restore failed: {message}")
                        return result

            if actual_mode == "import":
                if progress_callback:
                    progress_callback({"status": "importing", "vm": target_vm_name})

                success, import_result = self._restore_import(
                    import_path,
                    target_vm_name,
                    restore_path,
                    vhd_destination_path,
                    generate_new_id,
                    smb_unc,
                    full_username,
                    safe_password,
                    progress_callback,
                )

                if success:
                    result["success"] = True
                    result["vm_id"] = import_result.get("vm_id")
                    result["vm_name"] = import_result.get("vm_name", target_vm_name)
                    if import_result.get("warnings"):
                        result["warnings"].extend(import_result["warnings"])
                else:
                    if restore_mode == "auto":
                        # Fall back to rebuild
                        actual_mode = "rebuild"
                        result["actual_mode"] = actual_mode
                        error_msg = import_result.get("error", "Unknown error")
                        result["warnings"].append(f"Import failed ({error_msg}), falling back to rebuild")
                        logger.info(f"Import failed, falling back to rebuild: {error_msg}")
                    else:
                        result["errors"].append(import_result.get("error", "Import failed"))
                        return result

            if actual_mode == "rebuild":
                if progress_callback:
                    progress_callback({"status": "rebuilding", "vm": target_vm_name})

                success, rebuild_result = self._restore_rebuild(
                    import_path,
                    target_vm_name,
                    full_config,
                    restore_path,
                    vhd_destination_path,
                    smb_unc,
                    full_username,
                    safe_password,
                    progress_callback,
                )

                if success:
                    result["success"] = True
                    result["vm_id"] = rebuild_result.get("vm_id")
                    result["vm_name"] = rebuild_result.get("vm_name", target_vm_name)
                    if rebuild_result.get("warnings"):
                        result["warnings"].extend(rebuild_result["warnings"])
                else:
                    result["errors"].append(rebuild_result.get("error", "Rebuild failed"))
                    return result

            # Step 4: Apply comprehensive config if available and restore succeeded
            if result["success"] and full_config and actual_mode in ("import", "rebuild"):
                if progress_callback:
                    progress_callback({"status": "applying_config", "vm": result["vm_name"]})

                final_vm_name = result.get("vm_name", target_vm_name)
                config_success, config_warnings = self.api.apply_vm_config(
                    final_vm_name,
                    full_config,
                    network_mapping,
                )

                if config_warnings:
                    result["warnings"].extend(config_warnings)

                if not config_success:
                    result["warnings"].append("Some configuration settings could not be applied")

            # Step 5: Start VM if requested
            if result["success"] and start_after_restore:
                if progress_callback:
                    progress_callback({"status": "starting_vm", "vm": result["vm_name"]})

                final_vm_name = result.get("vm_name", target_vm_name)
                if self.api.start_vm(final_vm_name):
                    result["vm_started"] = True
                else:
                    result["warnings"].append("Failed to start VM after restore")
                    result["vm_started"] = False

            if progress_callback:
                progress_callback({
                    "status": "completed",
                    "success": result["success"],
                    "vm_name": result.get("vm_name"),
                })

        except Exception as e:
            logger.exception(f"Restore failed for {import_path}")
            result["errors"].append(str(e))

        finally:
            result["duration_seconds"] = (dt.now() - started_at).total_seconds()

        return result

    def _load_backup_config(
        self,
        import_path: str,
        smb_unc: str,
        smb_user: str,
        smb_pass: str,
    ) -> dict[str, Any] | None:
        """Load vm_full_config.json from backup path."""
        # Config is in the timestamp folder (parent of VM export)
        # Structure: {timestamp}/vm_full_config.json
        #            {timestamp}/{vm_name}/Virtual Machines/...

        script = f"""
$ErrorActionPreference = 'Stop'
$importPath = '{import_path}'
$smbUnc = '{smb_unc}'
$smbUser = '{smb_user}'
$smbPass = '{smb_pass}'

try {{
    # Connect to SMB if needed
    if ($smbUnc) {{
        $netUseResult = & net use $smbUnc /user:$smbUser $smbPass 2>&1
        if ($LASTEXITCODE -ne 0) {{
            throw "Failed to connect to SMB share ${{smbUnc}}: $netUseResult"
        }}
    }}

    # Look for vm_full_config.json in the import path (timestamp folder)
    $configPath = Join-Path $importPath 'vm_full_config.json'

    if (Test-Path $configPath) {{
        $content = Get-Content $configPath -Raw
        Write-Output $content
    }} else {{
        # Also check parent folder in case import_path is the VM folder
        $parentPath = Split-Path -Parent $importPath
        $configPath = Join-Path $parentPath 'vm_full_config.json'
        if (Test-Path $configPath) {{
            $content = Get-Content $configPath -Raw
            Write-Output $content
        }} else {{
            Write-Output "NOT_FOUND"
        }}
    }}
}} finally {{
    if ($smbUnc) {{
        & net use $smbUnc /delete /y 2>&1 | Out-Null
    }}
}}
"""
        rc, stdout, stderr = self.api._run_powershell_large(script, timeout=60)

        if rc != 0 or not stdout.strip() or stdout.strip() == "NOT_FOUND":
            return None

        try:
            return json.loads(stdout.strip())
        except json.JSONDecodeError:
            logger.warning("Failed to parse vm_full_config.json")
            return None

    def _get_vm_name_from_path(self, import_path: str) -> str:
        """Extract VM name from backup path structure."""
        # Path structure: {backup_path}/{vm_name}/{timestamp}/{vm_name}/...
        # Or: {backup_path}/{vm_name}/{timestamp}/ (import_path is timestamp folder)
        parts = import_path.rstrip("\\").split("\\")

        # If this is the timestamp folder, VM name is in parent
        # Try to find a subfolder with Virtual Machines
        script = f"""
$importPath = '{import_path}'
$subfolders = Get-ChildItem -Path $importPath -Directory -ErrorAction SilentlyContinue
foreach ($folder in $subfolders) {{
    $vmFolder = Join-Path $folder.FullName 'Virtual Machines'
    if (Test-Path $vmFolder) {{
        Write-Output $folder.Name
        exit 0
    }}
}}
# Fallback to parent folder name
Write-Output (Split-Path -Leaf (Split-Path -Parent $importPath))
"""
        rc, stdout, stderr = self.api._run_powershell(script, timeout=30)
        if rc == 0 and stdout.strip():
            return stdout.strip()

        # Last resort - use last non-timestamp looking part
        for part in reversed(parts):
            # Skip timestamp-looking parts (YYYYMMDD_HHMMSS)
            if not (len(part) == 15 and part[8] == "_"):
                return part
        return parts[-1] if parts else "RestoredVM"

    def _restore_inplace(
        self,
        vm_name: str,
        import_path: str,
        smb_unc: str,
        smb_user: str,
        smb_pass: str,
        progress_callback: Any | None,
    ) -> tuple[bool, str]:
        """Restore by replacing VHD contents only, keeping VM configuration."""
        if progress_callback:
            progress_callback({"status": "inplace_restore", "vm": vm_name})

        script = f"""
$ErrorActionPreference = 'Stop'
$vmName = '{vm_name}'
$importPath = '{import_path}'
$smbUnc = '{smb_unc}'
$smbUser = '{smb_user}'
$smbPass = '{smb_pass}'

try {{
    # Connect to SMB if needed
    if ($smbUnc) {{
        $netUseResult = & net use $smbUnc /user:$smbUser $smbPass 2>&1
        if ($LASTEXITCODE -ne 0) {{
            throw "Failed to connect to SMB share ${{smbUnc}}: $netUseResult"
        }}
    }}

    # Get existing VM
    $vm = Get-VM -Name $vmName -ErrorAction Stop
    $wasRunning = $vm.State -eq 'Running'

    # Stop VM if running
    if ($wasRunning) {{
        Stop-VM -Name $vmName -Force -TurnOff -ErrorAction Stop
        # Wait for stop
        $timeout = 120
        $waited = 0
        while ((Get-VM -Name $vmName).State -ne 'Off' -and $waited -lt $timeout) {{
            Start-Sleep -Seconds 2
            $waited += 2
        }}
    }}

    # Get current VHD paths
    $currentVhds = Get-VMHardDiskDrive -VMName $vmName

    # Find backup VHDs
    # Check if Virtual Hard Disks exists directly in import path or in a subfolder
    $vmBackupFolder = $null
    $directVhdPath = Join-Path $importPath 'Virtual Hard Disks'
    if (Test-Path $directVhdPath) {{
        $vmBackupFolder = $importPath
    }} else {{
        $subfolders = Get-ChildItem -Path $importPath -Directory -ErrorAction SilentlyContinue
        foreach ($folder in $subfolders) {{
            $vmFolder = Join-Path $folder.FullName 'Virtual Hard Disks'
            if (Test-Path $vmFolder) {{
                $vmBackupFolder = $folder.FullName
                break
            }}
        }}
    }}

    if (-not $vmBackupFolder) {{
        throw "Could not find VM backup folder with VHDs in $importPath"
    }}

    $backupVhdFolder = Join-Path $vmBackupFolder 'Virtual Hard Disks'
    $backupVhds = Get-ChildItem -Path $backupVhdFolder -Include *.vhdx,*.vhd -Recurse |
        Where-Object {{ $_.Extension -ne '.avhdx' }}

    if ($backupVhds.Count -eq 0) {{
        throw "No VHD files found in backup"
    }}

    # Match and replace VHDs
    foreach ($currentVhd in $currentVhds) {{
        $currentPath = $currentVhd.Path
        $currentName = Split-Path -Leaf $currentPath

        # Find matching backup VHD
        $matchingBackup = $backupVhds | Where-Object {{ $_.Name -eq $currentName }} | Select-Object -First 1

        if ($matchingBackup) {{
            # Detach current VHD
            Remove-VMHardDiskDrive -VMHardDiskDrive $currentVhd -ErrorAction Stop

            # Copy backup VHD to original location
            Copy-Item -Path $matchingBackup.FullName -Destination $currentPath -Force -ErrorAction Stop

            # Re-attach VHD
            Add-VMHardDiskDrive -VMName $vmName `
                -ControllerType $currentVhd.ControllerType `
                -ControllerNumber $currentVhd.ControllerNumber `
                -ControllerLocation $currentVhd.ControllerLocation `
                -Path $currentPath -ErrorAction Stop
        }}
    }}

    # Start VM if it was running
    if ($wasRunning) {{
        Start-VM -Name $vmName -ErrorAction SilentlyContinue
    }}

    @{{ Success = $true; VMName = $vmName }} | ConvertTo-Json -Compress

}} catch {{
    @{{ Success = $false; Error = $_.Exception.Message }} | ConvertTo-Json -Compress
}} finally {{
    if ($smbUnc) {{
        & net use $smbUnc /delete /y 2>&1 | Out-Null
    }}
}}
"""
        rc, stdout, stderr = self.api._run_powershell_large(script, timeout=86400)

        if rc != 0:
            return False, stderr or "PowerShell error"

        try:
            result = json.loads(stdout.strip())
            if result.get("Success"):
                return True, "In-place restore completed"
            return False, result.get("Error", "Unknown error")
        except json.JSONDecodeError:
            return False, "Failed to parse result"

    def _restore_import(
        self,
        import_path: str,
        vm_name: str | None,
        restore_path: str | None,
        vhd_destination_path: str | None,
        generate_new_id: bool,
        smb_unc: str,
        smb_user: str,
        smb_pass: str,
        progress_callback: Any | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Restore using Import-VM with the .vmcx file."""
        script = f"""
$ErrorActionPreference = 'Stop'
$importPath = '{import_path}'
$targetVmName = '{vm_name or ""}'
$restorePath = '{restore_path or ""}'
$vhdDestPath = '{vhd_destination_path or ""}'
$generateNewId = ${str(generate_new_id).lower()}
$smbUnc = '{smb_unc}'
$smbUser = '{smb_user}'
$smbPass = '{smb_pass}'

$warnings = @()

try {{
    # Connect to SMB if needed
    if ($smbUnc) {{
        $netUseResult = & net use $smbUnc /user:$smbUser $smbPass 2>&1
        if ($LASTEXITCODE -ne 0) {{
            throw "Failed to connect to SMB share ${{smbUnc}}: $netUseResult"
        }}
    }}

    # Find VM subfolder and .vmcx file
    $vmcxPath = $null
    $vmBackupFolder = $null

    # First check if 'Virtual Machines' folder exists directly in import path
    # This handles the case where import_path already includes the VM folder name
    $directVmFolder = Join-Path $importPath 'Virtual Machines'
    if (Test-Path $directVmFolder) {{
        $vmcx = Get-ChildItem -Path $directVmFolder -Filter '*.vmcx' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($vmcx) {{
            $vmcxPath = $vmcx.FullName
            $vmBackupFolder = $importPath
        }}
    }}

    # If not found directly, check subfolders for VM export structure
    if (-not $vmcxPath) {{
        $subfolders = Get-ChildItem -Path $importPath -Directory -ErrorAction SilentlyContinue
        foreach ($folder in $subfolders) {{
            $vmFolder = Join-Path $folder.FullName 'Virtual Machines'
            if (Test-Path $vmFolder) {{
                $vmcx = Get-ChildItem -Path $vmFolder -Filter '*.vmcx' -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                if ($vmcx) {{
                    $vmcxPath = $vmcx.FullName
                    $vmBackupFolder = $folder.FullName
                    break
                }}
            }}
        }}
    }}

    if (-not $vmcxPath) {{
        throw "No .vmcx file found in backup at $importPath"
    }}

    # Get default paths if not specified
    $defaultVmPath = if ($restorePath) {{ $restorePath }} else {{ (Get-VMHost).VirtualMachinePath }}
    $defaultVhdPath = if ($vhdDestPath) {{ $vhdDestPath }} else {{ (Get-VMHost).VirtualHardDiskPath }}

    # Get VM name from backup folder
    $backupVmName = Split-Path -Leaf $vmBackupFolder
    $finalVmName = if ($targetVmName) {{ $targetVmName }} else {{ $backupVmName }}

    # Check if VM with this name exists
    $existingVm = Get-VM -Name $finalVmName -ErrorAction SilentlyContinue
    if ($existingVm) {{
        if ($existingVm.State -ne 'Off') {{
            Stop-VM -Name $finalVmName -Force -TurnOff -ErrorAction Stop
            Start-Sleep -Seconds 5
        }}
        Remove-VM -Name $finalVmName -Force
        $warnings += "Removed existing VM '$finalVmName' before import"
    }}

    # Import the VM
    $importParams = @{{
        Path = $vmcxPath
        VirtualMachinePath = $defaultVmPath
        VhdDestinationPath = $defaultVhdPath
    }}

    if ($generateNewId) {{
        $importParams.Copy = $true
        $importParams.GenerateNewId = $true
    }}

    $vm = Import-VM @importParams -ErrorAction Stop

    # Rename if needed
    if ($targetVmName -and $vm.Name -ne $targetVmName) {{
        Rename-VM -VM $vm -NewName $targetVmName -ErrorAction Stop
        $vm = Get-VM -Name $targetVmName
    }}

    @{{
        Success = $true
        VMId = $vm.Id.ToString()
        VMName = $vm.Name
        Warnings = $warnings
    }} | ConvertTo-Json -Compress

}} catch {{
    @{{
        Success = $false
        Error = $_.Exception.Message
        Warnings = $warnings
    }} | ConvertTo-Json -Compress
}} finally {{
    if ($smbUnc) {{
        & net use $smbUnc /delete /y 2>&1 | Out-Null
    }}
}}
"""
        rc, stdout, stderr = self.api._run_powershell_large(script, timeout=86400)

        if rc != 0:
            return False, {"error": stderr or "PowerShell error"}

        try:
            result = json.loads(stdout.strip())
            if result.get("Success"):
                return True, {
                    "vm_id": result.get("VMId"),
                    "vm_name": result.get("VMName"),
                    "warnings": result.get("Warnings", []),
                }
            return False, {
                "error": result.get("Error", "Unknown error"),
                "warnings": result.get("Warnings", []),
            }
        except json.JSONDecodeError:
            return False, {"error": "Failed to parse result"}

    def _restore_rebuild(
        self,
        import_path: str,
        vm_name: str,
        full_config: dict[str, Any] | None,
        restore_path: str | None,
        vhd_destination_path: str | None,
        smb_unc: str,
        smb_user: str,
        smb_pass: str,
        progress_callback: Any | None,
    ) -> tuple[bool, dict[str, Any]]:
        """Rebuild VM from VHDs and apply saved configuration."""
        # Helper to extract single value from potential array (PowerShell JSON quirk)
        def single_value(val: Any, default: Any) -> Any:
            if isinstance(val, list):
                return val[0] if val else default
            return val if val is not None else default

        # Get settings from config or use defaults
        generation = 2
        memory_bytes = 4 * 1024 * 1024 * 1024  # 4GB
        processor_count = 2
        # Note: TPM not enabled by default in rebuild - apply_vm_config handles it

        if full_config:
            vm_settings = full_config.get("vm", {})
            generation = single_value(vm_settings.get("generation"), 2)

            mem_settings = full_config.get("memory", {})
            memory_bytes = single_value(mem_settings.get("startupBytes"), memory_bytes)

            proc_settings = full_config.get("processor", {})
            processor_count = single_value(proc_settings.get("count"), processor_count)

        script = f"""
$ErrorActionPreference = 'Stop'
$importPath = '{import_path}'
$vmName = '{vm_name}'
$generation = {generation}
$memoryBytes = {memory_bytes}
$processorCount = {processor_count}
$restorePath = '{restore_path or ""}'
$vhdDestPath = '{vhd_destination_path or ""}'
$smbUnc = '{smb_unc}'
$smbUser = '{smb_user}'
$smbPass = '{smb_pass}'

$warnings = @()

try {{
    # Connect to SMB if needed
    if ($smbUnc) {{
        $netUseResult = & net use $smbUnc /user:$smbUser $smbPass 2>&1
        if ($LASTEXITCODE -ne 0) {{
            throw "Failed to connect to SMB share ${{smbUnc}}: $netUseResult"
        }}
    }}

    # Get default paths
    $defaultVmPath = if ($restorePath) {{ $restorePath }} else {{ (Get-VMHost).VirtualMachinePath }}
    $defaultVhdPath = if ($vhdDestPath) {{ $vhdDestPath }} else {{ (Get-VMHost).VirtualHardDiskPath }}

    # Find VHD files in backup
    # Check if Virtual Hard Disks exists directly in import path or in a subfolder
    $vmBackupFolder = $null
    $directVhdPath = Join-Path $importPath 'Virtual Hard Disks'
    if (Test-Path $directVhdPath) {{
        $vmBackupFolder = $importPath
    }} else {{
        $subfolders = Get-ChildItem -Path $importPath -Directory -ErrorAction SilentlyContinue
        foreach ($folder in $subfolders) {{
            $vhdFolder = Join-Path $folder.FullName 'Virtual Hard Disks'
            if (Test-Path $vhdFolder) {{
                $vmBackupFolder = $folder.FullName
                break
            }}
        }}
    }}

    if (-not $vmBackupFolder) {{
        throw "Could not find VM backup folder with VHDs in $importPath"
    }}

    $vhdFolder = Join-Path $vmBackupFolder 'Virtual Hard Disks'
    $backupVhds = Get-ChildItem -Path $vhdFolder -Include *.vhdx,*.vhd -Recurse |
        Where-Object {{ $_.Extension -ne '.avhdx' }}

    if ($backupVhds.Count -eq 0) {{
        throw "No VHD files found in backup"
    }}

    # === COMPREHENSIVE VM CLEANUP ===
    # This handles stuck/orphaned VMs including those on Cluster Shared Volumes (CSV)

    # Step 1: Remove any existing VM with this name (including from cluster)
    $existingVm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if ($existingVm) {{
        $warnings += "Found existing VM '$vmName', removing..."

        # If it's a clustered VM, remove from cluster first
        try {{
            $clusterGroup = Get-ClusterGroup -Name $vmName -ErrorAction SilentlyContinue
            if ($clusterGroup) {{
                Remove-ClusterGroup -Name $vmName -RemoveResources -Force -ErrorAction SilentlyContinue
                $warnings += "Removed VM '$vmName' from cluster"
                Start-Sleep -Seconds 2
            }}
        }} catch {{ }}

        # Stop and remove the VM
        if ($existingVm.State -ne 'Off') {{
            Stop-VM -Name $vmName -Force -TurnOff -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
        }}
        Remove-VM -Name $vmName -Force -ErrorAction SilentlyContinue
        $warnings += "Removed existing VM '$vmName' before rebuild"
        Start-Sleep -Seconds 2
    }}

    # Step 2: Clean up VM config folder from default path
    $vmConfigPath = Join-Path $defaultVmPath $vmName
    if (Test-Path $vmConfigPath) {{
        Start-Sleep -Seconds 2
        try {{
            Remove-Item -Path $vmConfigPath -Recurse -Force -ErrorAction Stop
            $warnings += "Cleaned up VM folder: $vmConfigPath"
        }} catch {{
            $null = & cmd /c rd /s /q "$vmConfigPath" 2>&1
            $warnings += "Used fallback cleanup for $vmConfigPath"
        }}
    }}

    # Step 3: Clean up any leftover VM folders by searching all known storage locations
    # This is fully dynamic - discovers paths from cluster, VMs, and VMHost config
    $pathsToCheck = @()

    # Get paths from Cluster Shared Volumes (if this is a cluster node)
    try {{
        $clusterSharedVolumes = Get-ClusterSharedVolume -ErrorAction SilentlyContinue
        if ($clusterSharedVolumes) {{
            foreach ($csv in $clusterSharedVolumes) {{
                $csvPath = $csv.SharedVolumeInfo.FriendlyVolumeName
                if ($csvPath) {{
                    # Search for VM folder anywhere under this CSV
                    Get-ChildItem -Path $csvPath -Directory -Recurse -ErrorAction SilentlyContinue |
                        Where-Object {{ $_.Name -eq $vmName }} | ForEach-Object {{
                            $pathsToCheck += $_.FullName
                        }}
                }}
            }}
        }}
    }} catch {{ }}

    # Get paths from all registered VMs that match our VM name
    Get-VM -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -eq $vmName }} | ForEach-Object {{
        if ($_.Path) {{ $pathsToCheck += $_.Path }}
        if ($_.ConfigurationLocation) {{ $pathsToCheck += $_.ConfigurationLocation }}
        if ($_.SnapshotFileLocation) {{ $pathsToCheck += $_.SnapshotFileLocation }}
    }}

    # Also check the default VM path
    $pathsToCheck += Join-Path $defaultVmPath $vmName

    # Remove duplicates and clean up each path
    $pathsToCheck = $pathsToCheck | Select-Object -Unique
    foreach ($pathToClean in $pathsToCheck) {{
        if ($pathToClean -and (Test-Path $pathToClean)) {{
            $warnings += "Found leftover VM folder: $pathToClean"
            Start-Sleep -Seconds 2
            try {{
                # First unregister any VM pointing to this path
                Get-VM -ErrorAction SilentlyContinue | Where-Object {{
                    $_.Path -eq $pathToClean -or
                    $_.ConfigurationLocation -eq $pathToClean -or
                    $_.Path -like "$pathToClean*"
                }} | ForEach-Object {{
                    $warnings += "Removing VM '$($_.Name)' that references $pathToClean"
                    # Remove from cluster first if clustered
                    try {{
                        $grp = Get-ClusterGroup -Name $_.Name -ErrorAction SilentlyContinue
                        if ($grp) {{
                            Remove-ClusterGroup -Name $_.Name -RemoveResources -Force -ErrorAction SilentlyContinue
                        }}
                    }} catch {{ }}
                    if ($_.State -ne 'Off') {{ Stop-VM -VM $_ -Force -TurnOff -ErrorAction SilentlyContinue }}
                    Remove-VM -VM $_ -Force -ErrorAction SilentlyContinue
                }}
                Start-Sleep -Seconds 2
                Remove-Item -Path $pathToClean -Recurse -Force -ErrorAction Stop
                $warnings += "Cleaned up folder: $pathToClean"
            }} catch {{
                # Try cmd fallback for stubborn folders
                $null = & cmd /c rd /s /q "$pathToClean" 2>&1
                $warnings += "Used fallback cleanup for: $pathToClean"
            }}
        }}
    }}

    # Step 4: Clean up orphaned vmcx files from Hyper-V's data root
    # Discover the path dynamically from registry or VMHost
    $hvDataRoot = $null
    try {{
        # Try to get from registry first (most reliable)
        $hvRegPath = "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Virtualization"
        $hvDataRoot = (Get-ItemProperty -Path $hvRegPath -ErrorAction SilentlyContinue).DataRoot
    }} catch {{ }}
    if (-not $hvDataRoot) {{
        # Fallback: derive from environment - use Join-Path to build dynamically
        $hvDataRoot = Join-Path $env:ProgramData (Join-Path "Microsoft" (Join-Path "Windows" "Hyper-V"))
    }}

    if ($hvDataRoot) {{
        $sysVmPath = Join-Path $hvDataRoot "Virtual Machines"
        if (Test-Path $sysVmPath) {{
            Get-ChildItem -Path $sysVmPath -Filter "*.vmcx" -ErrorAction SilentlyContinue | ForEach-Object {{
                $vmcxId = $_.BaseName
                $linkedVm = Get-VM -ErrorAction SilentlyContinue | Where-Object {{ $_.Id.ToString() -eq $vmcxId }}
                if (-not $linkedVm) {{
                    try {{
                        Remove-Item -Path $_.FullName -Force -ErrorAction SilentlyContinue
                        $warnings += "Removed orphaned vmcx: $($_.Name)"
                    }} catch {{ }}
                }}
            }}
        }}
    }}

    # Step 5: Final wait for any file handles to release
    Start-Sleep -Seconds 3

    # Copy VHDs to local storage
    $localVhdPath = Join-Path $defaultVhdPath $vmName
    if (Test-Path $localVhdPath) {{
        Remove-Item -Path $localVhdPath -Recurse -Force
    }}
    New-Item -ItemType Directory -Path $localVhdPath -Force | Out-Null

    $localVhds = @()
    foreach ($vhd in $backupVhds) {{
        $destVhd = Join-Path $localVhdPath $vhd.Name
        Copy-Item -Path $vhd.FullName -Destination $destVhd -Force
        $localVhds += $destVhd
    }}

    # Create new VM
    $vm = New-VM -Name $vmName -Generation $generation -MemoryStartupBytes $memoryBytes `
        -Path $defaultVmPath -NoVHD

    # Set processor count
    Set-VMProcessor -VMName $vmName -Count $processorCount

    # Attach VHDs
    foreach ($vhd in $localVhds) {{
        Add-VMHardDiskDrive -VMName $vmName -Path $vhd -ControllerType SCSI
    }}

    $warnings += "VM rebuilt from VHDs - configuration will be applied separately"

    @{{
        Success = $true
        VMId = $vm.Id.ToString()
        VMName = $vm.Name
        Warnings = $warnings
    }} | ConvertTo-Json -Compress

}} catch {{
    @{{
        Success = $false
        Error = $_.Exception.Message
        Warnings = $warnings
    }} | ConvertTo-Json -Compress
}} finally {{
    if ($smbUnc) {{
        & net use $smbUnc /delete /y 2>&1 | Out-Null
    }}
}}
"""
        rc, stdout, stderr = self.api._run_powershell_large(script, timeout=86400)

        if rc != 0:
            return False, {"error": stderr or "PowerShell error"}

        try:
            result = json.loads(stdout.strip())
            if result.get("Success"):
                return True, {
                    "vm_id": result.get("VMId"),
                    "vm_name": result.get("VMName"),
                    "warnings": result.get("Warnings", []),
                }
            return False, {
                "error": result.get("Error", "Unknown error"),
                "warnings": result.get("Warnings", []),
            }
        except json.JSONDecodeError:
            return False, {"error": "Failed to parse result"}

    def list_backups(
        self,
        backup_path: str,
        vm_name: str | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available VM backups in a directory.

        Scans the backup path for exported VMs using the VM-centric structure:
        {backup_path}/{vm_name}/{timestamp}/{vm_name}/Virtual Machines/*.vmcx

        Supports both local paths and SMB/UNC network paths with authentication.

        Args:
            backup_path: Path to the backup directory (local or UNC)
            vm_name: Optional filter by VM name
            smb_username: SMB username for network path authentication
            smb_password: SMB password for network path authentication
            smb_domain: SMB domain for network path authentication

        Returns:
            List of backup dicts with vm_name, timestamp, path, size info
        """
        logger.info(f"Listing Hyper-V backups in: {backup_path}")

        vm_filter = ""
        if vm_name:
            vm_filter = f"| Where-Object {{ $_.Name -eq '{vm_name}' }}"

        # Check if path is a UNC path (network share)
        is_unc_path = backup_path.startswith("\\\\")

        if is_unc_path and smb_username and smb_password:
            # For UNC paths, use PSDrive with credentials
            unc_parts = backup_path.lstrip("\\").split("\\")
            if len(unc_parts) >= 2:
                smb_server = unc_parts[0]
                smb_share = unc_parts[1]
                smb_unc = f"\\\\{smb_server}\\{smb_share}"
                safe_password = smb_password.replace("'", "''")
                full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username

                script = f"""
$ErrorActionPreference = 'Stop'
$backupPath = '{backup_path}'
$smbUnc = '{smb_unc}'
$smbUser = '{full_username}'
$smbPass = '{safe_password}'

# Authenticate to SMB share using PSDrive
$secPass = ConvertTo-SecureString $smbPass -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($smbUser, $secPass)

$driveName = "BackerSMB"
if (Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue) {{
    Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
}}

try {{
    New-PSDrive -Name $driveName -PSProvider FileSystem -Root $smbUnc -Credential $cred -ErrorAction Stop | Out-Null
}} catch {{
    throw "Failed to connect to SMB share $smbUnc : $_"
}}

try {{
    # Convert UNC path to mapped drive path
    $subPath = $backupPath.Substring($smbUnc.Length).TrimStart('\\')
    if ($subPath) {{
        $mappedPath = "${{driveName}}:\\$subPath"
    }} else {{
        $mappedPath = "${{driveName}}:\\"
    }}

    $backups = @()

    if (Test-Path $mappedPath) {{
        # VM-centric structure: backup_path/vm_name/timestamp/vm_name/Virtual Machines/*.vmcx
        # First level: VM name folders
        Get-ChildItem -Path $mappedPath -Directory {vm_filter} | ForEach-Object {{
            $vmFolder = $_
            $vmFolderName = $vmFolder.Name

            # Skip .backer metadata folder
            if ($vmFolderName -eq '.backer') {{ return }}

            # Second level: timestamp folders within each VM folder
            Get-ChildItem -Path $vmFolder.FullName -Directory | ForEach-Object {{
                $timestampFolder = $_
                $timestampName = $timestampFolder.Name

                # Look for the VM export folder inside timestamp
                # Export-VM creates: timestamp/vm_name/Virtual Machines/
                $vmExportFolder = Join-Path $timestampFolder.FullName $vmFolderName
                $vmcxPath = Join-Path $vmExportFolder 'Virtual Machines'

                if (Test-Path $vmcxPath) {{
                    $vmcxFiles = Get-ChildItem -Path $vmcxPath -Filter '*.vmcx' -ErrorAction SilentlyContinue
                    $jsonConfig = Get-ChildItem -Path $vmcxPath -Filter 'vm_config.json' -ErrorAction SilentlyContinue

                    # Accept either .vmcx (standard export) or vm_config.json (manual fallback)
                    if ($vmcxFiles -or $jsonConfig) {{
                        $size = (Get-ChildItem $vmExportFolder -Recurse -ErrorAction SilentlyContinue |
                                 Measure-Object -Property Length -Sum).Sum

                        # Return the UNC path to the VM export folder
                        $uncPath = $backupPath.TrimEnd('\\') + '\\' + $vmFolderName + '\\' + `
                            $timestampName + '\\' + $vmFolderName

                        $configFile = if ($vmcxFiles) {{ $vmcxFiles[0].Name }} else {{ 'vm_config.json' }}
                        $backups += @{{
                            VMName = $vmFolderName
                            Timestamp = $timestampName
                            Name = $vmFolderName + '/' + $timestampName
                            Path = $uncPath
                            CreatedAt = $timestampFolder.CreationTime.ToString('o')
                            ModifiedAt = $timestampFolder.LastWriteTime.ToString('o')
                            SizeBytes = if ($size) {{ $size }} else {{ 0 }}
                            VmcxFile = $configFile
                            IsManualBackup = [bool]$jsonConfig
                        }}
                    }}
                }}
            }}
        }}
    }}

    $backups | ConvertTo-Json -Compress
}} finally {{
    Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
}}
"""
            else:
                logger.error(f"Invalid UNC path format: {backup_path}")
                return []
        else:
            # Local path - access directly
            script = f"""
$backupPath = '{backup_path}'
$backups = @()

if (Test-Path $backupPath) {{
    # VM-centric structure: {{backup_path}}/{{vm_name}}/{{timestamp}}/{{vm_name}}/Virtual Machines/*.vmcx
    # First level: VM name folders
    Get-ChildItem -Path $backupPath -Directory {vm_filter} | ForEach-Object {{
        $vmFolder = $_
        $vmFolderName = $vmFolder.Name

        # Skip .backer metadata folder
        if ($vmFolderName -eq '.backer') {{ return }}

        # Second level: timestamp folders within each VM folder
        Get-ChildItem -Path $vmFolder.FullName -Directory | ForEach-Object {{
            $timestampFolder = $_
            $timestampName = $timestampFolder.Name

            # Look for the VM export folder inside timestamp
            # Export-VM creates: {{timestamp}}/{{vm_name}}/Virtual Machines/
            $vmExportFolder = Join-Path $timestampFolder.FullName $vmFolderName
            $vmcxPath = Join-Path $vmExportFolder 'Virtual Machines'

            if (Test-Path $vmcxPath) {{
                $vmcxFiles = Get-ChildItem -Path $vmcxPath -Filter '*.vmcx' -ErrorAction SilentlyContinue
                $jsonConfig = Get-ChildItem -Path $vmcxPath -Filter 'vm_config.json' -ErrorAction SilentlyContinue

                # Accept either .vmcx (standard export) or vm_config.json (manual fallback)
                if ($vmcxFiles -or $jsonConfig) {{
                    $size = (Get-ChildItem $vmExportFolder -Recurse -ErrorAction SilentlyContinue |
                             Measure-Object -Property Length -Sum).Sum

                    $configFile = if ($vmcxFiles) {{
                        $vmcxFiles[0].FullName
                    }} else {{ Join-Path $vmcxPath 'vm_config.json' }}
                    $backups += @{{
                        VMName = $vmFolderName
                        Timestamp = $timestampName
                        Name = $vmFolderName + '/' + $timestampName
                        Path = $vmExportFolder
                        CreatedAt = $timestampFolder.CreationTime.ToString('o')
                        ModifiedAt = $timestampFolder.LastWriteTime.ToString('o')
                        SizeBytes = if ($size) {{ $size }} else {{ 0 }}
                        VmcxFile = $configFile
                        IsManualBackup = [bool]$jsonConfig
                    }}
                }}
            }}
        }}
    }}
}}

$backups | ConvertTo-Json -Compress
"""

        rc, stdout, stderr = self.api._run_powershell_large(script, timeout=300)

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
                    "vm_name": item.get("VMName", ""),
                    "timestamp": item.get("Timestamp", ""),
                    "name": item.get("Name", ""),
                    "path": item.get("Path", ""),
                    "created_at": item.get("CreatedAt", ""),
                    "modified_at": item.get("ModifiedAt", ""),
                    "size_bytes": item.get("SizeBytes", 0),
                    "vmcx_file": item.get("VmcxFile", ""),
                })

            # Sort by created_at descending (newest first)
            backups.sort(key=lambda x: x.get("created_at", ""), reverse=True)

            logger.info(f"Found {len(backups)} Hyper-V backups in {backup_path}")
            return backups

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse backup list: {e}")
            return []


class HyperVClusterAPI(HyperVAPI):
    """Client for Hyper-V Failover Cluster management via WinRM + PowerShell.

    Extends HyperVAPI to handle clustered VMs. Key differences:
    - Connects to any cluster node (or cluster name)
    - Uses Failover Cluster cmdlets to find VM owner nodes
    - Routes VM operations to the correct owner node
    - Handles VM live migration between nodes

    Example usage:
        api = HyperVClusterAPI(
            cluster_name="HVCluster",
            host="node1.domain.com",  # Any cluster node to connect through
            username="Administrator",
            password="mypassword",
        )
        vms = api.list_guests()  # Gets VMs from all cluster nodes
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        cluster_name: str | None = None,
        port: int = 5985,
        use_ssl: bool = False,
        auth_method: HyperVAuthMethod = HyperVAuthMethod.NTLM,
        verify_ssl: bool = False,
        timeout: int = 60,
        domain: str | None = None,
    ):
        """Initialize Hyper-V Cluster API client.

        Args:
            host: Any cluster node hostname/IP to connect through
            username: Windows username (domain admin recommended)
            password: Windows password
            cluster_name: Optional cluster name (auto-detected if not provided)
            port: WinRM port (5985 for HTTP, 5986 for HTTPS)
            use_ssl: Use HTTPS for WinRM
            auth_method: Authentication method (basic, ntlm, kerberos)
            verify_ssl: Whether to verify SSL certificates
            timeout: Command timeout in seconds
            domain: Windows domain (required for cluster operations)
        """
        super().__init__(
            host=host,
            username=username,
            password=password,
            port=port,
            use_ssl=use_ssl,
            auth_method=auth_method,
            verify_ssl=verify_ssl,
            timeout=timeout,
            domain=domain,
        )
        self.cluster_name = cluster_name
        self._cluster_nodes: list[str] = []
        self._cluster_domain: str = ""
        self._node_ip_map: dict[str, str] = {}  # Maps node name/FQDN to IP address
        self._node_connections: dict[str, HyperVAPI] = {}

    def _get_or_create_node_connection(self, node_name: str) -> HyperVAPI:
        """Get or create a WinRM connection to a specific cluster node.

        Attempts connection by hostname/FQDN first, falls back to IP address
        if DNS resolution fails. This provides redundancy when the Backer
        server's DNS doesn't resolve the cluster domain.

        Args:
            node_name: Cluster node hostname (short name or FQDN)

        Returns:
            HyperVAPI instance connected to the specified node

        Raises:
            Exception: If connection fails by both hostname and IP
        """
        if node_name in self._node_connections:
            return self._node_connections[node_name]

        # Determine the host to connect to
        short_name = node_name.split(".")[0] if "." in node_name else node_name

        # Build FQDN if we have a short name and know the cluster domain
        if "." not in node_name and self._cluster_domain:
            connect_host = f"{node_name}.{self._cluster_domain}"
            logger.debug(f"Built FQDN '{connect_host}' from short name '{node_name}'")
        else:
            connect_host = node_name

        # Get IP for fallback - check both short name and FQDN in the map
        node_ip = self._node_ip_map.get(node_name)
        if not node_ip and short_name in self._node_ip_map:
            node_ip = self._node_ip_map[short_name]
        if not node_ip and connect_host in self._node_ip_map:
            node_ip = self._node_ip_map[connect_host]

        # Try to create connection with FQDN/hostname first
        try:
            api = HyperVAPI(
                host=connect_host,
                username=self.username,
                password=self.password,
                port=self.port,
                use_ssl=self.use_ssl,
                auth_method=self.auth_method,
                verify_ssl=self.verify_ssl,
                timeout=self.timeout,
                domain=self.domain,
            )
            # Test the connection works (will fail fast on DNS issues)
            api._run_powershell("$env:COMPUTERNAME", timeout=10)
            self._node_connections[node_name] = api
            logger.debug(f"Connected to node '{node_name}' via hostname")
            return api

        except Exception as e:
            error_str = str(e).lower()
            is_dns_error = (
                "name resolution" in error_str
                or "getaddrinfo" in error_str
                or "resolve" in error_str
                or "nodename nor servname" in error_str
            )

            if is_dns_error and node_ip:
                logger.warning(
                    f"DNS resolution failed for '{node_name}', "
                    f"falling back to IP {node_ip}"
                )
                try:
                    api = HyperVAPI(
                        host=node_ip,
                        username=self.username,
                        password=self.password,
                        port=self.port,
                        use_ssl=self.use_ssl,
                        auth_method=self.auth_method,
                        verify_ssl=self.verify_ssl,
                        timeout=self.timeout,
                        domain=self.domain,
                    )
                    # Verify connection works
                    api._run_powershell("$env:COMPUTERNAME", timeout=10)
                    self._node_connections[node_name] = api
                    logger.info(f"Connected to node '{node_name}' via IP {node_ip}")
                    return api
                except Exception as ip_error:
                    logger.error(
                        f"Failed to connect to node '{node_name}' "
                        f"via IP {node_ip}: {ip_error}"
                    )
                    raise
            else:
                # Not a DNS error or no IP fallback available
                raise

    def _run_powershell_on_node(
        self,
        node_name: str,
        script: str,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Execute PowerShell on a specific cluster node.

        Args:
            node_name: Target cluster node
            script: PowerShell script to execute
            timeout: Optional command timeout

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        node_api = self._get_or_create_node_connection(node_name)
        return node_api._run_powershell(script, timeout)

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to the Hyper-V cluster.

        Returns:
            Tuple of (success: bool, message: str with cluster info or error)
        """
        full_username = self._get_full_username()
        logger.info(
            f"Testing Hyper-V Cluster connection via {self.host} as {full_username}"
        )

        try:
            # Test basic connectivity and get cluster info
            # IMPORTANT: Run all cluster commands locally (no -Cluster parameter)
            # to avoid Kerberos double-hop authentication issues with WinRM
            script = """
$ErrorActionPreference = 'Stop'
try {
    # Get cluster info locally - this node must be a cluster member
    # Don't use -Cluster parameter to avoid double-hop auth issues
    $cluster = Get-Cluster -ErrorAction Stop

    # Get nodes with their IPs from cluster network interfaces
    # Get-ClusterNetworkInterface returns Ipv4Addresses property
    $nodeList = @()
    $clusterNodes = Get-ClusterNode

    foreach ($node in $clusterNodes) {
        $nodeInfo = @{
            Name = $node.Name
            State = $node.State.ToString()
            Id = $node.Id.ToString()
            Ipv4Addresses = @()
        }

        # Get network interfaces for this node
        # Use the first IPv4 address from cluster networks
        $netInterfaces = Get-ClusterNetworkInterface -Node $node.Name `
            -ErrorAction SilentlyContinue

        if ($netInterfaces) {
            foreach ($nic in $netInterfaces) {
                # Ipv4Addresses is an array property
                if ($nic.Ipv4Addresses) {
                    foreach ($ip in $nic.Ipv4Addresses) {
                        if ($ip -and $ip -notlike "169.254.*") {
                            # Skip APIPA addresses
                            $nodeInfo.Ipv4Addresses += $ip
                        }
                    }
                }
            }
        }

        $nodeList += $nodeInfo
    }

    $vmGroups = Get-ClusterGroup | Where-Object { $_.GroupType -eq 'VirtualMachine' }

    $os = Get-CimInstance Win32_OperatingSystem

    @{
        ClusterName = $cluster.Name
        ClusterDomain = $cluster.Domain
        Nodes = $nodeList
        TotalVMs = $vmGroups.Count
        OSVersion = $os.Caption
        OSBuild = $os.BuildNumber
        QuorumType = if ($cluster.QuorumType) { $cluster.QuorumType.ToString() } else { "Unknown" }
    } | ConvertTo-Json -Depth 4
} catch {
    @{
        Error = $_.Exception.Message
        FullError = $_.ToString()
        ScriptStackTrace = $_.ScriptStackTrace
        Category = $_.CategoryInfo.Category.ToString()
        IsCluster = $false
    } | ConvertTo-Json
}
"""
            rc, stdout, stderr = self._run_powershell(script)

            if rc != 0:
                error_msg = stderr.strip() or "Connection failed"
                logger.error(f"Cluster connection failed: {error_msg}")
                return False, f"Cluster connection failed: {error_msg}"

            try:
                json_match = re.search(r"\{.*\}", stdout, re.DOTALL)
                if json_match:
                    info = json.loads(json_match.group())

                    if info.get("Error"):
                        error_msg = info.get("Error", "Unknown error")
                        full_error = info.get("FullError", "")
                        category = info.get("Category", "")
                        stack = info.get("ScriptStackTrace", "")
                        logger.error(f"Cluster error: {error_msg}")
                        logger.error(f"Category: {category}")
                        logger.error(f"Full error: {full_error}")
                        if stack:
                            logger.error(f"Stack trace: {stack}")
                        return False, f"Cluster error: {error_msg}"

                    self.cluster_name = info.get("ClusterName", self.cluster_name)
                    cluster_domain = info.get("ClusterDomain", "")

                    # Determine domain for FQDN resolution - try multiple sources:
                    # 1. Explicit domain parameter
                    # 2. Domain from username (DOMAIN\user format)
                    # 3. Domain from host FQDN (node1.stockhome.local)
                    # 4. ClusterDomain from PowerShell
                    dns_domain = ""
                    if self.domain:
                        dns_domain = self.domain
                        logger.debug(f"Using explicit domain for DNS: {dns_domain}")
                    elif "\\" in self.username:
                        # Extract domain from DOMAIN\username format
                        dns_domain = self.username.split("\\")[0]
                        logger.debug(f"Extracted domain from username: {dns_domain}")
                    elif "." in self.host:
                        # Extract domain from FQDN host (e.g., node1.stockhome.local)
                        parts = self.host.split(".", 1)
                        if len(parts) > 1:
                            dns_domain = parts[1]
                            logger.debug(f"Extracted domain from host FQDN: {dns_domain}")
                    elif cluster_domain:
                        dns_domain = cluster_domain
                        logger.debug(f"Using cluster domain: {dns_domain}")

                    self._cluster_domain = dns_domain

                    # Build node names and IP map for FQDN resolution and fallback
                    # Node names from cluster are often short names (e.g., "node1")
                    # but DNS may require FQDN (e.g., "node1.stockhome.local")
                    self._cluster_nodes = []
                    self._node_ip_map = {}

                    for n in info.get("Nodes", []):
                        node_name = n["Name"]
                        # If node name doesn't contain a dot and we have a domain,
                        # create FQDN
                        if "." not in node_name and dns_domain:
                            node_fqdn = f"{node_name}.{dns_domain}"
                        else:
                            node_fqdn = node_name
                        self._cluster_nodes.append(node_fqdn)

                        # Extract IP addresses for DNS fallback
                        # Store mapping for both short name and FQDN
                        ipv4_addresses = n.get("Ipv4Addresses", [])
                        if ipv4_addresses and len(ipv4_addresses) > 0:
                            # Use first non-empty IP address
                            node_ip = ipv4_addresses[0]
                            self._node_ip_map[node_name] = node_ip
                            self._node_ip_map[node_fqdn] = node_ip
                            logger.debug(
                                f"Cluster node: {node_name} -> {node_fqdn} "
                                f"(IP: {node_ip})"
                            )
                        else:
                            logger.debug(
                                f"Cluster node: {node_name} -> {node_fqdn} "
                                "(no IP found)"
                            )

                    self._version = info.get("OSVersion", "Unknown")

                    node_count = len(self._cluster_nodes)
                    vm_count = info.get("TotalVMs", 0)
                    online_nodes = sum(
                        1 for n in info.get("Nodes", [])
                        if n.get("State") == "Up"
                    )

                    logger.info(
                        f"Connected to cluster '{self.cluster_name}' "
                        f"(dns_domain: {dns_domain}, "
                        f"{online_nodes}/{node_count} nodes online, {vm_count} VMs)"
                    )
                    logger.info(f"Cluster nodes: {self._cluster_nodes}")
                    if self._node_ip_map:
                        logger.info(f"Node IP map: {self._node_ip_map}")
                    return True, (
                        f"Connected to cluster '{self.cluster_name}' - "
                        f"{online_nodes}/{node_count} nodes online, {vm_count} VMs"
                    )
                else:
                    return False, "Failed to parse cluster response"

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse cluster info: {e}")
                return False, f"Failed to parse cluster info: {e}"

        except Exception as e:
            logger.exception("Cluster connection test failed")
            return False, f"Connection failed: {e}"

    def get_cluster_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes in the failover cluster.

        Returns:
            List of node info dicts with name, state, and VM count
        """
        script = """
$ErrorActionPreference = 'Stop'
$nodes = Get-ClusterNode | ForEach-Object {
    $nodeName = $_.Name
    $vmCount = (Get-ClusterGroup | Where-Object {
        $_.GroupType -eq 'VirtualMachine' -and $_.OwnerNode.Name -eq $nodeName
    }).Count

    @{
        Name = $_.Name
        State = $_.State.ToString()
        Id = $_.Id.ToString()
        VMCount = $vmCount
    }
}
$nodes | ConvertTo-Json -Depth 2
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc != 0:
            logger.error(f"Failed to get cluster nodes: {stderr}")
            return []

        try:
            stdout = stdout.strip()
            if not stdout or stdout == "null":
                return []

            data = json.loads(stdout)
            if isinstance(data, dict):
                data = [data]

            return data

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse cluster nodes: {e}")
            return []

    def get_vm_owner_node(self, vm_name: str) -> str | None:
        """Find which cluster node currently owns a VM.

        Args:
            vm_name: VM name to look up

        Returns:
            Node name or None if not found/not clustered
        """
        # Try to find by cluster group name first (usually matches VM name)
        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$group = Get-ClusterGroup -Name '{vm_name}' -ErrorAction SilentlyContinue
if ($group -and $group.GroupType -eq 'VirtualMachine') {{
    $group.OwnerNode.Name
}} else {{
    # Search by VM ID in cluster resources
    $vmGroups = Get-ClusterGroup | Where-Object {{ $_.GroupType -eq 'VirtualMachine' }}
    foreach ($g in $vmGroups) {{
        $vmRes = Get-ClusterResource -InputObject $g |
            Where-Object {{ $_.ResourceType -eq 'Virtual Machine' }}
        if ($vmRes) {{
            $vmId = (Get-ClusterParameter -InputObject $vmRes -Name VmId `
                -ErrorAction SilentlyContinue).Value
            $vm = Get-VM -Id $vmId -ErrorAction SilentlyContinue
            if ($vm -and $vm.Name -eq '{vm_name}') {{
                $g.OwnerNode.Name
                break
            }}
        }}
    }}
}}
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc == 0 and stdout.strip():
            owner = stdout.strip()

            # Convert short name to FQDN if needed
            if "." not in owner:
                dns_domain = self._cluster_domain
                if not dns_domain:
                    # Derive domain from available sources
                    if self.domain:
                        dns_domain = self.domain
                    elif "\\" in self.username:
                        dns_domain = self.username.split("\\")[0]
                    elif "." in self.host:
                        parts = self.host.split(".", 1)
                        if len(parts) > 1:
                            dns_domain = parts[1]
                if dns_domain:
                    owner = f"{owner}.{dns_domain}"

            logger.debug(f"VM '{vm_name}' is owned by node '{owner}'")
            return owner

        logger.debug(f"Could not find owner node for VM '{vm_name}'")
        return None

    def list_guests(self) -> list[HyperVGuest]:
        """List all VMs across the cluster with owner node info.

        Uses a two-phase approach to avoid Kerberos double-hop:
        1. Get cluster topology (VM names, IDs, owner nodes) from connected node
        2. Query each owner node directly via separate WinRM connections

        Returns:
            List of HyperVGuest objects with cluster info populated
        """
        # Phase 1: Get cluster VM topology AND node IPs (runs locally, no double-hop)
        # Include node IPs in case _node_ip_map isn't populated from test_connection
        script = """
$ErrorActionPreference = 'Stop'
$vmResults = @()
$nodeInfo = @{}

# Get all VM cluster groups with their owner nodes
$vmGroups = Get-ClusterGroup | Where-Object { $_.GroupType -eq 'VirtualMachine' }

foreach ($group in $vmGroups) {
    $ownerNode = $group.OwnerNode.Name

    # Get VM resource to find VmId
    $vmRes = Get-ClusterResource -InputObject $group |
        Where-Object { $_.ResourceType -eq 'Virtual Machine' }

    if ($vmRes) {
        $vmId = (Get-ClusterParameter -InputObject $vmRes -Name VmId `
            -ErrorAction SilentlyContinue).Value

        if ($vmId) {
            $vmResults += @{
                VmId = $vmId.ToString()
                VmName = $group.Name
                OwnerNode = $ownerNode
                ClusterGroupName = $group.Name
                State = $group.State.ToString()
            }
        }
    }
}

# Get node IPs from cluster network interfaces
$clusterNodes = Get-ClusterNode
foreach ($node in $clusterNodes) {
    $ips = @()
    $netInterfaces = Get-ClusterNetworkInterface -Node $node.Name -ErrorAction SilentlyContinue
    if ($netInterfaces) {
        foreach ($nic in $netInterfaces) {
            if ($nic.Ipv4Addresses) {
                foreach ($ip in $nic.Ipv4Addresses) {
                    if ($ip -and $ip -notlike "169.254.*") {
                        $ips += $ip
                    }
                }
            }
        }
    }
    $nodeInfo[$node.Name] = @{
        Ipv4Addresses = $ips
    }
}

@{
    VMs = $vmResults
    Nodes = $nodeInfo
} | ConvertTo-Json -Depth 3 -Compress
"""
        rc, stdout, stderr = self._run_powershell(script, timeout=60)

        if rc != 0:
            logger.error(f"Failed to get cluster VM topology: {stderr}")
            return self._list_guests_fallback()

        try:
            stdout = stdout.strip()
            if not stdout or stdout == "null" or stdout == "[]" or stdout == "{}":
                logger.info("No VMs found in cluster")
                return []

            response = json.loads(stdout)

            # Extract VMs and node info from response
            topology = response.get("VMs", [])
            node_info = response.get("Nodes", {})

            if isinstance(topology, dict):
                topology = [topology]

            logger.info(f"Found {len(topology)} VMs in cluster topology")

            # Update node IP map if we don't have it cached
            if not self._node_ip_map and node_info:
                logger.debug("Populating node IP map from list_guests response")
                for node_name, info in node_info.items():
                    ipv4_addresses = info.get("Ipv4Addresses", [])
                    if ipv4_addresses and len(ipv4_addresses) > 0:
                        self._node_ip_map[node_name] = ipv4_addresses[0]
                        logger.debug(f"Node IP: {node_name} -> {ipv4_addresses[0]}")

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse cluster topology: {e}")
            return self._list_guests_fallback()

        # Phase 2: Group VMs by owner node and query each node directly
        # Determine DNS domain for FQDN resolution if not already set
        dns_domain = self._cluster_domain
        if not dns_domain:
            # Try to derive domain from various sources
            if self.domain:
                dns_domain = self.domain
            elif "\\" in self.username:
                dns_domain = self.username.split("\\")[0]
            elif "." in self.host:
                parts = self.host.split(".", 1)
                if len(parts) > 1:
                    dns_domain = parts[1]
            if dns_domain:
                logger.debug(f"Derived DNS domain for node FQDN: {dns_domain}")
                # Also update _cluster_domain for future use
                self._cluster_domain = dns_domain

        vms_by_node: dict[str, list[dict]] = {}
        for vm in topology:
            owner = vm.get("OwnerNode", "")
            if owner:
                # Build FQDN if needed
                if "." not in owner and dns_domain:
                    owner = f"{owner}.{dns_domain}"
                if owner not in vms_by_node:
                    vms_by_node[owner] = []
                vms_by_node[owner].append(vm)

        guests = []
        for node_name, node_vms in vms_by_node.items():
            logger.debug(f"Querying {len(node_vms)} VMs from node '{node_name}'")

            # Get direct connection to this node
            try:
                node_api = self._get_or_create_node_connection(node_name)

                # Query all VMs on this node
                vm_ids = [vm["VmId"] for vm in node_vms]
                vm_ids_json = json.dumps(vm_ids)

                detail_script = f"""
$ErrorActionPreference = 'Stop'
$vmIds = '{vm_ids_json}' | ConvertFrom-Json
$results = @()

foreach ($id in $vmIds) {{
    $vm = Get-VM -Id $id -ErrorAction SilentlyContinue
    if ($vm) {{
        $results += @{{
            Id = $vm.Id.ToString()
            Name = $vm.Name
            State = $vm.State.ToString()
            ProcessorCount = $vm.ProcessorCount
            MemoryAssigned = $vm.MemoryAssigned
            Uptime = @{{ TotalSeconds = $vm.Uptime.TotalSeconds }}
            Generation = $vm.Generation
            Version = $vm.Version
            Path = $vm.Path
            DynamicMemoryEnabled = $vm.DynamicMemoryEnabled
            CheckpointsEnabled = ($vm.CheckpointType -ne 'Disabled')
        }}
    }}
}}

$results | ConvertTo-Json -Depth 3 -Compress
"""
                rc, stdout, stderr = node_api._run_powershell(
                    detail_script, timeout=60
                )

                if rc == 0 and stdout.strip():
                    try:
                        vm_details = json.loads(stdout.strip())
                        if isinstance(vm_details, dict):
                            vm_details = [vm_details]

                        for detail in vm_details:
                            vm_id = detail.get("Id", "")

                            memory_bytes = detail.get("MemoryAssigned", 0) or 0
                            memory_mb = memory_bytes // (1024 * 1024)

                            uptime_seconds = 0
                            uptime = detail.get("Uptime")
                            if isinstance(uptime, dict):
                                uptime_seconds = int(
                                    uptime.get("TotalSeconds", 0) or 0
                                )

                            guest = HyperVGuest(
                                vmid=vm_id,
                                name=detail.get("Name", "Unknown"),
                                state=str(detail.get("State", "Unknown")),
                                cpus=detail.get("ProcessorCount", 0) or 0,
                                memory_mb=memory_mb,
                                uptime=uptime_seconds,
                                generation=detail.get("Generation", 1) or 1,
                                version=str(detail.get("Version", "")),
                                path=detail.get("Path", "") or "",
                                dynamic_memory=bool(
                                    detail.get("DynamicMemoryEnabled", False)
                                ),
                                checkpoints_enabled=bool(
                                    detail.get("CheckpointsEnabled", True)
                                ),
                                owner_node=node_name,
                                is_clustered=True,
                            )
                            guests.append(guest)

                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Failed to parse VM details from {node_name}: {e}"
                        )

            except Exception as e:
                logger.warning(f"Failed to query node '{node_name}': {e}")
                # Add basic info from topology for VMs we couldn't query
                for vm in node_vms:
                    guest = HyperVGuest(
                        vmid=vm.get("VmId", ""),
                        name=vm.get("VmName", "Unknown"),
                        state=vm.get("State", "Unknown"),
                        cpus=0,
                        memory_mb=0,
                        uptime=0,
                        generation=1,
                        version="",
                        path="",
                        dynamic_memory=False,
                        checkpoints_enabled=True,
                        owner_node=node_name,
                        is_clustered=True,
                    )
                    guests.append(guest)

        logger.info(f"Listed {len(guests)} VMs across cluster")
        return guests

    def _list_guests_fallback(self) -> list[HyperVGuest]:
        """Fallback method to list VMs by querying each node directly.

        Used when Invoke-Command fails (e.g., CredSSP not configured).

        Returns:
            List of HyperVGuest objects
        """
        logger.info("Using fallback method to list cluster VMs")

        # First get the cluster nodes
        nodes = self.get_cluster_nodes()
        if not nodes:
            # If we can't get nodes, try the connected host
            return super().list_guests()

        all_guests: dict[str, HyperVGuest] = {}  # Use dict to dedup by vmid

        for node_info in nodes:
            node_name = node_info.get("Name", "")
            if node_info.get("State") != "Up":
                logger.debug(f"Skipping offline node: {node_name}")
                continue

            try:
                node_api = self._get_or_create_node_connection(node_name)
                node_vms = node_api.list_guests()

                for vm in node_vms:
                    # Add owner node info
                    vm.owner_node = node_name
                    vm.is_clustered = True
                    all_guests[vm.vmid] = vm

            except Exception as e:
                logger.warning(f"Failed to list VMs on node {node_name}: {e}")

        return list(all_guests.values())

    def get_guest(self, vm_name: str) -> HyperVGuest | None:
        """Get a specific VM by name, finding it across cluster nodes.

        Args:
            vm_name: VM name

        Returns:
            HyperVGuest object or None if not found
        """
        # First find which node owns the VM
        owner_node = self.get_vm_owner_node(vm_name)

        if owner_node:
            # Query the owner node directly
            node_api = self._get_or_create_node_connection(owner_node)
            guest = node_api.get_guest(vm_name)
            if guest:
                guest.owner_node = owner_node
                guest.is_clustered = True
            return guest

        # Fallback: search all nodes
        nodes = self.get_cluster_nodes()
        for node_info in nodes:
            node_name = node_info.get("Name", "")
            if node_info.get("State") != "Up":
                continue

            try:
                node_api = self._get_or_create_node_connection(node_name)
                guest = node_api.get_guest(vm_name)
                if guest:
                    guest.owner_node = node_name
                    guest.is_clustered = True
                    return guest
            except Exception as e:
                logger.debug(f"VM not found on node {node_name}: {e}")

        return None

    def get_guest_by_id(self, vmid: str) -> HyperVGuest | None:
        """Get a specific VM by GUID, finding it across cluster nodes.

        Args:
            vmid: VM GUID

        Returns:
            HyperVGuest object or None if not found
        """
        # Search for VM by ID across cluster
        script = f"""
$vmGroups = Get-ClusterGroup | Where-Object {{ $_.GroupType -eq 'VirtualMachine' }}
foreach ($group in $vmGroups) {{
    $vmRes = Get-ClusterResource -InputObject $group |
        Where-Object {{ $_.ResourceType -eq 'Virtual Machine' }}
    if ($vmRes) {{
        $id = (Get-ClusterParameter -InputObject $vmRes -Name VmId `
            -ErrorAction SilentlyContinue).Value
        if ($id -eq '{vmid}') {{
            $group.OwnerNode.Name
            break
        }}
    }}
}}
"""
        rc, stdout, stderr = self._run_powershell(script)

        if rc == 0 and stdout.strip():
            owner_node = stdout.strip()
            node_api = self._get_or_create_node_connection(owner_node)
            guest = node_api.get_guest_by_id(vmid)
            if guest:
                guest.owner_node = owner_node
                guest.is_clustered = True
            return guest

        # Fallback: search all nodes
        nodes = self.get_cluster_nodes()
        for node_info in nodes:
            node_name = node_info.get("Name", "")
            if node_info.get("State") != "Up":
                continue

            try:
                node_api = self._get_or_create_node_connection(node_name)
                guest = node_api.get_guest_by_id(vmid)
                if guest:
                    guest.owner_node = node_name
                    guest.is_clustered = True
                    return guest
            except Exception:
                pass

        return None

    def live_migrate_vm(
        self,
        vm_name: str,
        target_node: str,
        migration_type: str = "Live",
    ) -> tuple[bool, str]:
        """Live migrate a VM to a different cluster node.

        Args:
            vm_name: VM name to migrate
            target_node: Destination cluster node
            migration_type: Migration type (Live, Quick, Shutdown, ShutdownForce, TurnOff)

        Returns:
            Tuple of (success, message)
        """
        valid_types = ["Live", "Quick", "Shutdown", "ShutdownForce", "TurnOff"]
        if migration_type not in valid_types:
            return False, f"Invalid migration type. Must be one of: {valid_types}"

        script = f"""
$ErrorActionPreference = 'Stop'
try {{
    Move-ClusterVirtualMachineRole -Name '{vm_name}' -Node '{target_node}' `
        -MigrationType {migration_type} -Wait 0
    "Migration initiated successfully"
}} catch {{
    "ERROR: $($_.Exception.Message)"
}}
"""
        rc, stdout, stderr = self._run_powershell(script, timeout=300)

        output = stdout.strip()
        if rc != 0 or output.startswith("ERROR:"):
            error_msg = output.replace("ERROR: ", "") if output else stderr
            logger.error(f"Live migration of '{vm_name}' failed: {error_msg}")
            return False, f"Migration failed: {error_msg}"

        logger.info(f"Live migration of '{vm_name}' to '{target_node}' initiated")
        return True, f"Migration of '{vm_name}' to '{target_node}' initiated"

    def add_vm_to_cluster(
        self, vm_name: str, node: str | None = None
    ) -> tuple[bool, str]:
        """Add a VM to the failover cluster for high availability.

        Args:
            vm_name: VM name to add to cluster
            node: Optional node where the VM exists. If provided, connects
                  directly to that node to run Add-ClusterVirtualMachineRole
                  to avoid double-hop authentication issues.

        Returns:
            Tuple of (success, message)
        """
        # If node is specified, connect directly to that node to avoid double-hop issues
        # Double-hop issue: Backer -> Node1 -> Node4 requires CredSSP delegation
        # Solution: Connect directly to Node4 where the VM exists
        if node:
            # Get connection to the specific node where VM exists
            node_api = self._get_node_api(node)
            if not node_api:
                error_msg = f"Could not connect to node '{node}'"
                logger.error(error_msg)
                return False, error_msg

            # Run Add-ClusterVirtualMachineRole on the node where VM exists
            script = f"""
$ErrorActionPreference = 'Stop'
try {{
    Add-ClusterVirtualMachineRole -VMName '{vm_name}'
    "VM added to cluster successfully"
}} catch {{
    # On failure, gather path info to help diagnose storage issues
    $pathInfo = ""
    try {{
        $vm = Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue
        if ($vm) {{
            $vmPath = $vm.Path
            $vhdPaths = ($vm.HardDrives | ForEach-Object {{ $_.Path }}) -join ', '
            $pathInfo = " (VM Path: $vmPath; VHDs: $vhdPaths)"
        }}
    }} catch {{ }}
    "ERROR: $($_.Exception.Message)$pathInfo"
}}
"""
            # Run on the specific node
            rc, stdout, stderr = node_api._run_powershell(script)
        else:
            # No node specified - run on default connection
            script = f"""
$ErrorActionPreference = 'Stop'
try {{
    Add-ClusterVirtualMachineRole -VMName '{vm_name}'
    "VM added to cluster successfully"
}} catch {{
    # On failure, gather path info to help diagnose storage issues
    $pathInfo = ""
    try {{
        $vm = Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue
        if ($vm) {{
            $vmPath = $vm.Path
            $vhdPaths = ($vm.HardDrives | ForEach-Object {{ $_.Path }}) -join ', '
            $pathInfo = " (VM Path: $vmPath; VHDs: $vhdPaths)"
        }}
    }} catch {{ }}
    "ERROR: $($_.Exception.Message)$pathInfo"
}}
"""
            rc, stdout, stderr = self._run_powershell(script)

        output = stdout.strip()
        if rc != 0 or output.startswith("ERROR:"):
            error_msg = output.replace("ERROR: ", "") if output else stderr
            logger.error(f"Failed to add '{vm_name}' to cluster: {error_msg}")
            return False, f"Failed to add VM to cluster: {error_msg}"

        logger.info(f"Added '{vm_name}' to failover cluster")
        return True, f"VM '{vm_name}' added to cluster"

    def remove_vm_from_cluster(self, vm_name: str) -> tuple[bool, str]:
        """Remove a VM from the failover cluster.

        Note: This does not delete the VM, just removes HA protection.

        Args:
            vm_name: VM name to remove from cluster

        Returns:
            Tuple of (success, message)
        """
        script = f"""
$ErrorActionPreference = 'Stop'
try {{
    $group = Get-ClusterGroup -Name '{vm_name}' -ErrorAction Stop
    Remove-ClusterGroup -Name '{vm_name}' -RemoveResources -Force
    "VM removed from cluster successfully"
}} catch {{
    "ERROR: $($_.Exception.Message)"
}}
"""
        rc, stdout, stderr = self._run_powershell(script)

        output = stdout.strip()
        if rc != 0 or output.startswith("ERROR:"):
            error_msg = output.replace("ERROR: ", "") if output else stderr
            logger.error(f"Failed to remove '{vm_name}' from cluster: {error_msg}")
            return False, f"Failed to remove VM from cluster: {error_msg}"

        logger.info(f"Removed '{vm_name}' from failover cluster")
        return True, f"VM '{vm_name}' removed from cluster"

    def shutdown_vm(self, vm_name: str, timeout: int = 300) -> tuple[bool, str]:
        """Gracefully shutdown a clustered VM.

        Routes the command to the owner node.

        Args:
            vm_name: VM name
            timeout: Timeout in seconds

        Returns:
            Tuple of (success, message)
        """
        owner_node = self.get_vm_owner_node(vm_name)
        if owner_node:
            node_api = self._get_or_create_node_connection(owner_node)
            success = node_api.shutdown_vm(vm_name, timeout)
            if success:
                return True, f"VM '{vm_name}' shutdown on node '{owner_node}'"
            return False, f"Failed to shutdown VM '{vm_name}' on node '{owner_node}'"
        return False, f"Could not find owner node for VM '{vm_name}'"

    def stop_vm(self, vm_name: str, force: bool = False) -> tuple[bool, str]:
        """Stop a clustered VM.

        Routes the command to the owner node.

        Args:
            vm_name: VM name
            force: Force stop (turn off)

        Returns:
            Tuple of (success, message)
        """
        owner_node = self.get_vm_owner_node(vm_name)
        if owner_node:
            node_api = self._get_or_create_node_connection(owner_node)
            success = node_api.stop_vm(vm_name, force)
            if success:
                return True, f"VM '{vm_name}' stopped on node '{owner_node}'"
            return False, f"Failed to stop VM '{vm_name}' on node '{owner_node}'"
        return False, f"Could not find owner node for VM '{vm_name}'"

    def start_vm(self, vm_name: str, node: str | None = None) -> tuple[bool, str]:
        """Start a clustered VM.

        Routes the command to the owner node, or specified node.

        Args:
            vm_name: VM name
            node: Optional node to start VM on. If not provided, looks up owner.

        Returns:
            Tuple of (success, message)
        """
        target_node = node or self.get_vm_owner_node(vm_name)
        if target_node:
            node_api = self._get_or_create_node_connection(target_node)
            success = node_api.start_vm(vm_name)
            if success:
                return True, f"VM '{vm_name}' started on node '{target_node}'"
            return False, f"Failed to start VM '{vm_name}' on node '{target_node}'"
        return False, f"Could not find owner node for VM '{vm_name}'"

    def capture_vm_config(self, vm_name: str) -> dict[str, Any] | None:
        """Capture comprehensive VM configuration from the owner node.

        Routes to the owner node and adds cluster-specific config.

        Args:
            vm_name: VM name

        Returns:
            Configuration dict or None
        """
        owner_node = self.get_vm_owner_node(vm_name)
        if not owner_node:
            logger.error(f"Could not find owner node for VM '{vm_name}'")
            return None

        node_api = self._get_or_create_node_connection(owner_node)
        config = node_api.capture_vm_config(vm_name)

        if config:
            # Add cluster-specific information
            config["cluster"] = config.get("cluster", {})
            config["cluster"]["ownerNodeAtBackup"] = owner_node
            config["cluster"]["isClustered"] = True

        return config


class HyperVClusterBackupManager(HyperVBackupManager):
    """Backup manager for Hyper-V Failover Clusters.

    Extends HyperVBackupManager to handle cluster-aware operations:
    - Routes backup operations to the correct owner node
    - Handles VM migration during backup if needed
    - Supports cluster-aware restore with optional re-clustering
    """

    def __init__(self, api: HyperVClusterAPI):
        """Initialize cluster backup manager.

        Args:
            api: HyperVClusterAPI instance for cluster communication
        """
        super().__init__(api)
        self.cluster_api: HyperVClusterAPI = api

    def list_all_guests(self) -> list[dict[str, Any]]:
        """List all VMs in the cluster in a format suitable for the API.

        Returns:
            List of guest info dicts with standardized fields
        """
        guests = []

        try:
            vms = self.cluster_api.list_guests()
            for vm in vms:
                guests.append(
                    {
                        "vmid": vm.vmid,
                        "name": vm.name,
                        "node": vm.owner_node or self.cluster_api.host,
                        "type": "vm",
                        "guest_type": HyperVGuestType.VM.value,
                        "status": vm.state.lower(),
                        "cpus": vm.cpus,
                        "maxmem_gb": vm.memory_gb,
                        "maxdisk_gb": 0,
                        "generation": vm.generation,
                        "is_clustered": vm.is_clustered,
                        "owner_node": vm.owner_node,
                    }
                )
        except Exception as e:
            logger.error(f"Failed to list cluster VMs: {e}")

        return guests

    def backup_vm(
        self,
        vm_name: str,
        backup_path: str,
        backup_mode: str = "online",
        progress_callback: Any | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
    ) -> dict[str, Any]:
        """Backup a VM in the cluster.

        Automatically routes to the owner node for the backup operation.

        Args:
            vm_name: VM name to backup
            backup_path: Destination path (should be accessible from all nodes)
            backup_mode: Backup mode (online, offline, checkpoint)
            progress_callback: Optional callback for progress updates
            smb_username: SMB username for network share authentication
            smb_password: SMB password for network share authentication
            smb_domain: SMB domain for network share authentication

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
            "owner_node": None,
        }

        try:
            # Find which node owns the VM
            owner_node = self.cluster_api.get_vm_owner_node(vm_name)
            if not owner_node:
                result["errors"].append(
                    f"Could not find VM '{vm_name}' in cluster"
                )
                return result

            result["owner_node"] = owner_node
            logger.info(f"VM '{vm_name}' is on node '{owner_node}', routing backup there")

            if progress_callback:
                progress_callback({
                    "status": "starting",
                    "vm": vm_name,
                    "mode": backup_mode,
                    "node": owner_node,
                })

            # Get connection to owner node and perform backup
            node_api = self.cluster_api._get_or_create_node_connection(owner_node)
            node_backup_manager = HyperVBackupManager(node_api)

            # Perform backup on owner node
            backup_result = node_backup_manager.backup_vm(
                vm_name=vm_name,
                backup_path=backup_path,
                backup_mode=backup_mode,
                progress_callback=progress_callback,
                smb_username=smb_username,
                smb_password=smb_password,
                smb_domain=smb_domain,
            )

            # Merge results
            result.update(backup_result)
            result["owner_node"] = owner_node

            # Add cluster info to the saved config file
            # The node backup doesn't know about cluster info, so we need to update it
            if backup_result.get("success") and backup_result.get("config_path"):
                config_path = backup_result["config_path"]
                try:
                    # Load the saved config, add cluster info, and save it back
                    self._update_config_with_cluster_info(
                        config_path=config_path,
                        owner_node=owner_node,
                        smb_username=smb_username,
                        smb_password=smb_password,
                        smb_domain=smb_domain,
                    )
                    logger.info(f"Added cluster info to backup config (owner: {owner_node})")
                except Exception as e:
                    logger.warning(f"Failed to update config with cluster info: {e}")

            # Update duration
            ended_at = dt.now()
            result["duration_seconds"] = (ended_at - started_at).total_seconds()

            return result

        except Exception as e:
            logger.exception(f"Cluster backup of '{vm_name}' failed")
            result["errors"].append(str(e))
            ended_at = dt.now()
            result["duration_seconds"] = (ended_at - started_at).total_seconds()
            return result

    def _update_config_with_cluster_info(
        self,
        config_path: str,
        owner_node: str,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
    ) -> None:
        """Update the saved config file with cluster-specific information.

        Args:
            config_path: UNC path to the config file
            owner_node: The node that owns this VM
            smb_username: SMB username
            smb_password: SMB password
            smb_domain: SMB domain
        """
        # Build credentials for SMB access
        safe_password = smb_password.replace("'", "''") if smb_password else ""
        full_username = f"{smb_domain}\\{smb_username}" if smb_domain and smb_username else smb_username or ""

        # Extract SMB UNC for net use
        smb_unc = None
        if config_path.startswith("\\\\"):
            unc_parts = config_path.lstrip("\\").split("\\")
            if len(unc_parts) >= 2:
                smb_server = unc_parts[0]
                smb_share = unc_parts[1]
                smb_unc = f"\\\\{smb_server}\\{smb_share}"

        script = f"""
$ErrorActionPreference = 'Stop'
$configPath = '{config_path}'
$smbUnc = '{smb_unc or ""}'
$smbUser = '{full_username}'
$smbPass = '{safe_password}'
$ownerNode = '{owner_node}'

# Mount SMB share if needed
if ($smbUnc -and $smbUser) {{
    $netUseResult = & net use $smbUnc /user:$smbUser $smbPass 2>&1
}}

try {{
    if (Test-Path $configPath) {{
        $content = Get-Content $configPath -Raw
        $config = $content | ConvertFrom-Json

        # Add or update cluster info
        # Create a new cluster object with the properties we need
        $clusterInfo = [PSCustomObject]@{{
            ownerNodeAtBackup = $ownerNode
            isClustered = $true
        }}

        # If cluster property exists, preserve other properties and update
        if ($config.cluster) {{
            # Copy existing properties
            $config.cluster.PSObject.Properties | ForEach-Object {{
                if ($_.Name -notin @('ownerNodeAtBackup', 'isClustered')) {{
                    $clusterInfo | Add-Member -NotePropertyName $_.Name -NotePropertyValue $_.Value -Force
                }}
            }}
        }}

        # Replace or add the cluster property
        if ($config.PSObject.Properties['cluster']) {{
            $config.cluster = $clusterInfo
        }} else {{
            $config | Add-Member -NotePropertyName 'cluster' -NotePropertyValue $clusterInfo -Force
        }}

        # Save back
        $config | ConvertTo-Json -Depth 20 | Set-Content $configPath -Encoding UTF8
        Write-Output "SUCCESS"
    }} else {{
        Write-Output "CONFIG_NOT_FOUND"
    }}
}} catch {{
    Write-Output "ERROR: $_"
}}
"""
        rc, stdout, stderr = self.cluster_api._run_powershell(script)
        if "SUCCESS" not in stdout:
            logger.warning(f"Config update result: {stdout.strip()}")

    def _cluster_inplace_restore(
        self,
        vm_name: str,
        import_path: str,
        existing_owner: str,
        target_node: str,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
        progress_callback: Any | None = None,
        backup_config: dict | None = None,
    ) -> dict[str, Any]:
        """Perform cluster-aware in-place restore.

        This handles restoring a VM that exists in the cluster, potentially on
        a different node than the target. For clustered VMs on shared storage
        (CSV), the VHDs are accessible from any node.

        Args:
            vm_name: Name of the VM to restore
            import_path: Path to backup files
            existing_owner: Current owner node of the VM
            target_node: Target node for the restore
            smb_username: SMB username for backup access
            smb_password: SMB password for backup access
            smb_domain: SMB domain
            progress_callback: Optional progress callback
            backup_config: Optional backup configuration

        Returns:
            Dict with restore result
        """
        result: dict[str, Any] = {
            "success": False,
            "vm_name": vm_name,
            "actual_mode": "inplace",
            "errors": [],
            "warnings": [],
        }

        try:
            # Get the VM using the cluster API (routes to correct owner node)
            existing_vm = self.cluster_api.get_guest(vm_name)
            if not existing_vm:
                result["errors"].append(f"VM '{vm_name}' not found in cluster")
                return result

            result["vm_id"] = existing_vm.vmid

            # Get connection to the VM's actual owner node for the restore
            owner_api = self.cluster_api._get_or_create_node_connection(existing_owner)
            owner_backup_manager = HyperVBackupManager(owner_api)

            if progress_callback:
                progress_callback({
                    "status": "inplace_restore",
                    "vm": vm_name,
                    "owner_node": existing_owner,
                })

            # Build SMB connection info
            smb_unc = None
            full_username = None
            safe_password = None
            if smb_username and smb_password and import_path.startswith("\\\\"):
                unc_parts = import_path.lstrip("\\").split("\\")
                if len(unc_parts) >= 2:
                    smb_server = unc_parts[0]
                    smb_share = unc_parts[1]
                    smb_unc = f"\\\\{smb_server}\\{smb_share}"
                    safe_password = smb_password.replace("'", "''")
                    full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username

            # Perform the in-place restore on the owner node
            logger.info(f"Performing in-place restore of '{vm_name}' on owner node '{existing_owner}'")
            success, message = owner_backup_manager._restore_inplace(
                vm_name,
                import_path,
                smb_unc,
                full_username,
                safe_password,
                progress_callback,
            )

            if success:
                result["success"] = True
                result["warnings"].append("In-place restore completed - VM configuration unchanged")
                logger.info(f"Cluster in-place restore of '{vm_name}' completed successfully")

                # If target node is different from current owner, optionally move VM
                if target_node and existing_owner:
                    target_short = target_node.split(".")[0].lower()
                    owner_short = existing_owner.split(".")[0].lower()
                    if target_short != owner_short:
                        result["warnings"].append(
                            f"VM remains on '{existing_owner}'. "
                            f"Use cluster manager to migrate to '{target_node}' if needed."
                        )
            else:
                result["errors"].append(f"In-place restore failed: {message}")
                logger.error(f"Cluster in-place restore failed: {message}")

            return result

        except Exception as e:
            logger.exception(f"Cluster in-place restore of '{vm_name}' failed")
            result["errors"].append(str(e))
            return result

    def restore_vm(
        self,
        import_path: str,
        vm_name: str | None = None,
        restore_path: str | None = None,
        vhd_dest_path: str | None = None,
        register_only: bool = False,
        generate_new_id: bool = True,
        start_after_restore: bool = False,
        progress_callback: Any | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        smb_domain: str | None = None,
        network_mapping: dict[str, str] | None = None,
        restore_mode: str = "auto",
        target_node: str | None = None,
        add_to_cluster: bool = True,
    ) -> dict[str, Any]:
        """Restore a VM to the cluster.

        Args:
            import_path: Path to backup files
            vm_name: Override VM name (optional)
            restore_path: Override VM storage path
            vhd_dest_path: Override VHD storage path
            register_only: Only register VM (don't copy files)
            generate_new_id: Generate a new VM ID
            start_after_restore: Start VM after restore
            progress_callback: Optional callback for progress updates
            smb_username: SMB username
            smb_password: SMB password
            smb_domain: SMB domain
            network_mapping: Map old switches to new ones
            restore_mode: Restore mode (auto, inplace, import, rebuild)
            target_node: Specific node to restore to (optional)
            add_to_cluster: Whether to add restored VM to cluster (default True)

        Returns:
            Dict with restore result info
        """
        from datetime import datetime as dt

        started_at = dt.now()
        result: dict[str, Any] = {
            "success": False,
            "vm_name": vm_name or "Unknown",
            "import_path": import_path,
            "restore_mode": restore_mode,
            "errors": [],
            "warnings": [],
            "target_node": target_node,
            "added_to_cluster": False,
        }

        try:
            # Ensure node IP map is populated for DNS fallback
            # This is needed when restore creates a fresh API instance
            if not self.cluster_api._node_ip_map:
                logger.info("Populating node IP map before restore")
                self.cluster_api.test_connection()

            # Load backup config to get original owner node and VM name
            backup_config = None
            if smb_username and smb_password:
                # Build SMB UNC for config loading
                if import_path.startswith("\\\\"):
                    unc_parts = import_path.lstrip("\\").split("\\")
                    if len(unc_parts) >= 2:
                        smb_server = unc_parts[0]
                        smb_share = unc_parts[1]
                        smb_unc = f"\\\\{smb_server}\\{smb_share}"
                        safe_password = smb_password.replace("'", "''") if smb_password else ""
                        full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username

                        # Use the cluster API to load config (it can connect to any node)
                        backup_manager_for_config = HyperVBackupManager(self.cluster_api)
                        backup_config = backup_manager_for_config._load_backup_config(
                            import_path, smb_unc, full_username, safe_password
                        )
                        if backup_config:
                            logger.info("Loaded backup config for node selection")

            # Determine VM name - need this to check if VM exists in cluster
            effective_vm_name = vm_name
            if not effective_vm_name:
                # Try to get from backup config
                if backup_config and backup_config.get("vm", {}).get("name"):
                    config_name = backup_config["vm"]["name"]
                    # Handle PowerShell JSON array quirk
                    if isinstance(config_name, list):
                        effective_vm_name = config_name[0] if config_name else None
                    else:
                        effective_vm_name = config_name
                    if effective_vm_name:
                        logger.info(f"Got VM name from backup config: {effective_vm_name}")
                # Fallback: extract from path
                if not effective_vm_name:
                    effective_vm_name = self._get_vm_name_from_path(import_path)
                    if effective_vm_name:
                        logger.info(f"Got VM name from path: {effective_vm_name}")

            # Check if VM currently exists in cluster
            existing_owner = None
            if effective_vm_name:
                existing_owner = self.cluster_api.get_vm_owner_node(effective_vm_name)
                if existing_owner:
                    logger.info(f"VM '{effective_vm_name}' currently exists on '{existing_owner}'")

            # Determine target node - user-specified takes priority
            if not target_node:
                # First priority: use original owner node from backup config
                if backup_config:
                    cluster_info = backup_config.get("cluster", {})
                    original_owner = cluster_info.get("ownerNodeAtBackup")
                    logger.info(f"Backup config cluster info: {cluster_info}")
                    if original_owner:
                        # Verify this node is online
                        nodes = self.cluster_api.get_cluster_nodes()
                        online_nodes = [n["Name"] for n in nodes if n.get("State") == "Up"]
                        # Check if original owner (or its short name) is online
                        original_short = original_owner.split(".")[0].lower()
                        for node in online_nodes:
                            node_short = node.split(".")[0].lower()
                            if node_short == original_short:
                                target_node = node
                                logger.info(
                                    f"Using original owner node '{target_node}' from backup config"
                                )
                                break
                        if not target_node:
                            logger.warning(
                                f"Original owner '{original_owner}' is not online, "
                                "will use another node"
                            )

                # Second priority: use current owner if VM exists
                if not target_node and existing_owner:
                    target_node = existing_owner
                    logger.info(f"Using current owner node: {target_node}")

                # Fallback: use first available online node
                if not target_node:
                    nodes = self.cluster_api.get_cluster_nodes()
                    online_nodes = [
                        n["Name"] for n in nodes if n.get("State") == "Up"
                    ]
                    if online_nodes:
                        target_node = online_nodes[0]
                        logger.info(f"Using first available node: {target_node}")
                    else:
                        target_node = self.cluster_api.host

            result["target_node"] = target_node
            logger.info(f"Restoring VM to cluster node '{target_node}'")

            if progress_callback:
                progress_callback({
                    "status": "starting",
                    "vm": effective_vm_name or vm_name,
                    "mode": restore_mode,
                    "target_node": target_node,
                })

            # For clustered VMs, we need special handling for in-place restore
            # because the VM may exist on a different node than the target
            use_cluster_inplace = False
            if existing_owner and restore_mode in ("auto", "inplace"):
                # VM exists in cluster - for in-place, use cluster-aware restore
                use_cluster_inplace = True
                logger.info(
                    f"VM exists in cluster on '{existing_owner}', "
                    "using cluster-aware in-place restore"
                )

            if use_cluster_inplace:
                # Use cluster-aware in-place restore
                # This handles the case where VM is on a different node than target
                restore_result = self._cluster_inplace_restore(
                    vm_name=effective_vm_name,  # Use the resolved VM name
                    import_path=import_path,
                    existing_owner=existing_owner,
                    target_node=target_node,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                    progress_callback=progress_callback,
                    backup_config=backup_config,
                )

                # If in-place failed and we're in auto mode, fall back to rebuild
                if not restore_result.get("success") and restore_mode == "auto":
                    logger.info("Cluster in-place restore failed, falling back to rebuild")
                    # Save context from failed in-place attempt
                    inplace_warnings = restore_result.get("warnings", [])
                    inplace_errors = restore_result.get("errors", [])
                    fallback_msg = (
                        f"In-place restore failed: {inplace_errors}, "
                        "falling back to rebuild"
                    )

                    # Extract original paths from backup config for rebuild fallback
                    fallback_restore_path = restore_path
                    fallback_vhd_dest_path = vhd_dest_path
                    if backup_config and add_to_cluster:
                        vm_info = backup_config.get("vm", {})
                        original_path = vm_info.get("path", "")
                        if isinstance(original_path, list):
                            original_path = original_path[0] if original_path else ""
                        if original_path and not fallback_restore_path:
                            fallback_restore_path = original_path
                            logger.info(
                                f"Using original path for rebuild fallback: {fallback_restore_path}"
                            )
                        if not fallback_vhd_dest_path:
                            hard_drives = backup_config.get("hardDrives", [])
                            if hard_drives:
                                first_hd = hard_drives[0] if isinstance(hard_drives, list) else {}
                                hd_path = first_hd.get("path", "")
                                if isinstance(hd_path, list):
                                    hd_path = hd_path[0] if hd_path else ""
                                if hd_path:
                                    import os
                                    fallback_vhd_dest_path = os.path.dirname(hd_path)
                                    logger.info(
                                        f"Using original VHD path for rebuild fallback: {fallback_vhd_dest_path}"
                                    )

                    # Get connection to target node for rebuild
                    node_api = self.cluster_api._get_or_create_node_connection(target_node)
                    node_backup_manager = HyperVBackupManager(node_api)
                    restore_result = node_backup_manager.restore_vm(
                        import_path=import_path,
                        vm_name=vm_name,
                        restore_path=fallback_restore_path,
                        vhd_destination_path=fallback_vhd_dest_path,
                        generate_new_id=generate_new_id,
                        start_after_restore=False,
                        progress_callback=progress_callback,
                        smb_username=smb_username,
                        smb_password=smb_password,
                        smb_domain=smb_domain,
                        network_mapping=network_mapping,
                        restore_mode="rebuild",
                    )

                    # Preserve context from the in-place failure in the result
                    restore_result["warnings"] = restore_result.get("warnings", [])
                    restore_result["warnings"].insert(0, fallback_msg)
                    restore_result["warnings"].extend(inplace_warnings)
            else:
                # Standard node-based restore (import, rebuild, or new VM)
                node_api = self.cluster_api._get_or_create_node_connection(target_node)
                node_backup_manager = HyperVBackupManager(node_api)

                # When VM is not in cluster, check for orphaned local registrations
                # This happens when a VM is deleted from cluster but local registration remains
                effective_restore_mode = restore_mode
                if not existing_owner and effective_vm_name:
                    # Check if there's an orphaned local VM registration on the target node
                    orphan_check_script = f"""
$vm = Get-VM -Name '{effective_vm_name}' -ErrorAction SilentlyContinue
if ($vm) {{
    "ORPHAN_FOUND"
}} else {{
    "NO_ORPHAN"
}}
"""
                    rc, stdout, stderr = self.cluster_api._run_powershell_on_node(
                        target_node, orphan_check_script
                    )
                    if "ORPHAN_FOUND" in stdout:
                        logger.warning(
                            f"Found orphaned local VM registration for '{effective_vm_name}' "
                            f"on node '{target_node}' - removing before restore"
                        )
                        # Remove the orphaned registration AND clean up VM files
                        # to prevent "file in use" errors during rebuild
                        remove_script = f"""
$ErrorActionPreference = 'Stop'
try {{
    $vm = Get-VM -Name '{effective_vm_name}' -ErrorAction Stop
    # Only remove if it's not actually running
    if ($vm.State -eq 'Off' -or $vm.State -eq 'Saved') {{
        # Capture paths before removing VM
        $vmPath = $vm.Path
        $configPath = $vm.ConfigurationLocation

        # Remove VM registration
        Remove-VM -Name '{effective_vm_name}' -Force

        # Wait for Hyper-V to release file handles
        Start-Sleep -Seconds 3

        # Clean up VM config files (Virtual Machines folder)
        # This prevents "file in use" errors during rebuild
        if ($vmPath -and (Test-Path $vmPath)) {{
            $vmConfigFolder = Join-Path $vmPath "Virtual Machines"
            if (Test-Path $vmConfigFolder) {{
                try {{
                    Remove-Item -Path $vmConfigFolder -Recurse -Force -ErrorAction Stop
                }} catch {{
                    # Fallback to cmd for stubborn files
                    & cmd /c rd /s /q "$vmConfigFolder" 2>&1 | Out-Null
                }}
            }}
        }}

        "REMOVED"
    }} else {{
        "RUNNING"
    }}
}} catch {{
    "ERROR: $($_.Exception.Message)"
}}
"""
                        rc, stdout, stderr = self.cluster_api._run_powershell_on_node(
                            target_node, remove_script
                        )
                        if "REMOVED" in stdout:
                            logger.info(
                                f"Removed orphaned VM registration for '{effective_vm_name}'"
                            )
                        elif "RUNNING" in stdout:
                            logger.warning(
                                f"Orphaned VM '{effective_vm_name}' is running - "
                                "cannot remove, restore may fail"
                            )
                        else:
                            logger.warning(
                                f"Failed to remove orphaned VM: {stdout} {stderr}"
                            )

                # When VM doesn't exist in cluster, don't use in-place mode
                # The base class might find leftover registrations on individual nodes
                # Keep "auto" to allow fallback to rebuild if import fails
                if not existing_owner and restore_mode == "inplace":
                    # Only override if explicitly set to inplace (can't inplace a deleted VM)
                    effective_restore_mode = "auto"
                    logger.info(
                        "VM not in cluster, using 'auto' mode instead of 'inplace' "
                        "(will try import, then rebuild if needed)"
                    )
                elif not existing_owner and restore_mode == "auto":
                    logger.info(
                        "VM not in cluster, using 'auto' mode (will try import, then rebuild)"
                    )

                # For deleted VMs being restored to cluster, use original path from backup config
                # This ensures VM is restored to the same location (clustered storage) it came from
                effective_restore_path = restore_path
                effective_vhd_dest_path = vhd_dest_path
                if not existing_owner and backup_config and add_to_cluster:
                    # VM doesn't exist - restore to original location for cluster compatibility
                    vm_info = backup_config.get("vm", {})
                    original_path = vm_info.get("path", "")
                    # Handle PowerShell JSON array quirk
                    if isinstance(original_path, list):
                        original_path = original_path[0] if original_path else ""

                    # Use original VM path from backup (where it was on clustered storage)
                    if original_path and not effective_restore_path:
                        effective_restore_path = original_path
                        logger.info(
                            f"Using original path for restore: {effective_restore_path}"
                        )

                    # Also extract VHD path from hard drives if not specified
                    if not effective_vhd_dest_path:
                        hard_drives = backup_config.get("hardDrives", [])
                        if hard_drives:
                            first_hd = hard_drives[0] if isinstance(hard_drives, list) else {}
                            hd_path = first_hd.get("path", "")
                            if isinstance(hd_path, list):
                                hd_path = hd_path[0] if hd_path else ""
                            if hd_path:
                                # Extract parent directory of VHD for destination
                                # e.g., \\storage\vms\VMName\Virtual Hard Disks\disk.vhdx
                                # -> \\storage\vms\VMName\Virtual Hard Disks
                                import os
                                effective_vhd_dest_path = os.path.dirname(hd_path)
                                logger.info(
                                    f"Using original VHD path: {effective_vhd_dest_path}"
                                )

                # Perform restore on target node
                restore_result = node_backup_manager.restore_vm(
                    import_path=import_path,
                    vm_name=vm_name,
                    restore_path=effective_restore_path,
                    vhd_destination_path=effective_vhd_dest_path,
                    generate_new_id=generate_new_id,
                    start_after_restore=False,  # Don't start yet, add to cluster first
                    progress_callback=progress_callback,
                    smb_username=smb_username,
                    smb_password=smb_password,
                    smb_domain=smb_domain,
                    network_mapping=network_mapping,
                    restore_mode=effective_restore_mode,
                )

            # Merge results
            result.update(restore_result)
            result["target_node"] = target_node

            # Add to cluster if restore succeeded and requested
            if restore_result.get("success") and add_to_cluster:
                restored_vm_name = restore_result.get("vm_name", vm_name)
                if restored_vm_name:
                    # Check if already in cluster
                    owner = self.cluster_api.get_vm_owner_node(restored_vm_name)
                    if not owner:
                        logger.info(
                            f"Adding restored VM '{restored_vm_name}' to cluster"
                        )
                        if progress_callback:
                            progress_callback({
                                "status": "adding_to_cluster",
                                "vm": restored_vm_name,
                            })

                        # Pass node so add_vm_to_cluster knows where to find the VM
                        # The function uses Get-VM -ComputerName to get the VMId,
                        # then runs Add-ClusterVirtualMachineRole on the default
                        # connection to avoid WinRM double-hop issues
                        success, msg = self.cluster_api.add_vm_to_cluster(
                            restored_vm_name, node=target_node
                        )
                        if success:
                            result["added_to_cluster"] = True
                            logger.info(f"VM '{restored_vm_name}' added to cluster")
                        else:
                            result["warnings"].append(
                                f"Failed to add VM to cluster: {msg}"
                            )
                    else:
                        result["added_to_cluster"] = True
                        logger.info(
                            f"VM '{restored_vm_name}' already in cluster "
                            f"on node '{owner}'"
                        )

            # Start VM if requested (after adding to cluster)
            if (
                restore_result.get("success")
                and start_after_restore
                and restore_result.get("vm_name")
            ):
                restored_vm_name = restore_result["vm_name"]
                logger.info(f"Starting restored VM '{restored_vm_name}'")
                # Pass target_node in case VM is not yet visible in cluster
                start_success, start_msg = self.cluster_api.start_vm(
                    restored_vm_name, node=target_node
                )
                if not start_success:
                    result["warnings"].append(
                        f"VM restored but failed to start: {start_msg}"
                    )
                    logger.warning(
                        f"Failed to start restored VM '{restored_vm_name}': {start_msg}"
                    )

            ended_at = dt.now()
            result["duration_seconds"] = (ended_at - started_at).total_seconds()
            return result

        except Exception as e:
            logger.exception("Cluster restore failed")
            result["errors"].append(str(e))
            ended_at = dt.now()
            result["duration_seconds"] = (ended_at - started_at).total_seconds()
            return result

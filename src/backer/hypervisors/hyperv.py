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
    ) -> dict[str, Any]:
        """Restore (import) a Hyper-V VM from an export.

        Per Microsoft Import-VM documentation, this supports two modes:
        - Register in-place: Uses -Path only (files stay where they are)
        - Copy: Uses -Copy flag with optional destination paths

        For SMB/UNC paths, the backup files are first copied to local storage
        before import, since Import-VM may have issues with network paths.

        Args:
            import_path: Path to the exported VM folder containing Virtual Machines subfolder
            vm_name: Optional new name for the VM after import
            restore_path: Optional destination path for VM configuration files
                         (maps to -VirtualMachinePath parameter)
            vhd_destination_path: Optional destination for VHD files
                                 (maps to -VhdDestinationPath parameter)
            generate_new_id: Generate new VM ID (required for duplicate imports)
            progress_callback: Optional callback for progress updates
            smb_username: SMB username for network path authentication
            smb_password: SMB password for network path authentication
            smb_domain: SMB domain for network path authentication

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

            # Check if source is a UNC path (network share)
            is_unc_path = import_path.startswith("\\\\")

            if is_unc_path and smb_username and smb_password:
                # For UNC paths, copy backup to local temp, then import
                # This avoids authentication issues with Import-VM on network paths
                unc_parts = import_path.lstrip("\\").split("\\")
                if len(unc_parts) >= 2:
                    smb_server = unc_parts[0]
                    smb_share = unc_parts[1]
                    smb_unc = f"\\\\{smb_server}\\{smb_share}"
                    safe_password = smb_password.replace("'", "''")
                    full_username = f"{smb_domain}\\{smb_username}" if smb_domain else smb_username

                    # Build import parameters for the script
                    import_params = ""
                    if generate_new_id:
                        import_params += " -Copy -GenerateNewId"
                    if vhd_destination_path:
                        import_params += f" -VhdDestinationPath '{vhd_destination_path}'"
                    if restore_path:
                        import_params += f" -VirtualMachinePath '{restore_path}'"

                    script = f"""
$ErrorActionPreference = 'Stop'
$importPath = '{import_path}'
$smbUnc = '{smb_unc}'
$smbUser = '{full_username}'
$smbPass = '{safe_password}'

try {{
    # Establish SMB connection with credentials
    $netUseResult = & net use $smbUnc /user:$smbUser $smbPass 2>&1
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 2) {{
        throw "Failed to connect to SMB share: $netUseResult"
    }}

    # Verify source exists
    if (-not (Test-Path $importPath)) {{
        throw "Backup path not found: $importPath"
    }}

    # Find the .vmcx file or vm_config.json (manual backup fallback)
    $vmFolder = Join-Path $importPath 'Virtual Machines'
    $vmcxPath = $null
    $manualBackupConfig = $null

    if (Test-Path $vmFolder) {{
        $vmcx = Get-ChildItem -Path $vmFolder -Filter '*.vmcx' -ErrorAction SilentlyContinue `
            | Select-Object -First 1
        if ($vmcx) {{
            $vmcxPath = $vmcx.FullName
        }} else {{
            # Check for manual backup JSON config
            $jsonConfig = Join-Path $vmFolder 'vm_config.json'
            if (Test-Path $jsonConfig) {{
                $manualBackupConfig = Get-Content $jsonConfig -Raw | ConvertFrom-Json
            }} else {{
                throw "No .vmcx file or vm_config.json found in $vmFolder"
            }}
        }}
    }} else {{
        $vmcx = Get-ChildItem -Path $importPath -Filter '*.vmcx' -Recurse -ErrorAction SilentlyContinue `
            | Select-Object -First 1
        if ($vmcx) {{
            $vmcxPath = $vmcx.FullName
        }} else {{
            # Check for manual backup JSON config
            $jsonConfig = Get-ChildItem -Path $importPath -Filter 'vm_config.json' -Recurse `
                -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($jsonConfig) {{
                $manualBackupConfig = Get-Content $jsonConfig.FullName -Raw | ConvertFrom-Json
            }} else {{
                throw "No .vmcx file or vm_config.json found in $importPath"
            }}
        }}
    }}

    # Get default paths
    $defaultVmPath = (Get-VMHost).VirtualMachinePath
    $defaultVhdPath = (Get-VMHost).VirtualHardDiskPath

    # Get VM name from backup folder
    $vmName = Split-Path -Leaf $importPath

    # Check if VM with this name exists and stop it
    $existingVm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
    if ($existingVm) {{
        if ($existingVm.State -ne 'Off') {{
            Stop-VM -Name $vmName -Force -TurnOff
            # Wait for VM to fully stop
            $timeout = 60
            $waited = 0
            while ((Get-VM -Name $vmName).State -ne 'Off' -and $waited -lt $timeout) {{
                Start-Sleep -Seconds 2
                $waited += 2
            }}
            if ((Get-VM -Name $vmName).State -ne 'Off') {{
                throw "Timed out waiting for VM '$vmName' to stop"
            }}
        }}
        # Remove the existing VM (but keep VHDs for safety)
        Remove-VM -Name $vmName -Force
    }}

    # Helper function to create VM from VHDs (used for manual backups and vTPM fallback)
    function New-VMFromVHDs {{
        param(
            [string]$VMName,
            [string]$ImportPath,
            [string]$DefaultVmPath,
            [string]$DefaultVhdPath,
            [int]$Generation = 2,
            [long]$MemoryBytes = 4GB,
            [int]$ProcessorCount = 2,
            [bool]$EnableTPM = $true
        )

        # Find VHD files
        $vhdFolder = Join-Path $ImportPath 'Virtual Hard Disks'
        $vhds = @()
        if (Test-Path $vhdFolder) {{
            # Only get base VHDx files, skip AVHDX differencing disks
            $vhds = Get-ChildItem -Path $vhdFolder -Include *.vhdx,*.vhd -Recurse |
                Where-Object {{ $_.Extension -ne '.avhdx' }}
        }}
        if ($vhds.Count -eq 0) {{
            throw "No VHD files found in backup for VM creation"
        }}

        # Copy VHDs to local storage
        $localVhdPath = Join-Path $DefaultVhdPath $VMName
        if (Test-Path $localVhdPath) {{
            Remove-Item -Path $localVhdPath -Recurse -Force
        }}
        New-Item -ItemType Directory -Path $localVhdPath -Force | Out-Null

        $localVhds = @()
        foreach ($vhd in $vhds) {{
            $destVhd = Join-Path $localVhdPath $vhd.Name
            Copy-Item -Path $vhd.FullName -Destination $destVhd -Force
            $localVhds += $destVhd
        }}

        # Create new VM with specified generation
        $vm = New-VM -Name $VMName -Generation $Generation -MemoryStartupBytes $MemoryBytes `
            -Path $DefaultVmPath -NoVHD

        # Set processor count
        Set-VMProcessor -VMName $VMName -Count $ProcessorCount

        # Attach copied VHDs
        foreach ($vhd in $localVhds) {{
            Add-VMHardDiskDrive -VMName $VMName -Path $vhd -ControllerType SCSI
        }}

        # Enable vTPM with new local key protector (for Gen2 VMs only)
        if ($EnableTPM -and $Generation -eq 2) {{
            try {{
                Set-VMKeyProtector -VMName $VMName -NewLocalKeyProtector
                Enable-VMTPM -VMName $VMName
            }} catch {{
                # TPM setup may fail, continue without it
                Write-Warning "Could not enable TPM: $_"
            }}
        }}

        return $vm
    }}

    $vm = $null

    # Check if this is a manual backup (vm_config.json instead of .vmcx)
    if ($manualBackupConfig) {{
        # Manual backup - create VM from VHDs using saved config
        $generation = if ($manualBackupConfig.Generation) {{ $manualBackupConfig.Generation }} else {{ 2 }}
        $memoryBytes = if ($manualBackupConfig.MemoryStartupBytes) {{
            [long]$manualBackupConfig.MemoryStartupBytes
        }} else {{ 4GB }}
        $processorCount = if ($manualBackupConfig.ProcessorCount) {{
            $manualBackupConfig.ProcessorCount
        }} else {{ 2 }}

        $vm = New-VMFromVHDs -VMName $vmName -ImportPath $importPath `
            -DefaultVmPath $defaultVmPath -DefaultVhdPath $defaultVhdPath `
            -Generation $generation -MemoryBytes $memoryBytes `
            -ProcessorCount $processorCount -EnableTPM $true
    }} else {{
        # Standard backup with .vmcx - try Import-VM first
        $importError = $null
        try {{
            $vm = Import-VM -Path $vmcxPath -Copy -GenerateNewId `
                -VirtualMachinePath $defaultVmPath `
                -VhdDestinationPath $defaultVhdPath `
                -ErrorAction Stop
        }} catch {{
            $importError = $_
        }}

        # If import failed (likely vTPM issue), fall back to creating new VM from VHDs
        if (-not $vm -and $importError) {{
            # Check if it's a vTPM/vmgs related error
            if ($importError.Exception.Message -match 'vmgs|0x80070032|not supported|used by another') {{
                $vm = New-VMFromVHDs -VMName $vmName -ImportPath $importPath `
                    -DefaultVmPath $defaultVmPath -DefaultVhdPath $defaultVhdPath `
                    -Generation 2 -MemoryBytes 4GB -ProcessorCount 2 -EnableTPM $true
            }} else {{
                throw $importError
            }}
        }}
    }}

    if ($vm) {{
        @{{
            Success = $true
            VMId = $vm.Id.ToString()
            VMName = $vm.Name
        }} | ConvertTo-Json
    }} else {{
        @{{ Success = $false; Error = "Import returned no VM" }} | ConvertTo-Json
    }}
}} finally {{
    # Disconnect SMB
    & net use $smbUnc /delete /y 2>&1 | Out-Null
}}
"""
                    rc, stdout, stderr = self.api._run_powershell_large(script, timeout=86400)
                else:
                    result["errors"].append("Invalid UNC path format")
                    return result
            else:
                # Local path - import directly
                # First, find the .vmcx configuration file
                find_vmcx_script = f"""
$importPath = '{import_path}'
if ($importPath -like '*.vmcx') {{
    if (Test-Path $importPath) {{
        $importPath
    }} else {{
        Write-Error "VMCX file not found: $importPath"
        exit 1
    }}
}} else {{
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
        $vmcx = Get-ChildItem -Path $importPath -Filter '*.vmcx' -Recurse -ErrorAction SilentlyContinue `
            | Select-Object -First 1
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
                rc, stdout, stderr = self.api._run_powershell(script, timeout=86400)

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

                    $configFile = if ($vmcxFiles) {{ $vmcxFiles[0].FullName }} else {{ Join-Path $vmcxPath 'vm_config.json' }}
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

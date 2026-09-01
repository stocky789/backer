"""Windows service support for Backer agent.

This module provides functionality to install and manage Backer as a Windows service.
Supports multiple installation methods:
- Windows Service (via sc.exe or pywin32) - Runs at boot, survives lockscreen/logoff
- Scheduled Task with SYSTEM account - Runs at startup without user login
- Scheduled Task at logon - Original method, requires user login
- Startup folder script - Legacy method
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def get_python_path() -> str:
    """Get path to current Python executable."""
    return sys.executable


def get_service_executable_path() -> str:
    """Return the dedicated non-interactive service executable."""
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).with_name("backer-agent-service.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _prepare_service_config() -> None:
    """Copy the interactive user's registered agent config for SYSTEM."""
    config_dir = os.environ.get("BACKER_CONFIG_DIR")
    source = Path(config_dir) if config_dir else Path(os.environ.get("APPDATA", "")) / "Backer"
    config = source / "config.yaml"
    if not config.exists():
        config = source / "agent.yaml"
    if not config.exists():
        raise FileNotFoundError("Agent config not found; register the agent before installing the service")
    target = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Backer"
    target_existed = target.exists()
    target.mkdir(parents=True, exist_ok=True)
    target_config = target / config.name
    shutil.copy2(config, target_config)
    result = subprocess.run(
        [
            "icacls",
            str(target),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "/grant:r",
            "*S-1-5-32-544:(OI)(CI)F",
            "/remove:g",
            "*S-1-5-32-545",
            "/t",
            "/c",
        ],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )
    if result.returncode != 0:
        target_config.unlink(missing_ok=True)
        if not target_existed:
            try:
                target.rmdir()
            except OSError:
                pass
        raise OSError("Failed to restrict service config permissions")


def get_startup_folder() -> Path:
    """Get Windows startup folder for current user."""
    if not is_windows():
        raise RuntimeError("Not running on Windows")

    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup


def is_admin() -> bool:
    """Check if running with administrator privileges."""
    if not is_windows():
        return False

    try:
        import ctypes

        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_subprocess_flags() -> int:
    """Get subprocess creation flags to hide console window on Windows."""
    if is_windows():
        return subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0x08000000
    return 0


# =============================================================================
# Windows Service Methods (Best for lockscreen/background operation)
# =============================================================================


def create_background_scheduled_task(server_url: str | None = None) -> tuple[bool, str]:
    """Create a scheduled task that runs at system startup under SYSTEM account.

    This method runs the agent at boot time (before any user logs in) and
    continues running regardless of user login state. The agent will run
    even when:
    - No user is logged in
    - User is at the lockscreen
    - User logs off
    - Different user logs in

    Requires Administrator privileges.

    Args:
        server_url: Optional server URL to connect to

    Returns:
        Tuple of (success, message)
    """
    if not is_windows():
        return False, "Windows scheduled task only available on Windows"

    if not is_admin():
        return False, "Administrator privileges required. Run as Administrator."

    try:
        _prepare_service_config()
    except OSError as e:
        return False, f"Failed to prepare service config: {e}"

    task_name = "BackerAgentService"

    # Determine the command to run
    if getattr(sys, "frozen", False):
        # Running as PyInstaller exe
        command = get_service_executable_path()
        arguments = ""
    else:
        # Running from Python
        command = get_python_path()
        if server_url:
            arguments = f'-m backer agent start --server "{server_url}"'
        else:
            arguments = "-m backer agent start"

    # Delete existing task if any
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    # Create XML task definition for more control
    # This allows us to set "Run whether user is logged on or not"
    task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Backer Backup Agent - Background service for automated backups</Description>
    <Author>Backer</Author>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""

    # Write XML to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(task_xml)
        xml_path = f.name

    try:
        # Create task from XML
        result = subprocess.run(
            ["schtasks", "/create", "/tn", task_name, "/xml", xml_path, "/f"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )

        if result.returncode != 0:
            # Fall back to simpler command-line approach
            return _create_background_task_simple(server_url)

        # Start the task immediately
        subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            capture_output=True,
            creationflags=get_subprocess_flags(),
        )

        return True, (
            f"Background service task '{task_name}' created successfully.\n"
            "The agent will:\n"
            "  - Start automatically at system boot\n"
            "  - Run in the background (no login required)\n"
            "  - Continue running at lockscreen\n"
            "  - Auto-restart on failure"
        )

    finally:
        # Clean up temp file
        try:
            os.unlink(xml_path)
        except Exception:
            pass


def create_local_scheduled_task() -> tuple[bool, str]:
    """Install the independent SYSTEM minute trigger for ``job run --due``."""
    if not is_windows():
        return False, "Windows scheduled task only available on Windows"
    if not is_admin():
        return False, "Administrator privileges required. Run as Administrator."
    data_dir = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Backer"
    command = get_service_executable_path() if getattr(sys, "frozen", False) else get_python_path()
    arguments = (
        "job run --due --no-progress" if getattr(sys, "frozen", False) else "-m backer job run --due --no-progress"
    )
    task_name = "BackerLocalSchedule"
    action = (
        f'cmd.exe /d /c "set BACKER_DATA_DIR={data_dir}&& set BACKER_CONFIG_DIR={data_dir}'
        f'&& set BACKER_RUN_AS_SYSTEM=1&& "{command}" {arguments}"'
    )
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"], capture_output=True, creationflags=get_subprocess_flags()
    )
    result = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn",
            task_name,
            "/tr",
            action,
            "/sc",
            "minute",
            "/mo",
            "1",
            "/ru",
            "SYSTEM",
            "/rl",
            "highest",
            "/f",
        ],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )
    if result.returncode:
        return False, result.stderr.strip() or "Failed to create local backup task"
    return True, f"Local backup task '{task_name}' created."


def create_local_scheduled_test_task(token: str) -> tuple[bool, str]:
    """Run one configured local job once as SYSTEM without changing the due scheduler."""
    if not is_windows():
        return False, "Windows scheduled task only available on Windows"
    if not is_admin():
        return False, "Administrator privileges required. Run as Administrator."
    # The GUI executable owns the explicit scheduled-test dispatch when frozen.
    command = sys.executable if getattr(sys, "frozen", False) else get_python_path()
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        return False, "Invalid scheduled test token"
    arguments = (
        f"scheduled-test {token}" if getattr(sys, "frozen", False) else f"-m backer.serverless.scheduled_test {token}"
    )
    task_name = f"BackerLocalTest-{token}"
    action = subprocess.list2cmdline([command, *arguments.split()])
    result = subprocess.run(
        [
            "schtasks", "/create", "/tn", task_name, "/tr", action, "/sc", "once", "/st", "00:00", "/ru", "SYSTEM",
            "/rl", "highest", "/f",
        ],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )
    if result.returncode:
        return False, result.stderr.strip() or "Failed to create scheduled test task"
    launched = subprocess.run(
        ["schtasks", "/run", "/tn", task_name], capture_output=True, text=True, creationflags=get_subprocess_flags()
    )
    if launched.returncode:
        remove_local_scheduled_test_task(token)
        return False, launched.stderr.strip() or "Failed to start scheduled test task"
    return True, task_name


def remove_local_scheduled_test_task(token: str) -> tuple[bool, str]:
    """Remove only the short-lived selected-job SYSTEM test task."""
    if not is_windows() or not re.fullmatch(r"[0-9a-f]{12}", token):
        return False, "Invalid scheduled test token"
    task = f"BackerLocalTest-{token}"
    stopped = subprocess.run(
        ["schtasks", "/end", "/tn", task], capture_output=True, text=True, creationflags=get_subprocess_flags()
    )
    try:
        state = _windows_task_state(task)
    except OSError as error:
        return False, f"Could not verify scheduled test task stopped: {error}"
    if not state.get("exists"):
        return True, "Scheduled test task was already stopped"
    if state.get("exists") and state.get("running"):
        detail = stopped.stderr.strip() or "schtasks could not stop the task"
        return False, f"Scheduled test task is still running; isolated credentials were retained: {detail}"
    deleted = subprocess.run(
        ["schtasks", "/delete", "/tn", task, "/f"],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )
    if deleted.returncode:
        return False, deleted.stderr.strip() or "Could not remove scheduled test task"
    return True, "Scheduled test task stopped and removed"


def remove_local_scheduled_task() -> bool:
    """Remove only the SYSTEM task created for local serverless scheduling."""
    if not is_windows():
        return False
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", "BackerLocalSchedule", "/f"],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )
    return result.returncode == 0


def _windows_task_state(task_name: str) -> dict[str, object]:
    """Read the full task definition and its current state without changing it."""
    definition = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/xml"],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )
    if definition.returncode:
        detail = (definition.stderr or definition.stdout).lower()
        if "cannot find" in detail or "does not exist" in detail:
            return {"exists": False}
        raise OSError(definition.stderr.strip() or "Could not read local scheduled task")
    status = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "list", "/v"],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )
    if status.returncode:
        raise OSError(status.stderr.strip() or "Could not read local scheduled task state")
    return {
        "exists": True,
        "definition": definition.stdout,
        "enabled": "<enabled>true</enabled>" in definition.stdout.lower(),
        "running": "status: running" in status.stdout.lower(),
    }


def snapshot_local_scheduler() -> dict[str, object]:
    """Capture the actual platform scheduler before a settings transaction mutates it."""
    if is_windows():
        return {"platform": "windows", "task": _windows_task_state("BackerLocalSchedule")}
    directory = Path.home() / ".config" / "systemd" / "user"
    units = {}
    for suffix in ("service", "timer"):
        path = directory / f"backer-local.{suffix}"
        units[suffix] = path.read_bytes() if path.exists() else None
    state = {}
    for unit in ("backer-local.timer", "backer-local.service"):
        enabled = subprocess.run(["systemctl", "--user", "is-enabled", unit], capture_output=True, text=True)
        active = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
        state[unit] = {"enabled": enabled.returncode == 0, "running": active.returncode == 0}
    return {"platform": "linux", "directory": directory, "units": units, "state": state}


def prepare_local_scheduler_mutation(snapshot: dict[str, object]) -> tuple[bool, str]:
    """Freeze the trigger and recheck its job before changing its definition.

    A settings save must never delete a definition while a scheduler-triggered
    backup is running.  Disabling/stopping only the trigger closes the race
    between the initial read and the later definition change; an already-started
    service is left alone and the trigger is restored before returning failure.
    """
    if snapshot.get("platform") == "windows":
        task = snapshot.get("task", {})
        if not isinstance(task, dict) or not task.get("exists"):
            return True, "No local scheduled task to freeze"
        if task.get("running"):
            return False, "Local scheduled backup is running; retry after it finishes"
        disabled = subprocess.run(
            ["schtasks", "/change", "/tn", "BackerLocalSchedule", "/disable"],
            capture_output=True,
            text=True,
            creationflags=get_subprocess_flags(),
        )
        if disabled.returncode:
            return False, disabled.stderr.strip() or "Could not freeze local scheduled task"
        current = _windows_task_state("BackerLocalSchedule")
        if not current.get("running"):
            return True, "Local scheduled task is idle"
        if task.get("enabled"):
            enabled = subprocess.run(
                ["schtasks", "/change", "/tn", "BackerLocalSchedule", "/enable"],
                capture_output=True,
                text=True,
                creationflags=get_subprocess_flags(),
            )
            if enabled.returncode:
                return False, "Local scheduled backup started; task could not be re-enabled"
        return False, "Local scheduled backup started; retry after it finishes"

    state = snapshot.get("state", {})
    units = snapshot.get("units", {})
    if not isinstance(state, dict) or not isinstance(units, dict) or not any(units.values()):
        return True, "No local systemd timer to freeze"
    service_state = state.get("backer-local.service", {})
    if isinstance(service_state, dict) and service_state.get("running"):
        return False, "Local scheduled backup is running; retry after it finishes"
    stopped = subprocess.run(["systemctl", "--user", "stop", "backer-local.timer"], capture_output=True, text=True)
    if stopped.returncode:
        return False, stopped.stderr.strip() or "Could not freeze local backup timer"
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "backer-local.service"], capture_output=True, text=True
    )
    if active.returncode:
        return True, "Local systemd timer is idle"
    timer_state = state.get("backer-local.timer", {})
    if isinstance(timer_state, dict) and timer_state.get("running"):
        restarted = subprocess.run(["systemctl", "--user", "start", "backer-local.timer"], capture_output=True)
        if restarted.returncode:
            return False, "Local scheduled backup started; timer could not be restored"
    return False, "Local scheduled backup started; retry after it finishes"


def restore_local_scheduler(snapshot: dict[str, object]) -> tuple[bool, str]:
    """Restore exactly the scheduler state captured by :func:`snapshot_local_scheduler`."""
    if snapshot.get("platform") == "windows":
        task = snapshot["task"]
        if not isinstance(task, dict):
            return False, "Invalid local scheduled task snapshot"
        current = _windows_task_state("BackerLocalSchedule")
        if not task.get("exists"):
            if current.get("exists"):
                removed = subprocess.run(
                    ["schtasks", "/delete", "/tn", "BackerLocalSchedule", "/f"],
                    capture_output=True,
                    text=True,
                    creationflags=get_subprocess_flags(),
                )
                if removed.returncode:
                    return False, removed.stderr.strip() or "Could not remove local scheduled task"
            return True, "Local scheduled task restored"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", delete=False) as handle:
            handle.write(str(task["definition"]))
            xml_path = handle.name
        try:
            restored = subprocess.run(
                ["schtasks", "/create", "/tn", "BackerLocalSchedule", "/xml", xml_path, "/f"],
                capture_output=True,
                text=True,
                creationflags=get_subprocess_flags(),
            )
        finally:
            Path(xml_path).unlink(missing_ok=True)
        if restored.returncode:
            return False, restored.stderr.strip() or "Could not restore local scheduled task"
        command = ["schtasks", "/run", "/tn", "BackerLocalSchedule"] if task.get("running") else [
            "schtasks", "/end", "/tn", "BackerLocalSchedule"
        ]
        changed = subprocess.run(command, capture_output=True, text=True, creationflags=get_subprocess_flags())
        if changed.returncode and task.get("running"):
            return False, changed.stderr.strip() or "Could not restore local scheduled task state"
        restored_state = _windows_task_state("BackerLocalSchedule")
        if bool(restored_state.get("running")) != bool(task.get("running")):
            return False, "Could not restore local scheduled task running state"
        return True, "Local scheduled task restored"

    directory = snapshot.get("directory")
    units = snapshot.get("units")
    state = snapshot.get("state")
    if not isinstance(directory, Path) or not isinstance(units, dict) or not isinstance(state, dict):
        return False, "Invalid local systemd snapshot"
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in ("service", "timer"):
        path = directory / f"backer-local.{suffix}"
        content = units.get(suffix)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    for unit, wanted in state.items():
        if not isinstance(wanted, dict):
            return False, "Invalid local systemd state snapshot"
        enabled = "enable" if wanted.get("enabled") else "disable"
        active = "start" if wanted.get("running") else "stop"
        enabled_result = subprocess.run(["systemctl", "--user", enabled, unit], capture_output=True, text=True)
        active_result = subprocess.run(["systemctl", "--user", active, unit], capture_output=True, text=True)
        if enabled_result.returncode or (active_result.returncode and wanted.get("running")):
            return False, f"Could not restore {unit}"
    return True, "Local systemd timer restored"


def _create_background_task_simple(server_url: str | None = None) -> tuple[bool, str]:
    """Fallback method using simpler schtasks command."""
    task_name = "BackerAgentService"

    # Determine the command to run
    if getattr(sys, "frozen", False):
        cmd = [get_service_executable_path()]
    else:
        python_exe = get_python_path()
        cmd = [python_exe, "-m", "backer", "agent", "start"]
        if server_url:
            cmd.extend(["--server", server_url])

    # Delete existing task
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    # Create task that runs at startup under SYSTEM account
    result = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn",
            task_name,
            "/tr",
            subprocess.list2cmdline(cmd),
            "/sc",
            "onstart",
            "/ru",
            "SYSTEM",
            "/rl",
            "highest",
            "/f",
        ],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip() if result.stderr else "Unknown error"
        return False, f"Failed to create background task: {error_msg}"

    # Start the task immediately
    subprocess.run(
        ["schtasks", "/run", "/tn", task_name],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    return True, (f"Background task '{task_name}' created.\nAgent will start at system boot and run in background.")


def remove_background_scheduled_task() -> bool:
    """Remove the background scheduled task."""
    if not is_windows():
        return False

    task_name = "BackerAgentService"

    # Stop the task first
    subprocess.run(
        ["schtasks", "/end", "/tn", task_name],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    # Delete the task
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    return result.returncode == 0


def get_background_task_status() -> dict[str, Any]:
    """Get status of the background scheduled task."""
    if not is_windows():
        return {"installed": False, "running": False, "method": None}

    task_name = "BackerAgentService"

    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "csv", "/v"],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )

    if result.returncode != 0:
        return {"installed": False, "running": False, "method": None}

    lines = result.stdout.strip().split("\n")
    if len(lines) < 2:
        return {"installed": False, "running": False, "method": None}

    # Parse CSV output
    try:
        headers = lines[0].strip('"').split('","')
        values = lines[1].strip('"').split('","')

        status_idx = headers.index("Status") if "Status" in headers else 2
        status = values[status_idx] if len(values) > status_idx else "Unknown"

        return {
            "installed": True,
            "status": status,
            "running": status == "Running",
            "method": "background_task",
            "task_name": task_name,
        }
    except Exception:
        return {"installed": True, "running": False, "method": "background_task"}


# =============================================================================
# Legacy Methods (User logon-based)
# =============================================================================


def create_startup_script() -> Path:
    """Create a VBS script to start the agent silently at login."""
    if not is_windows():
        raise RuntimeError("Not running on Windows")

    startup_folder = get_startup_folder()
    vbs_path = startup_folder / "backer-agent.vbs"

    python_exe = get_python_path()

    # VBScript to run Python silently
    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """{python_exe}"" -m backer agent start", 0, False
'''

    vbs_path.write_text(vbs_content)
    return vbs_path


def remove_startup_script() -> bool:
    """Remove the startup script."""
    if not is_windows():
        return False

    startup_folder = get_startup_folder()
    vbs_path = startup_folder / "backer-agent.vbs"

    if vbs_path.exists():
        vbs_path.unlink()
        return True
    return False


def create_scheduled_task(server_url: str | None = None) -> bool:
    """Create a Windows scheduled task to run agent at logon.

    This is the legacy method that requires a user to be logged in.
    For background operation (lockscreen support), use create_background_scheduled_task().
    """
    if not is_windows():
        return False

    python_exe = get_python_path()
    task_name = "BackerAgent"

    # Build command
    cmd = [python_exe, "-m", "backer", "agent", "start"]
    if server_url:
        cmd.extend(["--server", server_url])

    # Delete existing task if any
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    # Create new task
    result = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn",
            task_name,
            "/tr",
            subprocess.list2cmdline(cmd),
            "/sc",
            "onlogon",
            "/rl",
            "highest",
            "/f",
        ],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )

    return result.returncode == 0


def remove_scheduled_task() -> bool:
    """Remove the Windows scheduled task."""
    if not is_windows():
        return False

    task_name = "BackerAgent"

    result = subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
        creationflags=get_subprocess_flags(),
    )

    return result.returncode == 0


def get_task_status() -> dict[str, Any]:
    """Get status of the scheduled task (legacy logon-based task)."""
    if not is_windows():
        return {"installed": False, "running": False}

    task_name = "BackerAgent"

    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "csv"],
        capture_output=True,
        text=True,
        creationflags=get_subprocess_flags(),
    )

    if result.returncode != 0:
        return {"installed": False, "running": False}

    lines = result.stdout.strip().split("\n")
    if len(lines) < 2:
        return {"installed": False, "running": False}

    # Parse CSV output
    values = lines[1].strip('"').split('","')
    status = values[2] if len(values) > 2 else "Unknown"

    return {
        "installed": True,
        "status": status,
        "running": status == "Running",
        "method": "logon_task",
    }


# =============================================================================
# Unified Install/Uninstall Interface
# =============================================================================


def install_service(method: str = "service", server_url: str | None = None) -> tuple[bool, str]:
    """Install the Backer agent as a Windows service/startup item.

    Args:
        method: Installation method:
            - "service" (default): Background service that runs at boot (recommended)
            - "task": Scheduled task at user logon (legacy)
            - "startup": Startup folder script (legacy)
        server_url: Optional server URL to connect to

    Returns:
        Tuple of (success, message)
    """
    if not is_windows():
        return False, "Windows service installation only available on Windows"

    if method == "service":
        # Recommended: Background service that survives lockscreen
        return create_background_scheduled_task(server_url)

    elif method == "task":
        # Legacy: Task at user logon
        success = create_scheduled_task(server_url)
        if success:
            return True, "Scheduled task 'BackerAgent' created. Agent will start at login."
        else:
            return False, "Failed to create scheduled task. Try running as administrator."

    elif method == "startup":
        # Legacy: Startup folder script
        try:
            path = create_startup_script()
            return True, f"Startup script created at: {path}"
        except Exception as e:
            return False, f"Failed to create startup script: {e}"

    else:
        return False, f"Unknown installation method: {method}"


def uninstall_service() -> tuple[bool, str]:
    """Remove the Backer agent from Windows startup.

    Removes all installation methods (service, task, startup script).

    Returns:
        Tuple of (success, message)
    """
    if not is_windows():
        return False, "Windows service removal only available on Windows"

    removed_items = []

    # Remove background service task
    if remove_background_scheduled_task():
        removed_items.append("background service task")

    # Remove legacy scheduled task
    if remove_scheduled_task():
        removed_items.append("scheduled task")

    # Remove startup script
    if remove_startup_script():
        removed_items.append("startup script")

    if removed_items:
        return True, f"Removed: {', '.join(removed_items)}"
    else:
        return False, "No startup items found to remove"


def get_service_status() -> dict[str, Any]:
    """Get status for each Windows installation method.

    Returns:
        Dictionary with status information for all methods
    """
    if not is_windows():
        return {"installed": False, "method": None}

    # Check background task first (preferred)
    bg_status = get_background_task_status()
    if bg_status.get("installed"):
        return bg_status

    # Check legacy task
    task_status = get_task_status()
    if task_status.get("installed"):
        return task_status

    # Check startup script
    startup_folder = get_startup_folder()
    vbs_path = startup_folder / "backer-agent.vbs"
    if vbs_path.exists():
        return {
            "installed": True,
            "running": False,  # Can't determine from script
            "method": "startup_script",
        }

    return {"installed": False, "method": None}


# =============================================================================
# Linux systemd support
# =============================================================================


def create_systemd_service() -> tuple[bool, str]:
    """Create a systemd user service for Linux."""
    if is_windows():
        return False, "Systemd services only available on Linux"

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)

    service_path = systemd_dir / "backer-agent.service"
    python_exe = get_python_path()

    service_content = f"""[Unit]
Description=Backer Backup Agent
After=network.target

[Service]
Type=simple
ExecStart={python_exe} -m backer agent start
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""

    service_path.write_text(service_content)

    # Enable and start the service
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "backer-agent"], capture_output=True)

    return True, f"Systemd service created at: {service_path}\nEnable with: systemctl --user enable --now backer-agent"


def create_local_systemd_timer(*, headless: bool = False) -> tuple[bool, str]:
    """Install only Backer's local scheduler units, never the server-agent unit."""
    if is_windows():
        return False, "Systemd services only available on Linux"
    user = os.environ.get("USER", "")
    if not headless:
        linger = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger", "--value"], capture_output=True, text=True
        )
        if linger.returncode or linger.stdout.strip().lower() != "yes":
            enabled = subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True)
            verify = subprocess.run(
                ["loginctl", "show-user", user, "-p", "Linger", "--value"], capture_output=True, text=True
            )
            if enabled.returncode or verify.returncode or verify.stdout.strip().lower() != "yes":
                return False, f"loginctl enable-linger {user}"
        directory = Path.home() / ".config" / "systemd" / "user"
        systemctl = ["systemctl", "--user"]
    else:
        directory = Path("/etc/systemd/system")
        systemctl = ["systemctl"]
    directory.mkdir(parents=True, exist_ok=True)
    prefix = "backer-local"
    service = directory / f"{prefix}.service"
    timer = directory / f"{prefix}.timer"
    command = f"{get_python_path()} -m backer job run --due --no-progress"
    service.write_text(f"[Service]\nType=oneshot\nExecStart={command}\n", encoding="utf-8")
    timer.write_text(
        "[Unit]\nDescription=Backer local backup scheduler\n\n[Timer]\n"
        "OnCalendar=*-*-* *:*:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    subprocess.run([*systemctl, "daemon-reload"], capture_output=True)
    result = subprocess.run([*systemctl, "enable", "--now", f"{prefix}.timer"], capture_output=True, text=True)
    if result.returncode:
        return False, result.stderr.strip() or "Failed to enable local backup timer"
    return True, f"Local backup timer created at: {timer}"


def remove_local_systemd_timer(*, headless: bool = False) -> tuple[bool, str]:
    """Remove only units created by :func:`create_local_systemd_timer`."""
    directory = Path("/etc/systemd/system") if headless else Path.home() / ".config" / "systemd" / "user"
    systemctl = ["systemctl"] if headless else ["systemctl", "--user"]
    disabled = subprocess.run([*systemctl, "disable", "--now", "backer-local.timer"], capture_output=True, text=True)
    if disabled.returncode:
        return False, disabled.stderr.strip() or "Could not disable local backup timer"
    try:
        for suffix in ("service", "timer"):
            (directory / f"backer-local.{suffix}").unlink(missing_ok=True)
    except OSError as error:
        return False, f"Could not remove local backup timer files: {error}"
    reload = subprocess.run([*systemctl, "daemon-reload"], capture_output=True, text=True)
    if reload.returncode:
        return False, reload.stderr.strip() or "Could not reload systemd"
    return True, "Local backup timer removed"


def create_local_systemd_test_service(token: str) -> tuple[bool, str]:
    """Start one selected job through a short-lived root systemd service."""
    if is_windows() or not re.fullmatch(r"[0-9a-f]{12}", token):
        return False, "Invalid scheduled test request"
    service = Path("/etc/systemd/system") / f"backer-local-test-{token}.service"
    service.write_text(
        "[Service]\nType=oneshot\n"
        f"ExecStart={get_python_path()} -m backer.serverless.scheduled_test {token}\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    result = subprocess.run(["systemctl", "start", service.name], capture_output=True, text=True)
    if result.returncode:
        service.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        return False, result.stderr.strip() or "Failed to start root scheduled test service"
    return True, service.stem


def remove_local_systemd_test_service(token: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        return False, "Invalid scheduled test token"
    service = Path("/etc/systemd/system") / f"backer-local-test-{token}.service"
    if not service.exists():
        return True, "Scheduled test service was already stopped"
    stopped = subprocess.run(["systemctl", "stop", service.name], capture_output=True, text=True)
    state = subprocess.run(
        ["systemctl", "show", service.name, "-p", "ActiveState", "--value"], capture_output=True, text=True
    )
    active = state.stdout.strip().lower()
    if stopped.returncode or state.returncode or active not in {"inactive", "failed"}:
        detail = stopped.stderr.strip() or state.stderr.strip() or active or "unknown state"
        return False, f"Scheduled test service is still active; isolated credentials were retained: {detail}"
    service.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    return True, "Root scheduled test service stopped and removed"

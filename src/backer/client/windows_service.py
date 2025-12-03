"""Windows service support for Backer agent.

This module provides functionality to install and manage Backer as a Windows service.
Uses the Windows Task Scheduler for simplicity (no additional dependencies).
"""

import os
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def get_python_path() -> str:
    """Get path to current Python executable."""
    return sys.executable


def get_startup_folder() -> Path:
    """Get Windows startup folder for current user."""
    if not is_windows():
        raise RuntimeError("Not running on Windows")

    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup


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

    This is more reliable than the startup folder method.
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
    )

    # Create new task
    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", task_name,
            "/tr", subprocess.list2cmdline(cmd),
            "/sc", "onlogon",
            "/rl", "highest",
            "/f",
        ],
        capture_output=True,
        text=True,
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
    )

    return result.returncode == 0


def get_task_status() -> dict:
    """Get status of the scheduled task."""
    if not is_windows():
        return {"installed": False, "running": False}

    task_name = "BackerAgent"

    result = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/fo", "csv"],
        capture_output=True,
        text=True,
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
    }


def install_service(method: str = "task", server_url: str | None = None) -> tuple[bool, str]:
    """Install the Backer agent as a Windows service/startup item.

    Args:
        method: Installation method - "task" (scheduled task) or "startup" (startup folder)
        server_url: Optional server URL to connect to

    Returns:
        Tuple of (success, message)
    """
    if not is_windows():
        return False, "Windows service installation only available on Windows"

    if method == "task":
        success = create_scheduled_task(server_url)
        if success:
            return True, "Scheduled task 'BackerAgent' created. Agent will start at login."
        else:
            return False, "Failed to create scheduled task. Try running as administrator."

    elif method == "startup":
        try:
            path = create_startup_script()
            return True, f"Startup script created at: {path}"
        except Exception as e:
            return False, f"Failed to create startup script: {e}"

    else:
        return False, f"Unknown installation method: {method}"


def uninstall_service() -> tuple[bool, str]:
    """Remove the Backer agent from Windows startup.

    Returns:
        Tuple of (success, message)
    """
    if not is_windows():
        return False, "Windows service removal only available on Windows"

    removed_task = remove_scheduled_task()
    removed_startup = remove_startup_script()

    if removed_task or removed_startup:
        return True, "Backer agent removed from startup"
    else:
        return False, "No startup items found to remove"


# Linux systemd support
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

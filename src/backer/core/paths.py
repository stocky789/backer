"""Platform paths shared by Backer client configuration and data."""

import os
import sys
from pathlib import Path


def _user_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Backer"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "backer"


def _machine_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Backer"
    return Path("/etc/backer")


def get_config_dir() -> Path:
    """Return the directory containing the active client configuration."""
    if configured := os.environ.get("BACKER_CONFIG_DIR"):
        return Path(configured)
    user_dir = _user_config_dir()
    if (user_dir / "config.yaml").exists():
        return user_dir
    machine_dir = _machine_config_dir()
    if (machine_dir / "config.yaml").exists():
        return machine_dir
    return user_dir


def get_data_dir() -> Path:
    """Return the directory holding local client run state."""
    if configured := os.environ.get("BACKER_DATA_DIR"):
        return Path(configured)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Backer"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "backer"

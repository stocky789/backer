#!/usr/bin/env python3
"""Build script for Backer Windows Agent executable.

This script:
1. Downloads rclone and restic for Windows
2. Builds the PyInstaller executable
3. Creates a zip package with everything needed

Usage:
    python scripts/build_agent.py

Requirements:
    pip install pyinstaller
"""

import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# Versions to bundle
RCLONE_VERSION = "1.65.0"
RESTIC_VERSION = "0.16.2"

# Build directories
BUILD_DIR = Path("build")
DIST_DIR = Path("dist")
TOOLS_DIR = BUILD_DIR / "tools"
DIST_TOOLS_DIR = DIST_DIR / "tools"


def download_file(url: str, dest: Path) -> None:
    """Download a file with progress."""
    print(f"  Downloading {url}...")
    urllib.request.urlretrieve(url, dest)
    print(f"  Saved to {dest}")


def download_rclone_windows() -> Path:
    """Download rclone for Windows."""
    print("Downloading rclone...")
    url = f"https://downloads.rclone.org/v{RCLONE_VERSION}/rclone-v{RCLONE_VERSION}-windows-amd64.zip"
    zip_path = TOOLS_DIR / "rclone.zip"

    download_file(url, zip_path)

    # Extract
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(TOOLS_DIR)

    # Find the exe
    exe_path = TOOLS_DIR / f"rclone-v{RCLONE_VERSION}-windows-amd64" / "rclone.exe"
    return exe_path


def download_restic_windows() -> Path:
    """Download restic for Windows."""
    print("Downloading restic...")
    url = f"https://github.com/restic/restic/releases/download/v{RESTIC_VERSION}/restic_{RESTIC_VERSION}_windows_amd64.zip"
    zip_path = TOOLS_DIR / "restic.zip"

    download_file(url, zip_path)

    # Extract
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(TOOLS_DIR)

    exe_path = TOOLS_DIR / "restic.exe"
    # Sometimes it's in a subfolder
    if not exe_path.exists():
        exe_path = TOOLS_DIR / f"restic_{RESTIC_VERSION}_windows_amd64.exe"
    return exe_path


def build_pyinstaller() -> Path:
    """Build the PyInstaller executable."""
    print("Building executable with PyInstaller...")

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "backer-agent.spec"],
        check=True,
    )

    return DIST_DIR / "backer-agent.exe"


def create_package() -> Path:
    """Create the final distribution package."""
    print("Creating distribution package...")

    package_dir = DIST_DIR / "backer-agent-windows"
    package_dir.mkdir(parents=True, exist_ok=True)
    DIST_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    # Copy executable
    shutil.copy(DIST_DIR / "backer-agent.exe", package_dir / "backer-agent.exe")

    # Copy tools if on Windows and they exist
    tools_subdir = package_dir / "tools"
    tools_subdir.mkdir(exist_ok=True)

    for tool in ["rclone.exe", "restic.exe"]:
        for src in TOOLS_DIR.rglob(tool):
            shutil.copy(src, tools_subdir / tool)
            shutil.copy(src, DIST_TOOLS_DIR / tool)
            break

    # Create a simple batch launcher
    launcher = package_dir / "start-agent.bat"
    launcher.write_text('''@echo off
echo Backer Agent
echo.
echo Usage:
echo   backer-agent.exe register --server http://your-server:8420
echo   backer-agent.exe start
echo   backer-agent.exe install
echo.
pause
''')

    # Create README
    readme = package_dir / "README.txt"
    readme.write_text('''Backer Agent for Windows
========================

Quick Start:
1. Open Command Prompt as Administrator
2. Register with your server:
   backer-agent.exe register --server http://your-server:8420

3. Install as Windows service:
   backer-agent.exe install

4. Or run manually:
   backer-agent.exe start

The agent will:
- Connect to your Backer server
- Receive and execute backup jobs
- Report status back to the server

For more info, visit: https://github.com/stocky789/backer
''')

    # Create zip
    from backer import __version__
    zip_name = f"backer-agent-{__version__}-windows-amd64.zip"
    zip_path = DIST_DIR / zip_name

    print(f"Creating {zip_path}...")
    shutil.make_archive(
        str(zip_path).replace('.zip', ''),
        'zip',
        DIST_DIR,
        "backer-agent-windows"
    )

    return zip_path


def main():
    print("=" * 50)
    print("Backer Agent Build Script")
    print("=" * 50)
    print()

    # Create directories
    BUILD_DIR.mkdir(exist_ok=True)
    TOOLS_DIR.mkdir(exist_ok=True)
    DIST_DIR.mkdir(exist_ok=True)
    DIST_TOOLS_DIR.mkdir(exist_ok=True)

    # Only download Windows tools if building on Windows
    if platform.system() == "Windows":
        download_rclone_windows()
        download_restic_windows()
    else:
        print("Note: Not on Windows, skipping tool downloads")
        print("      (tools will be downloaded on first run)")

    # Build
    build_pyinstaller()

    # Package
    if platform.system() == "Windows":
        zip_path = create_package()
        print()
        print("=" * 50)
        print(f"Build complete: {zip_path}")
        print("=" * 50)
    else:
        print()
        print("=" * 50)
        print(f"Build complete: {DIST_DIR / 'backer-agent.exe'}")
        print("Note: Run on Windows for full package with tools")
        print("=" * 50)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build script for Backer Windows Agent executable.

This script:
1. Downloads rclone, restic, and kopia for Windows
2. Builds GUI and unattended service executables
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
from pathlib import Path

# Versions to bundle

# Build directories
BUILD_DIR = Path("build")
DIST_DIR = Path("dist")
TOOLS_DIR = BUILD_DIR / "tools"
DIST_TOOLS_DIR = DIST_DIR / "tools"
VERSION_FILE = BUILD_DIR / "backer-agent-version.txt"


def _version_parts(version: str) -> tuple[int, int, int, int]:
    parts = [int(part) for part in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def write_pyinstaller_version_file() -> Path:
    """Write Windows version metadata consumed by PyInstaller."""
    from backer import __version__

    filevers = _version_parts(__version__)
    version_csv = ", ".join(str(part) for part in filevers)
    version_text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({version_csv}),
    prodvers=({version_csv}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Backer'),
          StringStruct('FileDescription', 'Backer Agent'),
          StringStruct('FileVersion', '{__version__}'),
          StringStruct('InternalName', 'backer-agent'),
          StringStruct('OriginalFilename', 'backer-agent.exe'),
          StringStruct('ProductName', 'Backer Agent'),
          StringStruct('ProductVersion', '{__version__}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    VERSION_FILE.write_text(version_text, encoding="utf-8")
    return VERSION_FILE


def download_windows_tool(tool: str) -> Path:
    """Download a checksum-verified Windows tool using the shared manager."""
    sys.path.insert(0, str(Path("src").resolve()))
    from backer.tools.manager import ToolManager

    manager = ToolManager(TOOLS_DIR)
    manager._system = "Windows"
    manager._machine = "AMD64"
    return manager.download(tool)


def build_pyinstaller() -> Path:
    """Build the PyInstaller executable."""
    print("Building executable with PyInstaller...")

    write_pyinstaller_version_file()

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "backer-agent.spec"],
        check=True,
    )

    return DIST_DIR / "backer-agent.exe"


def build_service_executable() -> Path:
    """Build the dedicated entry point used by the boot task."""
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--onefile", "--console",
         "--name", "backer-agent-service", "src/backer/agent/service_entry.py"],
        check=True,
    )
    return DIST_DIR / "backer-agent-service.exe"


def create_package() -> Path:
    """Create the final distribution package."""
    print("Creating distribution package...")

    package_dir = DIST_DIR / "backer-agent-windows"
    package_dir.mkdir(parents=True, exist_ok=True)
    DIST_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    # Copy executable
    shutil.copy(DIST_DIR / "backer-agent.exe", package_dir / "backer-agent.exe")
    shutil.copy(DIST_DIR / "backer-agent-service.exe", package_dir / "backer-agent-service.exe")

    # Copy tools if on Windows and they exist
    tools_subdir = package_dir / "tools"
    tools_subdir.mkdir(exist_ok=True)

    for tool in ["rclone.exe", "restic.exe", "kopia.exe"]:
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

For more info, visit: https://git.stockhome.com.au/stocky789/backer
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
        for tool in ["rclone", "restic", "kopia"]:
            download_windows_tool(tool)
    else:
        print("Note: Not on Windows, skipping tool downloads")
        print("      (tools will be downloaded on first run)")

    # Build
    build_pyinstaller()
    build_service_executable()

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

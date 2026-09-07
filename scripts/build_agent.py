#!/usr/bin/env python3
"""Build script for the Backer Windows agent artifacts.

This script:
1. Downloads Kopia for Windows
2. Builds the console CLI (backer.exe) and the unattended service executable
3. Publishes the Avalonia desktop client (backer-desktop.exe)
4. Creates a zip package with everything needed

Usage:
    python scripts/build_agent.py

Requirements:
    pip install pyinstaller
    .NET 8 SDK (for the desktop client)
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
DESKTOP_PROJECT = Path("desktop") / "Backer.Desktop"
DESKTOP_PUBLISH_DIR = DIST_DIR / "desktop"
DESKTOP_EXE = "backer-desktop.exe"


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
          StringStruct('InternalName', 'backer'),
          StringStruct('OriginalFilename', 'backer.exe'),
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
    """Build the console CLI executable every mutation goes through."""
    print("Building backer.exe with PyInstaller...")

    write_pyinstaller_version_file()

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "backer-agent.spec"],
        check=True,
    )

    return DIST_DIR / "backer.exe"


def staged_desktop_files() -> list[Path]:
    """Publish output the installer ships: everything except debug symbols."""
    return sorted(
        path for path in DESKTOP_PUBLISH_DIR.iterdir() if path.is_file() and path.suffix != ".pdb"
    )


def build_desktop() -> Path:
    """Publish the Avalonia desktop client that drives the CLI."""
    print("Publishing the desktop client with dotnet...")

    subprocess.run(
        ["dotnet", "publish", str(DESKTOP_PROJECT), "-c", "Release", "-r", "win-x64",
         "--self-contained", "-p:PublishSingleFile=true",
         "-p:IncludeNativeLibrariesForSelfExtract=true",
         "-p:EnableCompressionInSingleFile=true", "-o", str(DESKTOP_PUBLISH_DIR)],
        check=True,
    )

    # Native libraries are bundled into the exe, but stage anything else the
    # publish leaves behind so the installer never ships a partial client.
    for path in staged_desktop_files():
        shutil.copy(path, DIST_DIR / path.name)
    return DIST_DIR / DESKTOP_EXE


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

    # Copy executables
    shutil.copy(DIST_DIR / "backer.exe", package_dir / "backer.exe")
    shutil.copy(DIST_DIR / "backer-agent-service.exe", package_dir / "backer-agent-service.exe")
    for path in staged_desktop_files():
        shutil.copy(DIST_DIR / path.name, package_dir / path.name)

    # Copy tools if on Windows and they exist
    tools_subdir = package_dir / "tools"
    tools_subdir.mkdir(exist_ok=True)

    for tool in ["kopia.exe"]:
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
echo   backer.exe agent register --server http://your-server:8420
echo   backer.exe agent install
echo   backer.exe agent start
echo.
echo Or launch the desktop client: backer-desktop.exe
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
   backer.exe agent register --server http://your-server:8420

3. Install as Windows service:
   backer.exe agent install

4. Or run manually:
   backer.exe agent start

Prefer a window? Run backer-desktop.exe - the desktop client reads the same
configuration and performs every action by calling backer.exe.

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
        for tool in ["kopia"]:
            download_windows_tool(tool)
    else:
        print("Note: Not on Windows, skipping tool downloads")
        print("      (tools will be downloaded on first run)")

    # Build
    build_pyinstaller()
    build_service_executable()
    build_desktop()

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
        print(f"Build complete: {DIST_DIR / 'backer.exe'}")
        print("Note: Run on Windows for full package with tools")
        print("=" * 50)


if __name__ == "__main__":
    main()

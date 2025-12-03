# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Backer Windows Agent.

Build with: pyinstaller backer-agent.spec

This creates a single-file executable that includes:
- The backer agent
- All Python dependencies
- rclone and restic binaries (downloaded during build)
"""

import os
import sys
from pathlib import Path

# Add src to path for analysis
sys.path.insert(0, str(Path('src').absolute()))

block_cipher = None

# Collect all backer modules
a = Analysis(
    ['src/backer/client/agent.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        # Include any data files needed
    ],
    hiddenimports=[
        'backer',
        'backer.backends',
        'backer.backends.rsync',
        'backer.backends.rclone',
        'backer.backends.restic',
        'backer.backends.base',
        'backer.backends.registry',
        'backer.client',
        'backer.client.agent',
        'backer.client.windows_service',
        'backer.tools',
        'backer.tools.manager',
        'httpx',
        'httpx._transports',
        'httpx._transports.default',
        'httpcore',
        'anyio',
        'anyio._backends',
        'anyio._backends._asyncio',
        'sniffio',
        'certifi',
        'h11',
        'yaml',
        'pydantic',
        'pydantic.deprecated',
        'pydantic.deprecated.decorator',
        'pydantic_core',
        'click',
        'rich',
        'rich.console',
        'rich.table',
        'croniter',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude server-only modules to reduce size
        'fastapi',
        'uvicorn',
        'starlette',
        'jinja2',
        'multipart',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='backer-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Console app for now, can change to False for GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add icon path here if you have one: icon='assets/backer.ico'
    version=None,  # Add version info file if needed
)

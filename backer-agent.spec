# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for Backer Windows Agent GUI.

Build with: pyinstaller backer-agent.spec

This creates a single-file executable that includes:
- The backer agent GUI
- All Python dependencies
"""

import os
import sys
from pathlib import Path

# Add src to path for analysis
sys.path.insert(0, str(Path('src').absolute()))

block_cipher = None
version_file = Path('build/backer-agent-version.txt')

# Collect all backer modules
a = Analysis(
    ['src/backer/agent/gui/app.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        # Include icon file at root level (where GUI looks for it)
        ('assets/backer.ico', '.'),
        # Include assets folder for other resources
        ('assets', 'assets') if os.path.exists('assets') else ('README.md', '.'),
        ('src/backer/assets/eff_large_wordlist.txt', 'backer/assets'),
    ],
    hiddenimports=[
        'backer',
        'backer.agent',
        'backer.agent.gui',
        'backer.agent.gui.app',
        'backer.agent.service',
        'backer.backends',
        'backer.backends.kopia',
        'backer.backends.proxy',
        'backer.backends.base',
        'backer.backends.registry',
        'backer.core',
        'backer.core.repo_metadata',
        # requests and its dependencies
        'requests',
        'requests.adapters',
        'requests.auth',
        'requests.cookies',
        'requests.models',
        'requests.sessions',
        'requests.structures',
        'urllib3',
        'urllib3.util',
        'urllib3.util.retry',
        'charset_normalizer',
        'idna',
        # tenacity and its dependencies
        'tenacity',
        'tenacity.stop',
        'tenacity.wait',
        'tenacity.retry',
        'backer.client',
        'backer.client.agent',
        'backer.tools',
        'backer.tools.manager',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
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
    console=False,  # GUI app - no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/backer.ico' if os.path.exists('assets/backer.ico') else None,
    version=str(version_file) if version_file.exists() else None,
)

# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for VM Alignment Client EXE.
Produces a single-file Windows executable that runs the setup wizard.
"""

import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect pika data files
datas_pika = collect_data_files('pika')
datas_requests = collect_data_files('requests')
datas_urllib3 = collect_data_files('urllib3')
datas_certifi = collect_data_files('certifi')
datas_charset_normalizer = collect_data_files('charset_normalizer')
datas_idna = collect_data_files('idna')
datas_win32 = []
try:
    datas_win32 = collect_data_files('win32')
except Exception:
    pass

# Collect submodules for pika (pika uses lazy imports)
hiddenimports_pika = collect_submodules('pika')

hiddenimports = [
    'json', 'socket', 'uuid', 'subprocess', 'pathlib',
    'urllib.request', 'urllib.error', 'urllib.parse',
    'pika', 'pika.exceptions',
    'requests', 'requests.api', 'requests.models',
    'win32serviceutil', 'win32service', 'win32event', 'servicemanager',
]
hiddenimports += hiddenimports_pika

datas = []
datas += datas_pika
datas += datas_requests
datas += datas_urllib3
datas += datas_certifi
datas += datas_charset_normalizer
datas += datas_idna
datas += datas_win32

# Add the project root so imports work
# __file__ may not be defined in all PyInstaller contexts — use SPECPATH fallback
_spec_dir = Path(os.environ.get('SPECPATH', '.')).resolve()
project_root = str(_spec_dir.parent)
client_dir = str(_spec_dir)

a = Analysis(
    [f'{client_dir}/setup_wizard.py'],
    pathex=[project_root, client_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'pandas',
        'pytest', 'IPython', 'notebook', 'jupyter',
        'PIL', 'Pillow',
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
    [],
    exclude_binaries=True,
    name='VMAlignmentClientSetup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # Show console for wizard interaction
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,             # Can set icon=.ico here
    version=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VMAlignmentClientSetup',
)

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DDM standalone binary."""

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent

a = Analysis(
    [str(_here / "ddm_entry.py")],
    pathex=[str(_here)],
    binaries=[],
    datas=[
        (str(_here / "config" / "config.yaml"), "config"),
    ],
    hiddenimports=[
        "ddm",
        "ddm.cli",
        "ddm.config",
        "ddm.services",
        "ddm.storage",
        "ddm.gates",
        "ddm.gates.runner",
        "ddm.gates.verilog_check",
        "ddm.gates.drc_check",
        "ddm.gates.lvs_check",
        "ddm.gates.final_check",
        "ddm.gates.pi_check",
        "ddm.gates.pi_final_check",
        "click",
        "rich",
        "rich.progress",
        "rich.console",
        "rich.table",
        "pydantic",
        "yaml",
        "loguru",
        "psutil",
        "sqlite3",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "cv2",
        "django",
        "flask",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ddm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

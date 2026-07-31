# -*- mode: python ; coding: utf-8 -*-
"""
NetWatch Pro v2.4 — PyInstaller spec file.

Compilar:
    pyinstaller netwatch.spec

Requisitos:
    pip install pyinstaller customtkinter paramiko pygame edge-tts

Nota: Coloque o ícone em resources/icons/netwatch.ico antes de compilar.
      Se o ícone não existir, o .exe será gerado sem ícone.
"""
import os
from pathlib import Path

block_cipher = None

# Caminho base do projeto
BASE_DIR = os.path.dirname(os.path.abspath(SPEC))

# Ícone — usa se existir, senão None
ICON_PATH = os.path.join(BASE_DIR, "resources", "icons", "netwatch.ico")
icon_file = ICON_PATH if os.path.exists(ICON_PATH) else None

# Dados adicionais para incluir no .exe
added_data = []

# Inclui resources/ se existir
resources_dir = os.path.join(BASE_DIR, "resources")
if os.path.isdir(resources_dir):
    added_data.append((resources_dir, "resources"))

# Inclui customtkinter themes/assets
import customtkinter
ctk_path = os.path.dirname(customtkinter.__file__)
added_data.append((ctk_path, "customtkinter"))

a = Analysis(
    ["main.py"],
    pathex=[BASE_DIR],
    binaries=[],
    datas=added_data,
    hiddenimports=[
        "paramiko",
        "pygame",
        "edge_tts",
        "customtkinter",
        "PIL",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "notebook",
        "jupyter",
        "test",
        "unittest",
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
    name="NetWatch Pro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed — sem console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
    version=None,
    uac_admin=False,        # não requer admin
)

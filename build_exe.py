"""
Build boleteria.exe with PyInstaller.

Usage:
    python build_exe.py            # with console (shows logs)
    python build_exe.py --noconsole  # no terminal window
"""

import argparse
import os
import shutil
import subprocess
import sys

SPEC_NAME = "_boleteria_build.spec"
DIST_DIR = "dist"
BUILD_DIR = "_pyinstaller_build"

SPEC_TEMPLATE = r'''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.building.api import EXE, PYZ
from PyInstaller.building.build_main import Analysis

a = Analysis(
    ['run_server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
    ],
    hiddenimports=[
        'pymongo', 'flask', 'jinja2', 'dotenv',
        'datetime', 're', 'math', 'time', 'functools', 'logging',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'test', 'pip',
        'setuptools', 'numpy', 'matplotlib',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='boleteria', debug=False,
    bootloader_ignore_signals=False, strip=False,
    upx=True, upx_exclude=[], runtime_tmpdir=None,
    console={console},
    disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
)
'''


def main():
    parser = argparse.ArgumentParser(description="Build boleteria.exe")
    parser.add_argument(
        "--console", action="store_true",
        help="Show terminal window (default: hidden GUI mode)",
    )
    args = parser.parse_args()

    console = "True" if args.console else "False"
    mode = "con consola" if args.console else "sin ventana (GUI)"

    print(f"  ✓ Modo: {mode}")
    print("  Construyendo ejecutable...")

    spec_content = SPEC_TEMPLATE.replace("{console}", console)
    with open(SPEC_NAME, "w", encoding="utf-8") as f:
        f.write(spec_content)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--clean",
             "--distpath", DIST_DIR, "--workpath", BUILD_DIR, SPEC_NAME],
            capture_output=True, text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr)
            print("  ✗ Error al construir")
            return 1
    finally:
        if os.path.exists(BUILD_DIR):
            shutil.rmtree(BUILD_DIR, ignore_errors=True)
        if os.path.exists(SPEC_NAME):
            os.remove(SPEC_NAME)

    exe_path = os.path.join(DIST_DIR, "boleteria.exe")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"  ✓ Listo! {exe_path} ({size_mb:.1f} MB)")
        print()
        print("  NOTA: Coloca tu .env junto al .exe si no usas")
        print("        variables de entorno del sistema.")
    else:
        print("  ✗ No se encontró el ejecutable")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

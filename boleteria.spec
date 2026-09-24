# -*- mode: python ; coding: utf-8 -*-
import os

common_kwargs = dict(
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=['pymongo', 'flask', 'jinja2', 'dotenv', 'datetime', 'logging', 'werkzeug', 'werkzeug.security'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'numpy', 'matplotlib'],
    noarchive=False,
    optimize=0,
)

a_servidor = Analysis(['run_server.py'], **common_kwargs)
a_backup = Analysis(['scripts/backup.py'], **common_kwargs)
a_integridad = Analysis(['scripts/integridad.py'], **common_kwargs)

pyz_servidor = PYZ(a_servidor.pure)
pyz_backup = PYZ(a_backup.pure)
pyz_integridad = PYZ(a_integridad.pure)

common_exe = dict(
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

exe_servidor = EXE(
    pyz_servidor,
    a_servidor.scripts,
    a_servidor.binaries,
    a_servidor.datas,
    [],
    name='BoleteriaServidor',
    **common_exe,
)

exe_backup = EXE(
    pyz_backup,
    a_backup.scripts,
    a_backup.binaries,
    a_backup.datas,
    [],
    name='BoleteriaBackup',
    **common_exe,
)

exe_integridad = EXE(
    pyz_integridad,
    a_integridad.scripts,
    a_integridad.binaries,
    a_integridad.datas,
    [],
    name='BoleteriaIntegridad',
    **common_exe,
)

# -*- mode: python ; coding: utf-8 -*-
"""Spec de PyInstaller para generar los ejecutables de la PC servidor.

Build:
    python -m PyInstaller --noconfirm --clean boleteria.spec

Genera en dist/:
    BoleteriaServidor.exe   -> servidor waitress (run_server.py), embebe templates/ y static/
    BoleteriaBackup.exe     -> scripts/backup.py
    BoleteriaIntegridad.exe -> scripts/integridad.py

El archivo .env NO se empaqueta: se lee desde el directorio del .exe (ver
app.py y database.py en modo frozen), por lo que las credenciales no viajan
en el binario y se pueden actualizar sin recompilar.
"""

from PyInstaller.utils.hooks import collect_submodules

_SITE_DATAS = [
    ("templates", "templates"),
    ("static", "static"),
]
_HIDDEN_IMPORTS = collect_submodules("motores")


def _analisis(script, datas):
    return Analysis(
        [script],
        pathex=[],
        binaries=[],
        datas=datas,
        hiddenimports=_HIDDEN_IMPORTS,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )


def _exe(nombre, analisis):
    pyz = PYZ(analisis.pure)
    return EXE(
        pyz,
        analisis.scripts,
        analisis.binaries,
        analisis.datas,
        [],
        name=nombre,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )


a_servidor = _analisis("run_server.py", _SITE_DATAS)
a_backup = _analisis("scripts/backup.py", [])
a_integridad = _analisis("scripts/integridad.py", [])

exe_servidor = _exe("BoleteriaServidor", a_servidor)
exe_backup = _exe("BoleteriaBackup", a_backup)
exe_integridad = _exe("BoleteriaIntegridad", a_integridad)

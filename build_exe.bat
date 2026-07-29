@echo off
REM ====================================
REM  Build boleteria.exe with PyInstaller
REM ====================================
echo.
echo  1) Console window? [C]onsola / [S]in consola (GUI)
echo.

set /p MODO="Select C or S: "
if /i "%MODO%"=="S" (
    set CONSOLE=--noconsole
    echo  Selected: No console (GUI)
) else (
    set CONSOLE=--console
    echo  Selected: With console
)

echo.
echo Building...
echo.

pyinstaller --clean --onefile %CONSOLE% ^
    --add-data "templates;templates" ^
    --hidden-import pymongo ^
    --hidden-import flask ^
    --hidden-import jinja2 ^
    --hidden-import dotenv ^
    --hidden-import datetime ^
    --hidden-import logging ^
    --exclude-module tkinter ^
    --exclude-module unittest ^
    --exclude-module numpy ^
    --exclude-module matplotlib ^
    --name boleteria run_server.py

if errorlevel 1 (
    echo  ERROR: Build failed. Install PyInstaller:
    echo    pip install pyinstaller
    pause
    exit /b 1
)

echo.
echo  Done! Executable: dist\boleteria.exe
echo.
echo  NOTE: Place your .env file next to the .exe
echo        if not using system environment variables.
pause

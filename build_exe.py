"""
Build a standalone .exe of the boletería app using PyInstaller.

Usage:
    python build_exe.py              -- one-folder build (fast startup)
    python build_exe.py --onefile    -- single .exe (easier to distribute)

The .env file must be placed next to the .exe at runtime.
"""
import os
import sys
import subprocess
import shutil
import argparse

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BUILD_DIR, "dist")

DATA_DIRS = [
    ("templates", "templates"),
    ("static", "static"),
]


def build(onefile=False):
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "--name", "boleteria",
        "--onedir" if not onefile else "--onefile",
    ]

    for src, dst in DATA_DIRS:
        cmd.extend(["--add-data", f"{src}{os.pathsep}{dst}"])

    for mod in [
        "openpyxl", "pymongo", "dns", "bson",
    ]:
        cmd.extend(["--collect-all", mod])

    cmd.append("run_server.py")

    subprocess.run(cmd, cwd=BUILD_DIR, check=True)

    if onefile:
        exe_path = os.path.join(DIST_DIR, "boleteria.exe")
    else:
        exe_path = os.path.join(DIST_DIR, "boleteria", "boleteria.exe")

    print(f"\nBuild complete: {exe_path}")
    print("Copy .env next to the .exe before running.")
    print("Each PC needs its own .env with the same MONGO_URI.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build boleteria executable")
    parser.add_argument("--onefile", action="store_true", help="Build single-file .exe")
    args = parser.parse_args()
    build(onefile=args.onefile)

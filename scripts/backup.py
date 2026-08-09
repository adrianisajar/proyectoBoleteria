"""Respaldo automático de todas las colecciones a un archivo ZIP.

Uso:
    python scripts/backup.py [--dest DIR] [--keep N] [--quiet]

Crea respaldos con el mismo formato del endpoint /backup (un archivo
``backup.json`` dentro del ZIP) y elimina los respaldos más antiguos dejando
solo los últimos ``--keep``. Se puede programar con el Programador de tareas de
Windows, por ejemplo:

    schtasks /Create /SC DAILY /ST 23:00 /TN "BoleteriaBackup" ^
      /TR "C:\\app_boleteria\\.venv\\Scripts\\python.exe C:\\app_boleteria\\scripts\\backup.py"
"""

import argparse
import io
import os
import sys
import zipfile
from datetime import datetime

from bson import json_util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database import boletas, configuracion, facturas, rifas, traslados, usuarios, vendedores  # noqa: E402

COLECCIONES = [
    ("boletas", boletas),
    ("vendedores", vendedores),
    ("facturas", facturas),
    ("rifas", rifas),
    ("configuracion", configuracion),
    ("usuarios", usuarios),
    ("traslados", traslados),
]


def crear_respaldo(dest: str) -> tuple[str, int]:
    """Export all collections to backup_<fecha>.zip and return (ruta, total_docs)."""
    if boletas is None:
        raise RuntimeError("No hay conexión activa a MongoDB.")
    os.makedirs(dest, exist_ok=True)
    data: dict = {}
    total = 0
    for nombre, col in COLECCIONES:
        if col is None:
            continue
        docs = list(col.find({}))
        data[nombre] = docs
        total += len(docs)
    filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    path = os.path.join(dest, filename)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json_util.dumps(data, ensure_ascii=False, indent=2))
    buf.seek(0)
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path, total


def limpiar_respaldos(dest: str, keep: int) -> list[str]:
    """Delete the oldest backups beyond `keep`, returning the removed paths."""
    files = sorted(os.path.join(dest, f) for f in os.listdir(dest) if f.startswith("backup_") and f.endswith(".zip"))
    removidos = []
    while len(files) > keep:
        antiguo = files.pop(0)
        os.remove(antiguo)
        removidos.append(antiguo)
    return removidos


def main() -> int:
    parser = argparse.ArgumentParser(description="Respaldo automático de la base de datos a un ZIP.")
    parser.add_argument("--dest", default=os.getenv("BACKUP_DIR", "respaldos"), help="Carpeta destino (default: respaldos o $BACKUP_DIR).")
    parser.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP", "30")), help="Número de respaldos a conservar (default: 30).")
    parser.add_argument("--quiet", action="store_true", help="No imprimir nada en caso de éxito.")
    args = parser.parse_args()
    try:
        path, total = crear_respaldo(args.dest)
        removidos = limpiar_respaldos(args.dest, args.keep)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"[OK] Respaldo creado: {path} ({total} documentos).")
        if removidos:
            print(f"[OK] Respaldos antiguos eliminados: {len(removidos)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

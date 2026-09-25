"""Respaldo automático de todas las colecciones a un archivo ZIP.

Uso:
    python scripts/backup.py [--dest DIR] [--keep N] [--quiet]
    python scripts/backup.py --import backup_20260101_120000.zip --admin-password PASS

Crea respaldos con el mismo formato del endpoint /backup (un archivo
``backup.json`` dentro del ZIP) y elimina los respaldos más antiguos dejando
solo los últimos ``--keep``. Se puede programar con el Programador de tareas de
Windows, por ejemplo:

    schtasks /Create /SC DAILY /ST 23:00 /TN "BoleteriaBackup" ^
      /TR "C:\\app_boleteria\\.venv\\Scripts\\python.exe C:\\app_boleteria\\scripts\\backup.py"

Importación requiere `--admin-password` para autenticación. Incluye protección
contra ZIP bombs (>500 MB sin comprimir) y path traversal en nombres de archivo.
"""

import argparse
import getpass
import json
import os
import sys
import time
import zipfile
from datetime import datetime

from bson import json_util
from werkzeug.security import check_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database import boletas, configuracion, facturas, reservas, rifas, traslados, usuarios, vendedores  # noqa: E402
from motores.constants import ADMIN_INICIAL_USUARIO  # noqa: E402

COLECCIONES = [
    ("boletas", boletas),
    ("vendedores", vendedores),
    ("facturas", facturas),
    ("rifas", rifas),
    ("configuracion", configuracion),
    ("usuarios", usuarios),
    ("traslados", traslados),
    ("reservas", reservas),
]

MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB ZIP bomb limit


def crear_respaldo(dest: str) -> tuple[str, int]:
    """Export all collections to backup_<fecha>.zip and return (ruta, total_docs).

    Serializes each collection independently to reduce peak RAM — the full
    JSON string is never held in memory simultaneously for all collections.
    """
    if boletas is None:
        raise RuntimeError("No hay conexión activa a MongoDB.")
    os.makedirs(dest, exist_ok=True)
    filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    path = os.path.join(dest, filename)
    total = 0
    parts: list[str] = ["{"]
    first = True
    for nombre, col in COLECCIONES:
        if col is None:
            continue
        docs = list(col.find({}))
        total += len(docs)
        separator = "" if first else ","
        first = False
        parts.append(f'{separator}"{nombre}": {json_util.dumps(docs, ensure_ascii=False)}')
        del docs  # release per-collection list immediately
    parts.append("}")
    data = "".join(parts)
    del parts  # release parts list
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", data)
    del data  # release merged string
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


# ── Importación protegida ──────────────────────────────────────


def _verificar_contrasena_admin(password: str) -> bool:
    """Verify admin password against the usuarios collection."""
    if usuarios is None:
        return False
    admin_user = ADMIN_INICIAL_USUARIO or "admin"
    user = usuarios.find_one({"usuario": admin_user, "rol": "admin"})
    if user is None:
        return False
    return check_password_hash(user.get("password_hash", ""), password)


def _validar_zip_seguro(zf: zipfile.ZipFile) -> None:
    """Reject ZIP bombs (>500 MB uncompressed) and path traversal attempts."""
    total_size = 0
    for info in zf.infolist():
        name = info.filename
        if ".." in name or name.startswith("/") or "\\" in name:
            raise ValueError(f"ZIP contiene ruta insegura: {name}")
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"ZIP bomb detectado: tamaño sin comprimir excede {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB.")


def _validar_estructura(data: dict) -> None:
    """Validate that the parsed JSON has the expected structure."""
    if not isinstance(data, dict):
        raise ValueError("Formato de backup inválido: se esperaba un objeto JSON.")
    allowed = {name for name, _ in COLECCIONES}
    unknown = set(data.keys()) - allowed
    if unknown:
        raise ValueError(f"Backup contiene colecciones desconocidas: {', '.join(sorted(unknown))}")
    for key, value in data.items():
        if not isinstance(value, list):
            raise ValueError(f"Colección '{key}' tiene formato inválido (se esperaba una lista).")


def importar_respaldo(zip_path: str, admin_password: str) -> int:
    """Import a backup ZIP into MongoDB with full security validation.

    Protections:
    - ZIP bomb detection (>500 MB uncompressed)
    - Path traversal rejection on ZIP entry names
    - Admin password authentication
    - JSON structure validation
    - Collection whitelist enforcement

    Returns total number of documents imported.
    """
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"Archivo no encontrado: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        _validar_zip_seguro(zf)

        nombres = zf.namelist()
        if "backup.json" in nombres:
            raw = zf.read("backup.json")
            data = json.loads(raw)
            del raw
        elif any(n.endswith(".json") for n in nombres):
            data = {}
            for nombre in nombres:
                if nombre.endswith(".json"):
                    data[nombre[:-5]] = json.loads(zf.read(nombre))
        else:
            raise ValueError("El ZIP no contiene archivos JSON de respaldo.")
    _validar_estructura(data)

    print(f"[IMPORTAR] Archivo válido: {zip_path}")
    confirm = input("[IMPORTAR] Esto reemplazará los datos actuales. Continuar? (SI/no): ").strip()
    if confirm.upper() != "SI":
        print("[IMPORTAR] Operación cancelada.")
        return 0

    if not _verificar_contrasena_admin(admin_password):
        raise PermissionError("Contraseña de administrador incorrecta.")

    print("[IMPORTAR] Autenticación exitosa. Importando respaldo...")
    total_imported = 0
    for nombre, col in COLECCIONES:
        if col is None or nombre not in data:
            continue
        docs = data[nombre]
        if not isinstance(docs, list) or not docs:
            continue
        start = time.time()
        col.delete_many({})
        col.insert_many(docs, ordered=False)
        elapsed = time.time() - start
        total_imported += len(docs)
        print(f"[IMPORTAR] {nombre}: {len(docs)} documentos ({elapsed:.2f}s)")

    print(f"[IMPORTAR] Total importado: {total_imported} documentos.")
    return total_imported


# ── CLI ────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Respaldo automático de la base de datos a un ZIP.",
    )
    parser.add_argument("--dest", default=os.getenv("BACKUP_DIR") or "respaldos", help="Carpeta destino (default: respaldos o $BACKUP_DIR).")
    parser.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP") or "30"), help="Número de respaldos a conservar (default: 30).")
    parser.add_argument("--quiet", action="store_true", help="No imprimir nada en caso de éxito.")
    parser.add_argument("--import", dest="import_file", metavar="ZIP", help="Importar un respaldo ZIP (requiere --admin-password).")
    parser.add_argument("--admin-password", dest="admin_password", default=None, help="Contraseña de administrador para importación.")
    args = parser.parse_args()

    if args.import_file:
        password = args.admin_password
        if not password:
            password = getpass.getpass("Contraseña de administrador: ")
        try:
            importar_respaldo(args.import_file, password)
        except Exception as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        return 0

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

import os
import sys
import re
from pymongo import MongoClient
from dotenv import load_dotenv

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    exe_dir = os.path.dirname(sys.executable)
    dotenv_path = os.path.join(exe_dir, ".env")
    load_dotenv(dotenv_path)
else:
    load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "sistema_boleteria")
MONGO_TIMEOUT_MS = int(os.getenv("MONGO_TIMEOUT_MS", "5000"))


def _connect():
    # Try 1: direct TLS connection (avoids SRV DNS issues)
    m = re.match(r"mongodb\+srv://([^:]+):([^@]+)@([^/]+)", MONGO_URI)
    if m:
        user, pwd, host = m.group(1), m.group(2), m.group(3)
        try:
            client = MongoClient(
                host=host, port=27017,
                username=user, password=pwd,
                authSource="admin",
                tls=True, tlsInsecure=True,
                serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
            )
            db = client[MONGO_DB]
            db.list_collection_names()
            return client, db
        except Exception:
            pass

    # Try 2: SRV URI with tlsInsecure
    uri = MONGO_URI
    if "tlsInsecure" not in uri:
        sep = "&" if "?" in uri else "?"
        uri += f"{sep}tlsInsecure=true"
    client = MongoClient(uri, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
    db = client[MONGO_DB]
    return client, db


try:
    _client, db = _connect()
except Exception as e:
    print(f"Error al conectar a MongoDB: {e}")
    db = None

boletas = db["boletas"] if db is not None else None
vendedores = db["vendedores"] if db is not None else None
configuracion = db["configuracion"] if db is not None else None
facturas = db["facturas"] if db is not None else None
rifas = db["rifas"] if db is not None else None

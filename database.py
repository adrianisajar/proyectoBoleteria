import logging
import os
import re
import sys

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    dotenv_path = os.path.join(os.path.dirname(sys.executable), ".env")
    if not load_dotenv(dotenv_path):
        load_dotenv()
else:
    load_dotenv()

MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB: str = os.getenv("MONGO_DB", "sistema_boleteria")
MONGO_TIMEOUT_MS: int = int(os.getenv("SERVER_SELECTION_TIMEOUT_MS", os.getenv("MONGO_TIMEOUT_MS", "5000")))
MONGO_MIN_POOL_SIZE: int = int(os.getenv("MIN_POOL_SIZE", "0"))
MONGO_MAX_POOL_SIZE: int = int(os.getenv("MAX_POOL_SIZE", "100"))
MONGO_TLS_INSECURE: bool = os.getenv("MONGO_TLS_INSECURE", "false").lower() in ("1", "true", "yes")


def _connect() -> tuple[MongoClient, Database]:
    m = re.match(r"mongodb\+srv://([^:]+):([^@]+)@([^/]+)", MONGO_URI)
    if m:
        user, pwd, host = m.group(1), m.group(2), m.group(3)
        try:
            client: MongoClient = MongoClient(
                host=host,
                port=27017,
                username=user,
                password=pwd,
                authSource="admin",
                tls=True,
                tlsInsecure=MONGO_TLS_INSECURE,
                serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
                minPoolSize=MONGO_MIN_POOL_SIZE,
                maxPoolSize=MONGO_MAX_POOL_SIZE,
            )
            db: Database = client[MONGO_DB]
            db.list_collection_names()
            return client, db
        except Exception:
            pass

    uri: str = MONGO_URI
    if MONGO_TLS_INSECURE and "tlsInsecure" not in uri:
        sep = "&" if "?" in uri else "?"
        uri += f"{sep}tlsInsecure=true"
    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
        minPoolSize=MONGO_MIN_POOL_SIZE,
        maxPoolSize=MONGO_MAX_POOL_SIZE,
    )
    db = client[MONGO_DB]
    return client, db


try:
    _client, db = _connect()
except Exception as e:
    logger.error("Error al conectar a MongoDB: %s", e)
    db = None

boletas: Collection | None = db["boletas"] if db is not None else None
vendedores: Collection | None = db["vendedores"] if db is not None else None
configuracion: Collection | None = db["configuracion"] if db is not None else None
facturas: Collection | None = db["facturas"] if db is not None else None
rifas: Collection | None = db["rifas"] if db is not None else None
liquidaciones: Collection | None = db["liquidaciones"] if db is not None else None

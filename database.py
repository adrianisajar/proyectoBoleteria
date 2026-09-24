import logging
import os
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

MONGO_URI: str = os.environ.get("MONGO_URI") or ""
MONGO_DB: str = os.environ.get("MONGO_DB") or "sistema_boleteria"
MONGO_TIMEOUT_MS: int = int(os.environ.get("SERVER_SELECTION_TIMEOUT_MS") or os.environ.get("MONGO_TIMEOUT_MS") or "5000")
MONGO_MIN_POOL_SIZE: int = int(os.environ.get("MIN_POOL_SIZE") or "0")
MONGO_MAX_POOL_SIZE: int = int(os.environ.get("MAX_POOL_SIZE") or "20")
MONGO_TLS_INSECURE: bool = (os.environ.get("MONGO_TLS_INSECURE") or "false").lower() in ("1", "true", "yes")

# Pool tuning constants (not env-configurable — derived from Waitress 8 threads).
# maxIdleTimeMS: close idle connections after 4 min so Atlas doesn't kill them silently.
# connectTimeoutMS: fail fast if the server is unreachable (3s vs PyMongo default 20s).
# socketTimeoutMS: kill operations stuck for >30s to avoid hung requests.
_MAX_IDLE_MS = 4 * 60 * 1000
_CONNECT_TIMEOUT_MS = 3_000
_SOCKET_TIMEOUT_MS = 30_000
_APP_NAME = "boleteria-flask"


def _connect() -> tuple[MongoClient, Database]:
    uri = MONGO_URI
    if MONGO_TLS_INSECURE and "tlsInsecure" not in uri:
        sep = "&" if "?" in uri else "?"
        uri += f"{sep}tlsInsecure=true"
    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
        minPoolSize=MONGO_MIN_POOL_SIZE,
        maxPoolSize=MONGO_MAX_POOL_SIZE,
        maxIdleTimeMS=_MAX_IDLE_MS,
        connectTimeoutMS=_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=_SOCKET_TIMEOUT_MS,
        appname=_APP_NAME,
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
usuarios: Collection | None = db["usuarios"] if db is not None else None
traslados: Collection | None = db["traslados"] if db is not None else None
reservas: Collection | None = db["reservas"] if db is not None else None
login_intentos: Collection | None = db["login_intentos"] if db is not None else None

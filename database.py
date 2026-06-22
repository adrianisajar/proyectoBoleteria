import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB = os.getenv("MONGO_DB", "sistema_boleteria")
MONGO_TIMEOUT_MS = int(os.getenv("MONGO_TIMEOUT_MS", "5000"))

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=MONGO_TIMEOUT_MS)
    db = client[MONGO_DB]
except Exception as e:
    print(f"Error al conectar a MongoDB: {e}")
    db = None

boletas = db["boletas"] if db is not None else None
vendedores = db["vendedores"] if db is not None else None
configuracion = db["configuracion"] if db is not None else None
auditoria = db["auditoria"] if db is not None else None

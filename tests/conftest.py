import contextlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TEST_DB_NAME = f"boleteria_test_{os.getpid()}_{int(time.time())}"
os.environ["MONGO_DB"] = TEST_DB_NAME
os.environ.setdefault("MONGO_TIMEOUT_MS", "8000")

import pytest
from werkzeug.security import generate_password_hash

import database
from app import app as flask_app
from database import boletas, configuracion, facturas, rifas, usuarios, vendedores
from motores.cache import invalidate_config_cache, invalidate_dashboard_cache
from motores.fechas import now_local
from optimizar_db import REQUIRED_INDEXES

if any(col is None for col in (boletas, vendedores, configuracion, facturas, rifas, usuarios)):
    pytest.skip("MongoDB no disponible: se omiten los tests que requieren base de datos.", allow_module_level=True)

COMISION_TIERS = [
    {"min": 0, "valor": 0},
    {"min": 10, "valor": 10000},
    {"min": 21, "valor": 15000},
    {"min": 51, "valor": 20000},
]

N_BOLETAS = 500
_RETRIES = 3

ADMIN_USUARIO = "admin"
ADMIN_PASSWORD = "admin-test"
CAJA_USUARIO = "caja"
CAJA_PASSWORD = "caja-test"


def _seed_usuarios():
    usuarios.drop()
    usuarios.create_index("usuario", unique=True)
    usuarios.insert_many(
        [
            {
                "nombre": "Admin Test",
                "usuario": ADMIN_USUARIO,
                "password_hash": generate_password_hash(ADMIN_PASSWORD),
                "rol": "admin",
                "activo": True,
                "fecha_creacion": now_local(),
                "ultimo_acceso": None,
            },
            {
                "nombre": "Caja Test",
                "usuario": CAJA_USUARIO,
                "password_hash": generate_password_hash(CAJA_PASSWORD),
                "rol": "cajero",
                "activo": True,
                "fecha_creacion": now_local(),
                "ultimo_acceso": None,
            },
        ]
    )


def login(client, usuario=ADMIN_USUARIO, password=ADMIN_PASSWORD):
    """Log into the app through the test client (works with or without CSRF)."""
    client.get("/login")
    data = {"usuario": usuario, "password": password}
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if token:
        data["csrf_token"] = token
    return client.post("/login", data=data)


def _with_retry(fn):
    for attempt in range(_RETRIES):
        try:
            return fn()
        except Exception:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


def _warm_up():
    _with_retry(lambda: boletas.count_documents({}))
    _with_retry(lambda: vendedores.count_documents({}))
    _with_retry(lambda: configuracion.find_one({"_id": "rifa"}))
    _with_retry(lambda: rifas.count_documents({}))


def _crear_indices():
    collections = {
        "boletas": boletas,
        "vendedores": vendedores,
        "facturas": facturas,
        "rifas": rifas,
        "configuracion": configuracion,
    }
    for nombre_col, collection in collections.items():
        if collection is None:
            continue
        for key_spec, name in REQUIRED_INDEXES.get(nombre_col, []):
            spec = {key_spec: 1} if isinstance(key_spec, str) else key_spec
            collection.create_index(spec, name=name)


def _seed_once():
    boletas.drop()
    vendedores.drop()
    facturas.drop()
    rifas.drop()
    configuracion.drop()
    _seed_usuarios()

    rifa_id = rifas.insert_one(
        {
            "nombre": "Rifa Test",
            "anio": 2026,
            "valor_boleta": 70000,
            "cantidad_boletas": N_BOLETAS,
            "premio_mayor": "",
            "comisiones_tiers": COMISION_TIERS,
            "estado": "activa",
            "creada_en": None,
        }
    ).inserted_id

    docs = [
        {
            "_id": n,
            "rifa_id": rifa_id,
            "vendedor_id": "",
            "cliente": {"nombre": "", "telefono": "", "direccion": ""},
            "estado": "disponible",
            "total_abonado": 0,
            "historial_pagos": [],
            "fecha_adquisicion": None,
        }
        for n in range(N_BOLETAS)
    ]
    boletas.insert_many(docs)
    configuracion.insert_one({"_id": "rifa", "factura_counter": 0})
    _crear_indices()
    _warm_up()
    invalidate_config_cache()
    invalidate_dashboard_cache()


def _reset():
    facturas.delete_many({})
    vendedores.delete_many({})
    boletas.update_many(
        {},
        {
            "$set": {
                "vendedor_id": "",
                "cliente": {"nombre": "", "telefono": "", "direccion": ""},
                "estado": "disponible",
                "total_abonado": 0,
                "historial_pagos": [],
                "fecha_adquisicion": None,
            }
        },
    )
    configuracion.update_one({"_id": "rifa"}, {"$set": {"factura_counter": 0}})
    _seed_usuarios()
    invalidate_config_cache()
    invalidate_dashboard_cache()


@pytest.fixture(scope="session", autouse=True)
def _session_seed():
    _seed_once()
    yield
    client = getattr(database, "_client", None)
    if client is None:
        return
    with contextlib.suppress(Exception):
        client.drop_database(TEST_DB_NAME)


@pytest.fixture(autouse=True)
def seeded_db():
    _reset()
    yield


@pytest.fixture()
def client():
    flask_app.config.update(TESTING=True)
    c = flask_app.test_client()
    resp = login(c)
    assert resp.status_code == 302, "Fallo el login del usuario admin de prueba"
    return c


@pytest.fixture()
def client_caja():
    flask_app.config.update(TESTING=True)
    c = flask_app.test_client()
    resp = login(c, usuario=CAJA_USUARIO, password=CAJA_PASSWORD)
    assert resp.status_code == 302, "Fallo el login del usuario caja de prueba"
    return c


@pytest.fixture()
def client_anon():
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()

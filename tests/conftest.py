import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("MONGO_DB", "sistema_boleteria_test")
os.environ.setdefault("MONGO_TIMEOUT_MS", "8000")

import pytest

from app import app as flask_app
from database import boletas, configuracion, facturas, rifas, vendedores
from motores.cache import invalidate_config_cache, invalidate_dashboard_cache
from optimizar_db import REQUIRED_INDEXES

COMISION_TIERS = [
    {"min": 0, "valor": 0},
    {"min": 10, "valor": 10000},
    {"min": 21, "valor": 15000},
    {"min": 51, "valor": 20000},
]

N_BOLETAS = 500
_RETRIES = 3


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
            }
        },
    )
    configuracion.update_one({"_id": "rifa"}, {"$set": {"factura_counter": 0}})
    invalidate_config_cache()
    invalidate_dashboard_cache()


@pytest.fixture(scope="session", autouse=True)
def _session_seed():
    _seed_once()
    yield


@pytest.fixture(autouse=True)
def seeded_db():
    _reset()
    yield


@pytest.fixture()
def client():
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()

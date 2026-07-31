import copy
import logging
import time

from database import boletas, configuracion, facturas, rifas, vendedores
from motores.cache import (
    CONFIG_CACHE,
    CONFIG_CACHE_SECONDS,
    RIFA_CACHE,
    RIFA_CACHE_SECONDS,
)
from motores.constants import COMISION_DEFAULT_TIERS, CONFIG_ID, DEFAULT_CONFIG, DEFAULT_RIFA
from motores.fechas import now_local


def get_rifa_activa(force: bool = False) -> dict:
    """Return active rifa document with 30s cache."""
    if not force and RIFA_CACHE["data"] and time.monotonic() - RIFA_CACHE["loaded_at"] < RIFA_CACHE_SECONDS:
        return RIFA_CACHE["data"].copy()

    if rifas is None:
        return DEFAULT_RIFA.copy()

    try:
        rifa = rifas.find_one({"estado": "activa"})
        if not rifa:
            stored = configuracion.find_one({"_id": CONFIG_ID}) if configuracion is not None else None
            if stored:
                rifa = migrar_config_a_rifa(stored)
            else:
                rifa = DEFAULT_RIFA.copy()
                rifa["creada_en"] = now_local()
                rifa["_id"] = rifas.insert_one(rifa).inserted_id
        else:
            RIFA_CACHE["data"] = rifa.copy()
        RIFA_CACHE["loaded_at"] = time.monotonic()
        migrar_boletas_existentes(rifa["_id"])
        return rifa.copy()
    except Exception as exc:
        logging.getLogger(__name__).error("get_rifa_activa: %s: %s", type(exc).__name__, exc)
        return DEFAULT_RIFA.copy()


def migrar_config_a_rifa(config_doc: dict) -> dict | None:
    """Convert legacy config document to rifa collection format."""
    rifa = {
        "nombre": config_doc.get("nombre_rifa", DEFAULT_RIFA["nombre"]),
        "anio": DEFAULT_RIFA["anio"],
        "valor_boleta": int(config_doc.get("valor_boleta", DEFAULT_RIFA["valor_boleta"])),
        "cantidad_boletas": DEFAULT_RIFA["cantidad_boletas"],
        "premio_mayor": "",
        "comisiones_tiers": config_doc.get("comisiones_tiers", COMISION_DEFAULT_TIERS),
        "estado": "activa",
        "creada_en": now_local(),
    }
    result = rifas.update_one({"estado": "activa"}, {"$setOnInsert": rifa}, upsert=True)
    if result.upserted_id:
        rifa["_id"] = result.upserted_id
    else:
        rifa = rifas.find_one({"estado": "activa"})
    return rifa


def migrar_boletas_existentes(rifa_id: str) -> None:
    """Backfill missing 'rifa_id' on old ticket docs (idempotent)."""
    if boletas is None:
        return
    pendientes = boletas.count_documents({"rifa_id": {"$exists": False}})
    if pendientes:
        boletas.update_many({"rifa_id": {"$exists": False}}, {"$set": {"rifa_id": rifa_id}})


def require_collections() -> None:
    """Raise RuntimeError if any DB collection is None (no connection)."""
    required = [boletas, configuracion, facturas, rifas, vendedores]
    if any(collection is None for collection in required):
        raise RuntimeError("No hay conexi\u00f3n activa a MongoDB.")


def get_config(force: bool = False) -> dict:
    """Return merged config (rifa + stored overrides) with 30s cache."""
    if not force and CONFIG_CACHE["data"] and time.monotonic() - CONFIG_CACHE["loaded_at"] < CONFIG_CACHE_SECONDS:
        return copy.deepcopy(CONFIG_CACHE["data"])

    rifa = get_rifa_activa(force)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "nombre_rifa": rifa.get("nombre", DEFAULT_CONFIG["nombre_rifa"]),
            "valor_boleta": int(rifa.get("valor_boleta", DEFAULT_CONFIG["valor_boleta"])),
            "cantidad_boletas": int(rifa.get("cantidad_boletas", 10000)),
            "premio_mayor": rifa.get("premio_mayor", ""),
            "estado": rifa.get("estado", "activa"),
            "comisiones_tiers": rifa.get("comisiones_tiers", COMISION_DEFAULT_TIERS),
            "rifa_id": rifa.get("_id"),
        }
    )

    stored = configuracion.find_one({"_id": CONFIG_ID}) if configuracion is not None else None
    if stored:
        if stored.get("nombre_rifa"):
            config["nombre_rifa"] = stored["nombre_rifa"]
        if stored.get("valor_boleta") is not None:
            config["valor_boleta"] = int(stored["valor_boleta"])
        if stored.get("cantidad_boletas") is not None:
            config["cantidad_boletas"] = int(stored["cantidad_boletas"])
        if stored.get("premio_mayor"):
            config["premio_mayor"] = stored["premio_mayor"]
        if stored.get("estado"):
            config["estado"] = stored["estado"]
        if stored.get("comisiones_tiers") is not None:
            config["comisiones_tiers"] = stored["comisiones_tiers"]
        for k in ("nombre_empresa", "direccion", "telefono", "ciudad", "footer_texto", "observaciones_recaudo"):
            if stored.get(k):
                config[k] = stored[k]

    CONFIG_CACHE["data"] = copy.deepcopy(config)
    CONFIG_CACHE["loaded_at"] = time.monotonic()
    return config

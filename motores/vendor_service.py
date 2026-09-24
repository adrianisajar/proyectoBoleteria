import copy
import logging
import re
import time

from flask import flash

from database import boletas, configuracion, facturas, vendedores
from motores.cache import VENDOR_PANEL_CACHE, VENDOR_PANEL_CACHE_SECONDS, VENDOR_PANEL_LOCK
from motores.config_service import get_config, require_collections
from motores.constants import (
    COMISION_DEFAULT_TIERS,
    CONFIG_ID,
    VENDEDOR_LOCAL,
    VENDEDOR_LOCAL_LABEL,
)
from motores.validacion import safe_error_message

logger = logging.getLogger(__name__)

VENDEDOR_ID_PREFIX = "VEND_"


def normalize_vendedor_id(value: str) -> str:
    """Normalize and validate a vendor id (uppercase, 2-32 chars, alnum/-/_)."""
    vendedor_id = re.sub(r"\s+", "_", value.strip().upper())
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", vendedor_id):
        raise ValueError("El ID del vendedor debe tener 2 a 32 caracteres: letras, números, guion o guion bajo.")
    return vendedor_id


def next_vendedor_id() -> str:
    """Return the next sequential vendor id (VEND_0001, VEND_0002, ...).

    The counter lives in ``configuracion.vendedor_counter`` and is persistent
    across rifas. If a candidate already exists (e.g. taken manually before this
    scheme was in place) it is skipped and the counter keeps advancing.
    """
    for _retry in range(50):
        result = configuracion.find_one_and_update(
            {"_id": CONFIG_ID},
            {"$inc": {"vendedor_counter": 1}},
            upsert=True,
            return_document=True,
        )
        counter = int(result["vendedor_counter"] if result else 1)
        candidate = f"{VENDEDOR_ID_PREFIX}{counter:04d}"
        if vendedores.count_documents({"_id": candidate}) == 0:
            return candidate
    raise RuntimeError("No se pudo obtener un id de vendedor libre tras 50 intentos.")


def calc_comision_por_boleta(vendidas: int, tiers: list[dict] | None = None) -> int:
    """Return the commission per ticket for the tier matching the sold count."""
    if tiers is None:
        config = get_config()
        tiers = config.get("comisiones_tiers", COMISION_DEFAULT_TIERS)
    tiers_sorted = sorted(tiers, key=lambda t: t["min"], reverse=True)
    for tier in tiers_sorted:
        if vendidas >= tier["min"]:
            return int(tier["valor"])
    return 0


def get_vendedor_options() -> list[dict]:
    """Return vendores as [{_id, nombre}] sorted by id (for select/autocomplete)."""
    require_collections()
    cursor = vendedores.find({}, {"nombre": 1}).sort("_id", 1)
    return [{"_id": doc["_id"], "nombre": doc.get("nombre", "")} for doc in cursor]


def vendedores_con_local() -> list[dict]:
    """Return all vendors plus the LOCAL system vendor (for selects/autocomplete)."""
    lista = list(vendedores.find({}, {"_id": 1, "nombre": 1}).sort("_id", 1))
    return [*[{"_id": VENDEDOR_LOCAL, "nombre": VENDEDOR_LOCAL_LABEL}], *lista]


def get_vendedores_snapshot(config: dict | None = None) -> tuple[list, dict]:
    """Build the vendor panel list with stats (asignadas, vendidas, recaudado, comisión, egresos).

    Cached for 30s to avoid re-running the heavy aggregation on every page load.
    """
    with VENDOR_PANEL_LOCK:
        if VENDOR_PANEL_CACHE["data"] and time.monotonic() - VENDOR_PANEL_CACHE["loaded_at"] < VENDOR_PANEL_CACHE_SECONDS:
            cached = copy.deepcopy(VENDOR_PANEL_CACHE["data"])
            return cached["lista"], cached["stats"]

    require_collections()
    config = config or get_config()
    valor_boleta = int(config["valor_boleta"])
    rifa_id = config.get("rifa_id")
    match = [{"$match": {"rifa_id": rifa_id}}] if rifa_id else []

    egresos_por_vendedor = _egresos_por_vendedor()
    total_egresos = sum(egresos_por_vendedor.values())

    stats_docs = list(
        boletas.aggregate(
            [
                *match,
                {
                    "$group": {
                        "_id": "$vendedor_id",
                        "vendidas": {"$sum": {"$cond": [{"$gte": ["$total_abonado", valor_boleta]}, 1, 0]}},
                        "pagadas": {"$sum": {"$cond": [{"$eq": ["$estado", "pagada"]}, 1, 0]}},
                        "recaudado": {"$sum": {"$ifNull": ["$total_abonado", 0]}},
                        "saldo_pendiente": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$gt": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                                            {"$lt": [{"$ifNull": ["$total_abonado", 0]}, valor_boleta]},
                                        ]
                                    },
                                    {"$subtract": [valor_boleta, {"$ifNull": ["$total_abonado", 0]}]},
                                    0,
                                ]
                            }
                        },
                    }
                },
            ]
        )
    )
    stats_by_vendor = {doc["_id"]: doc for doc in stats_docs}

    lista = []
    total_asignadas = 0
    total_recaudado = 0
    total_comision = 0

    # LOCAL is a system vendor (no DB doc): show it first with its own stats.
    local_match = {"vendedor_id": VENDEDOR_LOCAL}
    if rifa_id:
        local_match["rifa_id"] = rifa_id
    local_count = boletas.count_documents(local_match)
    local_preview = [doc["_id"] for doc in boletas.find(local_match, {"_id": 1}).sort("_id", 1).limit(12)]
    local_stats = stats_by_vendor.get(
        VENDEDOR_LOCAL,
        {"vendidas": 0, "pagadas": 0, "recaudado": 0, "saldo_pendiente": 0},
    )
    local_recaudado = int(local_stats.get("recaudado", 0) or 0)
    total_asignadas += local_count
    total_recaudado += local_recaudado
    lista.append(
        {
            "_id": VENDEDOR_LOCAL,
            "nombre": VENDEDOR_LOCAL_LABEL,
            "telefono": "",
            "cantidad": local_count,
            "preview": local_preview,
            "vendidas": int(local_stats.get("vendidas", 0) or 0),
            "pagadas": int(local_stats.get("pagadas", 0) or 0),
            "pendientes_fisicas": max(local_count - int(local_stats.get("vendidas", 0) or 0), 0),
            "recaudado": local_recaudado,
            "saldo_pendiente": int(local_stats.get("saldo_pendiente", 0) or 0),
            "comision_por_boleta": 0,
            "comision": 0,
            "total_egresos": int(egresos_por_vendedor.get(VENDEDOR_LOCAL, 0) or 0),
            "es_local": True,
        }
    )

    cursor = vendedores.find({}, {"nombre": 1, "telefono": 1, "boletas_asignadas": 1}).sort("_id", 1)
    for vendedor in cursor:
        asignadas = sorted(vendedor.get("boletas_asignadas") or [])
        cantidad = len(asignadas)
        stats = stats_by_vendor.get(
            vendedor["_id"],
            {"vendidas": 0, "pagadas": 0, "recaudado": 0, "saldo_pendiente": 0},
        )
        recaudado = int(stats.get("recaudado", 0) or 0)
        vendidas = int(stats.get("vendidas", 0) or 0)
        comision_por_boleta = calc_comision_por_boleta(vendidas)
        comision = vendidas * comision_por_boleta

        total_asignadas += cantidad
        total_recaudado += recaudado
        total_comision += comision
        lista.append(
            {
                "_id": vendedor["_id"],
                "nombre": vendedor.get("nombre", ""),
                "telefono": vendedor.get("telefono", ""),
                "cantidad": cantidad,
                "preview": asignadas[:12],
                "vendidas": vendidas,
                "pagadas": stats.get("pagadas", 0),
                "pendientes_fisicas": max(cantidad - vendidas, 0),
                "recaudado": recaudado,
                "saldo_pendiente": int(stats.get("saldo_pendiente", 0) or 0),
                "comision_por_boleta": comision_por_boleta,
                "comision": comision,
                "total_egresos": int(egresos_por_vendedor.get(vendedor["_id"], 0) or 0),
            }
        )

    stats_data = {
        "total_asignadas": total_asignadas,
        "total_recaudado": total_recaudado,
        "total_comision": total_comision,
        "total_egresos": total_egresos,
        "total_vendedores": sum(1 for v in lista if v["_id"] != VENDEDOR_LOCAL),
    }
    with VENDOR_PANEL_LOCK:
        VENDOR_PANEL_CACHE["data"] = {"lista": lista, "stats": stats_data}
        VENDOR_PANEL_CACHE["loaded_at"] = time.monotonic()
    return lista, stats_data


def _egresos_por_vendedor() -> dict:
    """Return {vendedor_id: sum_of_egreso_invoices} from egreso facturas."""
    pipeline = [
        {"$match": {"tipo": "egreso", "anulada": {"$ne": True}}},
        {"$group": {"_id": "$vendedor_id", "total": {"$sum": {"$ifNull": ["$valor_total", 0]}}}},
    ]
    try:
        return {doc["_id"]: int(doc.get("total") or 0) for doc in facturas.aggregate(pipeline)}
    except Exception as exc:
        logger.warning("No se pudieron calcular egresos por vendedor: %s", exc)
        return {}


def safe_vendedores_snapshot() -> tuple[list, dict]:
    """Like get_vendedores_snapshot but never raises; returns empty stats on error."""
    try:
        return get_vendedores_snapshot()
    except Exception as exc:
        flash(safe_error_message(exc), "danger")
        return [], {"total_asignadas": 0, "total_recaudado": 0, "total_comision": 0, "total_egresos": 0, "total_vendedores": 0}


def vendedor_label(vendedor_id: str, nombres_vendedores: dict) -> str:
    """Build a short display label for a vendor (handles LOCAL / empty ids)."""
    if not vendedor_id:
        return "SIN REGISTRAR"
    nombre = nombres_vendedores.get(vendedor_id, "")
    if vendedor_id == VENDEDOR_LOCAL or nombre == VENDEDOR_LOCAL:
        return "VEND. LOCAL"
    return f"VEND. {nombre or vendedor_id}".upper()

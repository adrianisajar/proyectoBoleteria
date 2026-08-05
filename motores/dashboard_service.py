import time

from pymongo.collection import Collection

from database import boletas, vendedores
from motores.cache import (
    DASHBOARD_CACHE,
    DASHBOARD_CACHE_SECONDS,
    GLOBAL_COUNTS_CACHE,
)
from motores.config_service import get_config, migrar_boletas_existentes, require_collections
from motores.constants import METODO_EFECTIVO, METODO_TRANSFERENCIA, VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL
from motores.fechas import now_local


def get_alertas() -> list:
    """Return active alerts list (currently always empty)."""
    return []


def get_dashboard_counts(rifa_id: str | None = None, valor_boleta: int | None = None) -> dict:
    """Aggregate ticket counts by state for the given rifa (or all tickets).

    The global (rifa_id=None) result is cached with a 30s TTL; the dashboard
    ``get_dashboard_stats`` still caches its own per-rifa snapshot separately.
    """
    require_collections()
    if rifa_id is None:
        cached = GLOBAL_COUNTS_CACHE
        if cached["data"] and cached["valor"] == valor_boleta and time.monotonic() - cached["loaded_at"] < DASHBOARD_CACHE_SECONDS:
            return cached["data"].copy()
    match = {}
    if rifa_id:
        match["rifa_id"] = rifa_id
    pipeline = [{"$match": match}] if match else []
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "recaudo_total": {"$sum": {"$ifNull": ["$total_abonado", 0]}},
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
                    "pagadas": {"$sum": {"$cond": [{"$gte": [{"$ifNull": ["$total_abonado", 0]}, valor_boleta]}, 1, 0]}},
                    "abonando": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$gt": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                                        {"$lt": [{"$ifNull": ["$total_abonado", 0]}, valor_boleta]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "separadas": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$eq": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                                        {"$eq": [{"$ifNull": ["$vendedor_id", ""]}, VENDEDOR_LOCAL]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "asignadas": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$eq": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                                        {"$not": {"$in": [{"$ifNull": ["$vendedor_id", ""]}, ["", None, VENDEDOR_LOCAL]]}},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            }
        ]
    )
    stats = first_aggregate(boletas, pipeline, {})
    total = int(stats.get("total", 0) or 0)
    pagadas = int(stats.get("pagadas", 0) or 0)
    abonando = int(stats.get("abonando", 0) or 0)
    separadas = int(stats.get("separadas", 0) or 0)
    asignadas = int(stats.get("asignadas", 0) or 0)
    disponibles = total - pagadas - abonando - separadas - asignadas
    result = {
        "total": total,
        "recaudo_total": int(stats.get("recaudo_total", 0) or 0),
        "saldo_pendiente": int(stats.get("saldo_pendiente", 0) or 0),
        "disponibles": max(disponibles, 0),
        "separadas": separadas,
        "abonando": abonando,
        "pagadas": pagadas,
        "vendidas": pagadas + abonando + separadas + asignadas,
        "asignadas": asignadas,
    }
    if rifa_id is None:
        GLOBAL_COUNTS_CACHE["data"] = result
        GLOBAL_COUNTS_CACHE["loaded_at"] = time.monotonic()
        GLOBAL_COUNTS_CACHE["valor"] = valor_boleta
    return result


def first_aggregate(collection: Collection, pipeline: list, default: dict | None = None) -> dict:
    """Run an aggregation and return the first result doc (or the default)."""
    return next(collection.aggregate(pipeline), None) or default or {}


def get_dashboard_stats(force: bool = False) -> dict:
    """Compute dashboard stats (recaudos, states, ranking) with 30s cache."""
    if not force and DASHBOARD_CACHE["data"] and time.monotonic() - DASHBOARD_CACHE["loaded_at"] < DASHBOARD_CACHE_SECONDS:
        return DASHBOARD_CACHE["data"].copy()
    require_collections()
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    rifa_id = config.get("rifa_id")
    today = now_local().date().isoformat()
    counts = get_dashboard_counts(rifa_id, valor_boleta)
    if rifa_id and counts.get("total", 0) == 0 and boletas.count_documents({}) > 0:
        migrar_boletas_existentes(rifa_id)
        counts = get_dashboard_counts(rifa_id, valor_boleta)
    total_boletas = int(counts.get("total", 0) or 0)
    vendidas = int(counts.get("vendidas", 0) or 0)

    match_rifa = [{"$match": {"rifa_id": rifa_id}}] if rifa_id else []

    today_totals = first_aggregate(
        boletas,
        [
            *match_rifa,
            {"$match": {"historial_pagos.fecha": today}},
            {"$unwind": {"path": "$historial_pagos", "preserveNullAndEmptyArrays": False}},
            {"$match": {"historial_pagos.fecha": today}},
            {"$group": {"_id": None, "recaudo_hoy": {"$sum": "$historial_pagos.valor"}, "pagos_hoy": {"$sum": 1}}},
        ],
        {"recaudo_hoy": 0, "pagos_hoy": 0},
    )

    pagos_por_metodo = list(
        boletas.aggregate(
            [
                *match_rifa,
                {"$unwind": {"path": "$historial_pagos", "preserveNullAndEmptyArrays": False}},
                {"$group": {"_id": "$historial_pagos.metodo", "total": {"$sum": "$historial_pagos.valor"}}},
            ],
        )
    )
    pagos_efectivo = 0
    pagos_transferencia = 0
    pagos_otros = 0
    for item in pagos_por_metodo:
        if item["_id"] == METODO_EFECTIVO:
            pagos_efectivo = int(item.get("total", 0) or 0)
        elif item["_id"] == METODO_TRANSFERENCIA:
            pagos_transferencia = int(item.get("total", 0) or 0)
        else:
            pagos_otros += int(item.get("total", 0) or 0)

    ranking_query = {"total_abonado": {"$gt": 0}}
    if rifa_id:
        ranking_query["rifa_id"] = rifa_id
    ranking = list(
        boletas.aggregate(
            [
                {"$match": ranking_query},
                {
                    "$group": {
                        "_id": "$vendedor_id",
                        "recaudo": {"$sum": "$total_abonado"},
                        "vendidas": {"$sum": 1},
                        "pagadas": {"$sum": {"$cond": [{"$eq": ["$estado", "pagada"]}, 1, 0]}},
                    }
                },
                {"$sort": {"recaudo": -1}},
                {"$limit": 8},
            ]
        )
    )
    if ranking:
        vendedor_ids = [item["_id"] for item in ranking if item["_id"] not in ("", None, VENDEDOR_LOCAL)]
        nombres = {}
        if vendedor_ids:
            for doc in vendedores.find({"_id": {"$in": vendedor_ids}}, {"nombre": 1}):
                nombres[doc["_id"]] = doc.get("nombre", "")
        for item in ranking:
            item["nombre"] = nombres.get(
                item["_id"], VENDEDOR_LOCAL_LABEL if item["_id"] == VENDEDOR_LOCAL else "Sin registrar" if item["_id"] in ("", None) else ""
            )

    recaudo_potencial = total_boletas * valor_boleta

    result = {
        **counts,
        "recaudo_hoy": today_totals.get("recaudo_hoy", 0),
        "pagos_hoy": today_totals.get("pagos_hoy", 0),
        "ranking": ranking,
        "valor_boleta": valor_boleta,
        "recaudo_potencial": recaudo_potencial,
        "progreso_ventas_pct": min(round((vendidas / total_boletas) * 100, 1), 100.0) if total_boletas else 0,
        "progreso_recaudo_pct": min(round((counts["recaudo_total"] / recaudo_potencial) * 100, 1), 100.0) if recaudo_potencial else 0,
        "pagos_efectivo": pagos_efectivo,
        "pagos_transferencia": pagos_transferencia,
        "pagos_otros": pagos_otros,
    }
    DASHBOARD_CACHE["data"] = result
    DASHBOARD_CACHE["loaded_at"] = time.monotonic()
    return result

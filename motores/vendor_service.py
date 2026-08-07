import re

from flask import flash

from database import boletas, facturas, vendedores
from motores.config_service import get_config, require_collections
from motores.constants import COMISION_DEFAULT_TIERS, VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL


def normalize_vendedor_id(value: str) -> str:
    """Normalize and validate a vendor id (uppercase, 2-32 chars, alnum/-/_)."""
    vendedor_id = re.sub(r"\s+", "_", value.strip().upper())
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", vendedor_id):
        raise ValueError("El ID del vendedor debe tener 2 a 32 caracteres: letras, números, guion o guion bajo.")
    return vendedor_id


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


def existing_boleta_ids(boleta_ids: list[int]) -> list[int]:
    """Filter the given ticket ids to those that actually exist in the collection."""
    if not boleta_ids:
        return []

    cursor = boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1})
    existing = {doc["_id"] for doc in cursor}
    return [boleta_id for boleta_id in boleta_ids if boleta_id in existing]


def get_vendedores_snapshot(config: dict | None = None) -> tuple[list, dict]:
    """Build the vendor panel list with stats (asignadas, vendidas, recaudado, comisión, egresos)."""
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
    local_docs = boletas.find(local_match, {"_id": 1, "total_abonado": 1}).sort("_id", 1)
    local_ids = [doc["_id"] for doc in local_docs]
    local_stats = stats_by_vendor.get(
        VENDEDOR_LOCAL,
        {"vendidas": 0, "pagadas": 0, "recaudado": 0, "saldo_pendiente": 0},
    )
    local_recaudado = int(local_stats.get("recaudado", 0) or 0)
    total_asignadas += len(local_ids)
    total_recaudado += local_recaudado
    lista.append(
        {
            "_id": VENDEDOR_LOCAL,
            "nombre": VENDEDOR_LOCAL_LABEL,
            "telefono": "",
            "cantidad": len(local_ids),
            "preview": local_ids[:12],
            "vendidas": int(local_stats.get("vendidas", 0) or 0),
            "pagadas": int(local_stats.get("pagadas", 0) or 0),
            "pendientes_fisicas": max(len(local_ids) - int(local_stats.get("vendidas", 0) or 0), 0),
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

    return lista, {
        "total_asignadas": total_asignadas,
        "total_recaudado": total_recaudado,
        "total_comision": total_comision,
        "total_egresos": total_egresos,
        "total_vendedores": sum(1 for v in lista if v["_id"] != VENDEDOR_LOCAL),
    }


def _egresos_por_vendedor() -> dict:
    """Return {vendedor_id: sum_of_egreso_invoices} from egreso facturas."""
    pipeline = [
        {"$match": {"tipo": "egreso"}},
        {"$group": {"_id": "$vendedor_id", "total": {"$sum": {"$ifNull": ["$valor_total", 0]}}}},
    ]
    try:
        return {doc["_id"]: int(doc.get("total") or 0) for doc in facturas.aggregate(pipeline)}
    except Exception:
        return {}


def safe_vendedores_snapshot() -> tuple[list, dict]:
    """Like get_vendedores_snapshot but never raises; returns empty stats on error."""
    try:
        return get_vendedores_snapshot()
    except Exception as exc:
        flash(f"No se pudo cargar el listado de vendedores: {exc}", "danger")
        return [], {"total_asignadas": 0, "total_recaudado": 0, "total_comision": 0, "total_egresos": 0, "total_vendedores": 0}


def vendedor_label(vendedor_id: str, nombres_vendedores: dict) -> str:
    """Build a short display label for a vendor (handles LOCAL / empty ids)."""
    if not vendedor_id:
        return "SIN REGISTRAR"
    nombre = nombres_vendedores.get(vendedor_id, "")
    if vendedor_id == VENDEDOR_LOCAL or nombre == VENDEDOR_LOCAL:
        return "VEND. LOCAL"
    return f"VEND. {nombre or vendedor_id}".upper()

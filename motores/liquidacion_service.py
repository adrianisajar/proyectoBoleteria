from datetime import datetime

from database import boletas, configuracion, liquidaciones, vendedores
from motores.auth import current_user
from motores.cache import invalidate_dashboard_cache
from motores.config_service import get_config, require_collections
from motores.constants import COMISION_DEFAULT_TIERS, CONFIG_ID, USUARIO_SISTEMA
from motores.fechas import now_local

ESTADO_PENDIENTE = "pendiente"
ESTADO_PARCIAL = "parcial"
ESTADO_LIQUIDADA = "liquidada"


def next_liquidacion_id() -> int:
    """Atomically increment and return the next liquidación id from configuracion."""
    result = configuracion.find_one_and_update(
        {"_id": CONFIG_ID},
        {"$inc": {"liquidacion_counter": 1}},
        upsert=True,
        return_document=True,
    )
    return result["liquidacion_counter"] if result else 1


def tier_aplicado(vendidas: int, tiers: list[dict] | None = None) -> dict:
    """Return the commission tier ({min, valor}) matching the sold count."""
    if tiers is None:
        config = get_config()
        tiers = config.get("comisiones_tiers", COMISION_DEFAULT_TIERS)
    tiers_sorted = sorted(tiers, key=lambda t: t["min"], reverse=True)
    for tier in tiers_sorted:
        if vendidas >= tier["min"]:
            return tier
    return {"min": 0, "valor": 0}


def estado_liquidacion(total_liquidado: int, total_comision: int) -> str:
    """Derive liquidación estado from the amounts paid vs the commission owed."""
    if total_comision <= 0:
        return ESTADO_PENDIENTE
    if total_liquidado >= total_comision:
        return ESTADO_LIQUIDADA
    if total_liquidado > 0:
        return ESTADO_PARCIAL
    return ESTADO_PENDIENTE


def _stats_por_vendedor(config: dict) -> dict:
    """Aggregate ticket stats per vendor (vendidas = con abono, pagadas = al 100%)."""
    valor_boleta = int(config["valor_boleta"])
    rifa_id = config.get("rifa_id")
    match = [{"$match": {"rifa_id": rifa_id}}] if rifa_id else []
    docs = list(
        boletas.aggregate(
            [
                *match,
                {
                    "$group": {
                        "_id": "$vendedor_id",
                        "en_sistema": {"$sum": 1},
                        "con_abono": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$total_abonado", 0]}, 0]}, 1, 0]}},
                        "pagadas": {"$sum": {"$cond": [{"$gte": [{"$ifNull": ["$total_abonado", 0]}, valor_boleta]}, 1, 0]}},
                        "recaudado": {"$sum": {"$ifNull": ["$total_abonado", 0]}},
                    }
                },
            ]
        )
    )
    return {doc["_id"]: doc for doc in docs}


def _liquidaciones_por_vendedor(config: dict) -> dict:
    """Map vendedor_id -> latest liquidación doc for the current rifa."""
    rifa_id = config.get("rifa_id")
    filtro = {"rifa_id": rifa_id} if rifa_id else {}
    docs = list(liquidaciones.find(filtro).sort([("_id", 1)]))
    por_vendedor = {}
    for doc in docs:
        por_vendedor[doc.get("vendedor_id")] = doc
    return por_vendedor


def _comision_vendedor(stats: dict) -> tuple[int, int, dict]:
    """Return (comision_por_boleta, total_comision, tier) from vendor stats."""
    pagadas = int(stats.get("pagadas", 0) or 0)
    tier = tier_aplicado(pagadas)
    comision_por_boleta = int(tier.get("valor", 0) or 0)
    total_comision = pagadas * comision_por_boleta
    return comision_por_boleta, total_comision, tier


def get_liquidaciones_resumen(config: dict | None = None) -> tuple[list[dict], dict]:
    """Build the liquidaciones panel rows (live stats + estado) and summary totals."""
    require_collections()
    config = config or get_config()
    stats_by_vendor = _stats_por_vendedor(config)
    liquidaciones_map = _liquidaciones_por_vendedor(config)

    rows = []
    total_comisiones = 0
    total_liquidado = 0
    total_vendedores = 0

    cursor = vendedores.find({}, {"nombre": 1, "telefono": 1, "boletas_asignadas": 1}).sort("_id", 1)
    for vendedor in cursor:
        stats = stats_by_vendor.get(vendedor["_id"], {"con_abono": 0, "pagadas": 0, "recaudado": 0})
        comision_por_boleta, total_comision, tier = _comision_vendedor(stats)

        liqui = liquidaciones_map.get(vendedor["_id"])
        liquidado = int((liqui or {}).get("total_liquidado", 0) or 0)
        estado = ESTADO_PENDIENTE if liqui is None else (liqui.get("estado") or estado_liquidacion(liquidado, total_comision))

        total_comisiones += total_comision
        total_liquidado += liquidado
        total_vendedores += 1
        rows.append(
            {
                "_id": vendedor["_id"],
                "nombre": vendedor.get("nombre", ""),
                "vendidas": int(stats.get("con_abono", 0) or 0),
                "pagadas": int(stats.get("pagadas", 0) or 0),
                "pendientes": max(int(stats.get("con_abono", 0) or 0) - int(stats.get("pagadas", 0) or 0), 0),
                "recaudado": int(stats.get("recaudado", 0) or 0),
                "comision_por_boleta": comision_por_boleta,
                "tier": tier,
                "total_comision": total_comision,
                "liquidado": liquidado,
                "pendiente_pagar": max(total_comision - liquidado, 0),
                "estado": estado,
                "liquidacion_id": (liqui or {}).get("_id"),
            }
        )

    resumen = {
        "total_vendedores": total_vendedores,
        "total_comisiones": total_comisiones,
        "total_liquidado": total_liquidado,
        "total_pendiente": max(total_comisiones - total_liquidado, 0),
    }
    return rows, resumen


def get_liquidacion_detalle(vendedor_id: str, config: dict | None = None) -> dict:
    """Build the detail view for a vendor: live stats, commission breakdown, pending tickets."""
    require_collections()
    config = config or get_config()
    vendedor = vendedores.find_one({"_id": vendedor_id})
    if not vendedor:
        raise ValueError(f"El vendedor {vendedor_id} no existe.")

    asignadas = sorted(vendedor.get("boletas_asignadas") or [])
    stats = _stats_por_vendedor(config).get(vendedor_id, {"con_abono": 0, "pagadas": 0, "recaudado": 0})
    valor_boleta = int(config["valor_boleta"])
    comision_por_boleta, total_comision, tier = _comision_vendedor(stats)

    pagadas = int(stats.get("pagadas", 0) or 0)
    pendientes_docs = []
    if asignadas:
        docs = list(boletas.find({"_id": {"$in": asignadas}}, {"_id": 1, "total_abonado": 1, "cliente": 1}).sort("_id", 1))
        pendientes_docs = [
            {
                "numero": doc["_id"],
                "abonado": int(doc.get("total_abonado", 0) or 0),
                "saldo": max(valor_boleta - int(doc.get("total_abonado", 0) or 0), 0),
                "cliente": (doc.get("cliente") or {}).get("nombre", ""),
            }
            for doc in docs
            if 0 < int(doc.get("total_abonado", 0) or 0) < valor_boleta
        ]

    liqui = _liquidaciones_por_vendedor(config).get(vendedor_id)
    liquidado = int((liqui or {}).get("total_liquidado", 0) or 0)
    estado = ESTADO_PENDIENTE if liqui is None else (liqui.get("estado") or estado_liquidacion(liquidado, total_comision))

    return {
        "_id": vendedor_id,
        "nombre": vendedor.get("nombre", ""),
        "telefono": vendedor.get("telefono", ""),
        "vendidas": int(stats.get("con_abono", 0) or 0),
        "pagadas": pagadas,
        "pendientes": max(int(stats.get("con_abono", 0) or 0) - pagadas, 0),
        "valor_vendido": int(stats.get("recaudado", 0) or 0),
        "comision_por_boleta": comision_por_boleta,
        "tier": tier,
        "total_comision": total_comision,
        "liquidado": liquidado,
        "pendiente_pagar": max(total_comision - liquidado, 0),
        "estado": estado,
        "pendientes_lista": pendientes_docs,
        "liquidacion": liqui,
        "valor_boleta": valor_boleta,
    }


def generar_liquidacion(vendedor_id: str, observaciones: str = "", config: dict | None = None) -> dict:
    """Persist a new liquidación snapshot for the vendor and return the stored doc."""
    require_collections()
    config = config or get_config()
    detalle = get_liquidacion_detalle(vendedor_id, config)
    liquidacion_id = next_liquidacion_id()
    usuario = (current_user() or {}).get("username", USUARIO_SISTEMA)

    doc = {
        "_id": liquidacion_id,
        "vendedor_id": vendedor_id,
        "vendedor_nombre": detalle["nombre"] or vendedor_id,
        "rifa_id": config.get("rifa_id"),
        "rifa_nombre": config.get("nombre_rifa", ""),
        "fecha": now_local(),
        "valor_boleta": int(config["valor_boleta"]),
        "boletas_vendidas": detalle["vendidas"],
        "boletas_pagadas": detalle["pagadas"],
        "boletas_pendientes": detalle["pendientes"],
        "valor_vendido": detalle["valor_vendido"],
        "comision_por_boleta": detalle["comision_por_boleta"],
        "tier": detalle["tier"],
        "total_comision": detalle["total_comision"],
        "total_liquidado": 0,
        "pendiente_pagar": detalle["total_comision"],
        "estado": ESTADO_PENDIENTE,
        "observaciones": observaciones,
        "boletas_pendientes_lista": [item["numero"] for item in detalle["pendientes_lista"]],
        "pagos": [],
        "generada_en": now_local(),
        "generada_por": usuario,
        "actualizada_en": now_local(),
    }
    liquidaciones.insert_one(doc)
    invalidate_dashboard_cache()
    return doc


def registrar_abono_liquidacion(liquidacion_id: int, monto: int, metodo: str = "efectivo", fecha: str = "", observaciones: str = "") -> dict:
    """Register a commission payment against a liquidación and update its estado."""
    require_collections()
    liqui = liquidaciones.find_one({"_id": liquidacion_id})
    if not liqui:
        raise ValueError(f"La liquidación N° {liquidacion_id} no existe.")
    if liqui.get("estado") == ESTADO_LIQUIDADA:
        raise ValueError("La liquidación ya está liquidada en su totalidad.")

    total_comision = int(liqui.get("total_comision", 0) or 0)
    liquidado_actual = int(liqui.get("total_liquidado", 0) or 0)
    pendiente = max(total_comision - liquidado_actual, 0)
    if monto <= 0:
        raise ValueError("El valor del abono debe ser mayor que cero.")
    if monto > pendiente:
        raise ValueError(f"El abono de ${monto:,} supera el saldo pendiente de ${pendiente:,}.")

    usuario = (current_user() or {}).get("username", USUARIO_SISTEMA)
    pago = {
        "fecha": fecha or now_local().date().isoformat(),
        "valor": monto,
        "metodo": metodo,
        "registrado_en": now_local(),
        "usuario": usuario,
        "observaciones": observaciones,
    }
    nuevo_liquidado = liquidado_actual + monto
    nuevo_estado = estado_liquidacion(nuevo_liquidado, total_comision)

    liquidaciones.update_one(
        {"_id": liquidacion_id},
        {
            "$push": {"pagos": pago},
            "$set": {
                "total_liquidado": nuevo_liquidado,
                "pendiente_pagar": max(total_comision - nuevo_liquidado, 0),
                "estado": nuevo_estado,
                "actualizada_en": now_local(),
            },
        },
    )
    invalidate_dashboard_cache()
    return liquidaciones.find_one({"_id": liquidacion_id})


def get_liquidacion(liquidacion_id: int, config: dict | None = None) -> dict | None:
    """Return a stored liquidación doc (for reprinting)."""
    require_collections()
    doc = liquidaciones.find_one({"_id": liquidacion_id})
    if not doc:
        return None
    if isinstance(doc.get("fecha"), datetime):
        doc["fecha_display"] = doc["fecha"].strftime("%d/%m/%Y %I:%M %p")
    return doc

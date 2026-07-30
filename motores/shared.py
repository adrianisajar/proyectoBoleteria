import copy
import io
import zipfile
import xml.etree.ElementTree as ET
import os
import sys
import re
import time
from collections import defaultdict, Counter

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
from datetime import datetime

from motores.fechas import now_local
from motores.auth import current_user, has_role, role_required
from motores.cache import (
    CONFIG_CACHE, CONFIG_CACHE_SECONDS,
    RIFA_CACHE, RIFA_CACHE_SECONDS,
    DASHBOARD_CACHE, DASHBOARD_CACHE_SECONDS,
    invalidate_rifa_cache, invalidate_dashboard_cache, invalidate_config_cache,
)
from motores.ticket_service import estado_para_total, sync_ticket_statuses, estado_pipeline_expr
from functools import wraps
from unicodedata import normalize as unicode_normalize

from flask import (
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from pymongo import UpdateOne

from database import boletas, configuracion, facturas, rifas, vendedores
from motores.constants import (
    BOLETA_MIN, BOLETA_MAX, METODOS_PAGO, OPERACIONES_VENDEDOR,
    ESTADOS_BOLETA, CONSULTA_LIMIT_DEFAULT, CONSULTA_LIMIT_MAX,
    CONFIG_ID, COMISION_DEFAULT_TIERS,
    DEFAULT_RIFA, DEFAULT_CONFIG,
    VENDEDOR_LOCAL, VENDEDOR_SIN_ASIGNAR, METODO_EFECTIVO, METODO_TRANSFERENCIA, REFERENCIA_N_A,
    USUARIO_SISTEMA,
    MODELO_RIFA_HEADERS, XLSX_NS, XLSX_REL_NS,
)
from motores.validacion import parse_int_filter, ticket_number_query, parse_money, parse_boletas_detailed, parse_boletas
from motores.modelos import crear_boleta_base
from motores.excel_export import column_letter, make_xlsx_response
from motores.excel_import import col_to_index, clean_excel_text, parse_excel_number, parse_excel_boleta, parse_excel_date
ABONADO_OP_MAP = {"gte": "$gte", "lte": "$lte", "eq": "$eq"}


def _buscar_transferencia_duplicada(ref: str, banco: str = "", exclude_factura_id: int | None = None) -> dict | None:
    elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": ref}
    if banco:
        elem_match["banco"] = banco
    if exclude_factura_id is not None:
        elem_match["factura_id"] = {"$ne": exclude_factura_id}
    return boletas.find_one({"historial_pagos": {"$elemMatch": elem_match}}, {"_id": 1})


def _build_factura_detalle(boleta_ids: list[int], factura_id: int) -> list[dict]:
    docs = list(boletas.find({"_id": {"$in": boleta_ids}}, sort=[("_id", 1)]))
    detalle = []
    for doc in docs:
        for pago in doc.get("historial_pagos") or []:
            if pago.get("factura_id") == factura_id:
                entry = {
                    "boleta": doc["_id"],
                    "fecha": str(pago.get("fecha", "")),
                    "valor": int(pago.get("valor", 0) or 0),
                    "metodo": pago.get("metodo", ""),
                }
                if pago.get("referencia"):
                    entry["referencia"] = pago["referencia"]
                if pago.get("banco"):
                    entry["banco"] = pago["banco"]
                detalle.append(entry)
    return detalle


def get_rifa_activa(force: bool = False) -> dict:
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
    if boletas is None:
        return
    pendientes = boletas.count_documents({"rifa_id": {"$exists": False}})
    if pendientes:
        boletas.update_many({"rifa_id": {"$exists": False}}, {"$set": {"rifa_id": rifa_id}})


def require_collections() -> None:
    required = [boletas, configuracion, facturas, rifas, vendedores]
    if any(collection is None for collection in required):
        raise RuntimeError("No hay conexión activa a MongoDB.")


def get_config(force: bool = False) -> dict:
    if not force and CONFIG_CACHE["data"] and time.monotonic() - CONFIG_CACHE["loaded_at"] < CONFIG_CACHE_SECONDS:
        return copy.deepcopy(CONFIG_CACHE["data"])

    rifa = get_rifa_activa(force)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update({
        "nombre_rifa": rifa.get("nombre", DEFAULT_CONFIG["nombre_rifa"]),
        "valor_boleta": int(rifa.get("valor_boleta", DEFAULT_CONFIG["valor_boleta"])),
        "cantidad_boletas": int(rifa.get("cantidad_boletas", 10000)),
        "premio_mayor": rifa.get("premio_mayor", ""),
        "estado": rifa.get("estado", "activa"),
        "comisiones_tiers": rifa.get("comisiones_tiers", COMISION_DEFAULT_TIERS),
        "rifa_id": rifa.get("_id"),
    })

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
            if k in stored and stored[k]:
                config[k] = stored[k]

    CONFIG_CACHE["data"] = copy.deepcopy(config)
    CONFIG_CACHE["loaded_at"] = time.monotonic()
    return config


def normalize_vendedor_id(value: str) -> str:
    vendedor_id = re.sub(r"\s+", "_", value.strip().upper())
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", vendedor_id):
        raise ValueError("El ID del vendedor debe tener 2 a 32 caracteres: letras, números, guion o guion bajo.")
    return vendedor_id




def calc_comision_por_boleta(vendidas: int, tiers: list[dict] | None = None) -> int:
    if tiers is None:
        config = get_config()
        tiers = config.get("comisiones_tiers", COMISION_DEFAULT_TIERS)
    tiers_sorted = sorted(tiers, key=lambda t: t["min"], reverse=True)
    for tier in tiers_sorted:
        if vendidas >= tier["min"]:
            return int(tier["valor"])
    return 0





def get_alertas():
    try:
        return []
    except Exception:
        return []





def get_vendedor_options() -> list[dict]:
    require_collections()
    cursor = vendedores.find({}, {"nombre": 1}).sort("_id", 1)
    return [{"_id": doc["_id"], "nombre": doc.get("nombre", "")} for doc in cursor]


def existing_boleta_ids(boleta_ids: list[int]) -> list[int]:
    if not boleta_ids:
        return []

    cursor = boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1})
    existing = {doc["_id"] for doc in cursor}
    return [boleta_id for boleta_id in boleta_ids if boleta_id in existing]


def get_dashboard_counts(rifa_id: str | None = None, valor_boleta: int | None = None) -> dict:
    require_collections()
    match = {}
    if rifa_id:
        match["rifa_id"] = rifa_id
    pipeline = [{"$match": match}] if match else []
    pipeline.extend([
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
                "abonando": {"$sum": {"$cond": [
                    {"$and": [
                        {"$gt": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                        {"$lt": [{"$ifNull": ["$total_abonado", 0]}, valor_boleta]},
                    ]},
                    1, 0,
                ]}},
                "separadas": {"$sum": {"$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                        {"$eq": [{"$ifNull": ["$vendedor_id", ""]}, VENDEDOR_LOCAL]},
                    ]},
                    1, 0,
                ]}},
                "asignadas": {"$sum": {"$cond": [
                    {"$and": [
                        {"$eq": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                        {"$not": {"$in": [{"$ifNull": ["$vendedor_id", ""]}, ["", None, VENDEDOR_LOCAL]]}},
                    ]},
                    1, 0,
                ]}},
            }
        }
    ])
    stats = first_aggregate(boletas, pipeline, {})
    total = int(stats.get("total", 0) or 0)
    pagadas = int(stats.get("pagadas", 0) or 0)
    abonando = int(stats.get("abonando", 0) or 0)
    separadas = int(stats.get("separadas", 0) or 0)
    asignadas = int(stats.get("asignadas", 0) or 0)
    disponibles = total - pagadas - abonando - separadas - asignadas
    return {
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


def first_aggregate(collection, pipeline, default=None):
    return next(collection.aggregate(pipeline), None) or default or {}


def get_dashboard_stats(force: bool = False) -> dict:
    if not force and DASHBOARD_CACHE["data"] and time.monotonic() - DASHBOARD_CACHE["loaded_at"] < DASHBOARD_CACHE_SECONDS:
        return DASHBOARD_CACHE["data"].copy()
    require_collections()
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    rifa_id = config.get("rifa_id")
    today = now_local().date().isoformat()
    counts = get_dashboard_counts(rifa_id, valor_boleta)
    total_boletas = int(counts.get("total", 0) or 0)
    vendidas = int(counts.get("vendidas", 0) or 0)

    match_rifa = [{"$match": {"rifa_id": rifa_id}}] if rifa_id else []

    today_totals = first_aggregate(
        boletas,
        match_rifa + [
            {"$match": {"historial_pagos.fecha": today}},
            {"$unwind": {"path": "$historial_pagos", "preserveNullAndEmptyArrays": False}},
            {"$match": {"historial_pagos.fecha": today}},
            {"$group": {"_id": None, "recaudo_hoy": {"$sum": "$historial_pagos.valor"}, "pagos_hoy": {"$sum": 1}}},
        ],
        {"recaudo_hoy": 0, "pagos_hoy": 0},
    )

    pagos_por_metodo = list(boletas.aggregate(
        match_rifa + [
            {"$unwind": {"path": "$historial_pagos", "preserveNullAndEmptyArrays": False}},
            {"$group": {"_id": "$historial_pagos.metodo", "total": {"$sum": "$historial_pagos.valor"}}},
        ],
    ))
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
            item["nombre"] = nombres.get(item["_id"], "Oficina local" if item["_id"] == VENDEDOR_LOCAL else "Sin registrar" if item["_id"] in ("", None) else "")

    recaudo_potencial = total_boletas * valor_boleta

    result = {
        **counts,
        "recaudo_hoy": today_totals.get("recaudo_hoy", 0),
        "pagos_hoy": today_totals.get("pagos_hoy", 0),
        "ranking": ranking,
        "valor_boleta": valor_boleta,
        "recaudo_potencial": recaudo_potencial,
        "progreso_ventas_pct": round((vendidas / total_boletas) * 100, 1) if total_boletas else 0,
        "progreso_recaudo_pct": round((counts["recaudo_total"] / recaudo_potencial) * 100, 1) if recaudo_potencial else 0,
        "pagos_efectivo": pagos_efectivo,
        "pagos_transferencia": pagos_transferencia,
        "pagos_otros": pagos_otros,
    }
    DASHBOARD_CACHE["data"] = result
    DASHBOARD_CACHE["loaded_at"] = time.monotonic()
    return result


def get_vendedores_snapshot(config=None):
    require_collections()
    config = config or get_config()
    valor_boleta = int(config["valor_boleta"])
    rifa_id = config.get("rifa_id")
    match = [{"$match": {"rifa_id": rifa_id}}] if rifa_id else []

    stats_docs = list(
        boletas.aggregate(
            match + [
                {
                    "$group": {
                        "_id": "$vendedor_id",
                        "boletas_en_sistema": {"$sum": 1},
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
                }
            ]
        )
    )
    stats_by_vendor = {doc["_id"]: doc for doc in stats_docs}

    lista = []
    total_asignadas = 0
    total_recaudado = 0
    total_comision = 0

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
            }
        )

    return lista, {
        "total_asignadas": total_asignadas,
        "total_recaudado": total_recaudado,
        "total_comision": total_comision,
        "total_vendedores": len(lista),
    }


def build_consulta_context(args: dict) -> dict:
    filters = {
        "numero": args.get("numero", args.get("buscar_numero", "")).strip(),
        "desde": args.get("desde", "").strip(),
        "hasta": args.get("hasta", "").strip(),
        "estado": args.get("estado", "").strip(),
        "vendedor_id": args.get("vendedor_id", "").strip(),
        "cliente": args.get("cliente", "").strip(),
        "telefono": args.get("telefono", "").strip(),
        "pago_metodo": args.get("pago_metodo", "").strip(),
        "referencia": args.get("referencia", "").strip(),
        "cliente_estado": args.get("cliente_estado", "").strip(),
        "abono_estado": args.get("abono_estado", "").strip(),
        "abonado_op": args.get("abonado_op", "").strip(),
        "abonado_valor": args.get("abonado_valor", "").strip(),

        "saldo_estado": args.get("saldo_estado", "").strip(),
        "limite": args.get("limite", str(CONSULTA_LIMIT_DEFAULT)).strip(),
    }

    errors = []
    query = {}
    numero_query, numero_exacto = ticket_number_query(filters["numero"], errors)
    desde = parse_int_filter(filters["desde"], "Desde", errors, BOLETA_MIN, BOLETA_MAX)
    hasta = parse_int_filter(filters["hasta"], "Hasta", errors, BOLETA_MIN, BOLETA_MAX)
    limite = parse_int_filter(filters["limite"], "Límite", errors, 1, CONSULTA_LIMIT_MAX) or CONSULTA_LIMIT_DEFAULT

    if filters["estado"]:
        if filters["estado"] in ESTADOS_BOLETA:
            query["estado"] = filters["estado"]
        else:
            errors.append("Estado inválido.")

    if filters["vendedor_id"]:
        if filters["vendedor_id"] == "__":
            query["vendedor_id"] = {"$in": ["", None]}
        else:
            query["vendedor_id"] = filters["vendedor_id"]

    if numero_query is not None and not errors:
        query["_id"] = numero_query
    else:
        range_query = {}
        if desde is not None:
            range_query["$gte"] = desde
        if hasta is not None:
            range_query["$lte"] = hasta
        if desde is not None and hasta is not None and desde > hasta:
            errors.append("Desde no puede ser mayor que Hasta.")
        if range_query:
            query["_id"] = range_query

    if filters["cliente"]:
        query["cliente.nombre"] = {"$regex": re.escape(filters["cliente"]), "$options": "i"}

    if filters["telefono"]:
        query["cliente.telefono"] = {"$regex": re.escape(filters["telefono"])}

    if filters["pago_metodo"]:
        if filters["pago_metodo"] in METODOS_PAGO:
            query["historial_pagos.metodo"] = filters["pago_metodo"]
        else:
            errors.append("Método de pago inválido.")

    if filters["referencia"]:
        query["historial_pagos.referencia"] = {"$regex": re.escape(filters["referencia"]), "$options": "i"}

    if filters["cliente"] and filters["cliente_estado"] == "sin_cliente":
        errors.append("Nombre de cliente y filtro 'Sin cliente' son incompatibles.")
    elif filters["cliente_estado"] == "con_cliente":
        if isinstance(query.get("cliente.nombre"), dict):
            query["cliente.nombre"]["$ne"] = ""
        else:
            query["cliente.nombre"] = {"$ne": ""}
    elif filters["cliente_estado"] == "sin_cliente":
        if "cliente.nombre" in query:
            query.setdefault("$and", []).append({"cliente.nombre": query.pop("cliente.nombre")})
        query["cliente.nombre"] = ""
    elif filters["cliente_estado"]:
        errors.append("Filtro de cliente inválido.")

    if filters["abono_estado"] == "con_abono":
        query["total_abonado"] = {"$gt": 0}
    elif filters["abono_estado"] == "sin_abono":
        query["total_abonado"] = 0
    elif filters["abono_estado"]:
        errors.append("Filtro de abono inválido.")

    if filters["saldo_estado"] == "pendiente":
        query.setdefault("$and", []).append({"total_abonado": {"$gt": 0}})
        query.setdefault("$and", []).append({"estado": {"$ne": "pagada"}})
    elif filters["saldo_estado"] == "sin_saldo":
        query.setdefault("$and", []).append({"estado": "pagada"})
    elif filters["saldo_estado"]:
        errors.append("Filtro de saldo inválido.")

    abonado_op = filters["abonado_op"]
    abonado_valor_raw = filters["abonado_valor"]
    if abonado_valor_raw:
        if abonado_op not in ("gte", "lte", "eq"):
            errors.append("Operador de abonado inválido.")
        else:
            abonado_valor = parse_money(abonado_valor_raw)
            if abonado_valor is not None and abonado_valor >= 0:
                query["total_abonado"] = {ABONADO_OP_MAP[abonado_op]: abonado_valor}
            else:
                errors.append("Valor de abonado inválido.")

    if filters["estado"]:
        if filters["estado"] == "disponible":
            if filters["abono_estado"] == "con_abono":
                errors.append("Estado 'Disponible' es incompatible con 'Con abono' (disponible = sin pagos).")
            if filters["saldo_estado"] == "pendiente":
                errors.append("Estado 'Disponible' no puede tener saldo pendiente (sin pagos).")
            if filters["vendedor_id"] and filters["vendedor_id"] not in ("", "__"):
                errors.append("Estado 'Disponible' es incompatible con vendedor asignado.")
        elif filters["estado"] == "separada":
            if filters["abono_estado"] == "con_abono":
                errors.append("Estado 'Separada' es incompatible con 'Con abono' (separada = compra local sin pago).")
            if filters["saldo_estado"] == "pendiente":
                errors.append("Estado 'Separada' no puede tener saldo pendiente (sin pagos).")
            if filters["vendedor_id"] and filters["vendedor_id"] not in ("", VENDEDOR_LOCAL):
                errors.append("Estado 'Separada' solo es compatible con vendedor LOCAL.")
        elif filters["estado"] == "asignada":
            if filters["abono_estado"] == "con_abono":
                errors.append("Estado 'Asignada' es incompatible con 'Con abono' (asignada = boletas sin pagar).")
            if filters["saldo_estado"] == "pendiente":
                errors.append("Estado 'Asignada' no puede tener saldo pendiente (sin pagos).")
            if filters["vendedor_id"] == "__":
                errors.append("Estado 'Asignada' es incompatible con 'Sin asignar'.")
            if filters["vendedor_id"] == VENDEDOR_LOCAL:
                errors.append("Estado 'Asignada' es incompatible con vendedor LOCAL.")
        elif filters["estado"] == "abonando":
            if filters["abono_estado"] == "sin_abono":
                errors.append("Estado 'Abonando' requiere 'Con abono'.")
            if filters["saldo_estado"] == "sin_saldo":
                errors.append("Estado 'Abonando' tiene saldo pendiente, no puede ser 'Sin saldo'.")
        elif filters["estado"] == "pagada":
            if filters["abono_estado"] == "sin_abono":
                errors.append("Estado 'Pagada' requiere 'Con abono'.")
            if filters["saldo_estado"] == "pendiente":
                errors.append("Estado 'Pagada' no tiene saldo pendiente.")

    page = parse_int_filter(args.get("page", "1").strip(), "Página", errors, 1, None) or 1
    offset = (page - 1) * limite
    has_filters = any(value is not None and value != "" for key, value in filters.items() if key != "limite")
    return filters, query, errors, page, limite, offset, has_filters, numero_exacto


def build_page_url(endpoint: str, filters: dict, page: int) -> str:
    params = {key: value for key, value in filters.items() if value is not None and value != ""}
    params["page"] = page
    return url_for(endpoint, **params)


def validar_form_abono(form):
    form_data = {
        "valor": form.get("valor", "").strip(),
        "fecha": form.get("fecha", "").strip() or now_local().date().isoformat(),
        "metodo": form.get("metodo", "").strip().lower() or METODO_EFECTIVO,
        "referencia": form.get("referencia", "").strip(),
        "banco": form.get("banco", "").strip(),
        "boletas": form.get("boletas", "").strip(),
    }
    errors = []

    valor_abono = parse_money(form_data["valor"])
    if valor_abono <= 0:
        errors.append("El valor del abono debe ser mayor que cero.")

    try:
        datetime.strptime(form_data["fecha"], "%Y-%m-%d")
    except ValueError:
        errors.append("La fecha debe tener formato AAAA-MM-DD.")

    if form_data["metodo"] not in METODOS_PAGO:
        errors.append("Selecciona un método de pago válido.")

    if form_data["metodo"] == METODO_TRANSFERENCIA and not form_data["referencia"]:
        errors.append("La referencia bancaria es obligatoria para transferencias.")

    boleta_ids, invalid, out_of_range, duplicadas = parse_boletas_detailed(form_data["boletas"])
    if invalid:
        errors.append("Hay entradas no numéricas: " + ", ".join(invalid[:8]))
    if out_of_range:
        errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
    if not boleta_ids:
        errors.append("Ingresa al menos una boleta válida.")

    return form_data, valor_abono, boleta_ids, duplicadas, errors


def build_abono_preview(form: dict, factura_id: int | None = None) -> dict:
    require_collections()
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    form_data, valor_abono, boleta_ids, duplicadas, errors = validar_form_abono(form)
    preview = {
        "validas": [],
        "inexistentes": [],
        "pagadas": [],
        "duplicadas": duplicadas,
        "referencias_usadas": [],
        "warnings": [],
        "errors": errors,
        "can_confirm": False,
        "valor_abono": valor_abono,
    }

    if errors:
        return form_data, preview

    docs = list(
        boletas.find(
            {"_id": {"$in": boleta_ids}},
            {"_id": 1, "estado": 1, "total_abonado": 1, "vendedor_id": 1, "cliente": 1},
        )
    )
    docs_by_id = {doc["_id"]: doc for doc in docs}
    preview["inexistentes"] = [number for number in boleta_ids if number not in docs_by_id]

    if form_data["metodo"] == METODO_TRANSFERENCIA:
        ref = form_data["referencia"]
        elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": ref}
        banco = form_data.get("banco", "").strip()
        if banco:
            elem_match["banco"] = banco
        if factura_id is not None:
            elem_match["factura_id"] = {"$ne": factura_id}
        used_refs = list(
            boletas.find(
                {"historial_pagos": {"$elemMatch": elem_match}},
                {"_id": 1},
            ).limit(10)
        )
        preview["referencias_usadas"] = [doc["_id"] for doc in used_refs]
        if used_refs:
            preview["errors"].append("La referencia bancaria ya existe en otro pago.")

    for number in boleta_ids:
        doc = docs_by_id.get(number)
        if not doc:
            continue
        if doc.get("estado") == "pagada":
            preview["pagadas"].append(doc)
            continue

        nuevo_total = int(doc.get("total_abonado", 0) or 0) + valor_abono
        if nuevo_total > valor_boleta:
            preview["excesos"] = preview.get("excesos", []) + [doc]
            continue
        doc["nuevo_total"] = nuevo_total
        doc["nuevo_estado"] = estado_para_total(nuevo_total, valor_boleta)
        preview["validas"].append(doc)

    if duplicadas:
        preview["warnings"].append("Se ignorarán números duplicados del bloque.")
    if preview["inexistentes"]:
        preview["warnings"].append("Las boletas inexistentes no serán modificadas.")
    if preview["pagadas"]:
        preview["warnings"].append("Las boletas ya pagadas se omitirán.")

    if preview.get("excesos"):
        preview["warnings"].append(f"Se omitieron {len(preview['excesos'])} boleta(s) porque el abono supera su saldo pendiente.")
    if not preview["validas"]:
        preview["errors"].append("No hay boletas disponibles para registrar este abono.")

    preview["can_confirm"] = bool(preview["validas"]) and not preview["errors"]
    return form_data, preview


def registrar_abono_lote(boleta_ids: list[int], form_data: dict, valor_abono: int, factura_id: int | None = None) -> dict:
    config = get_config()
    valor_boleta = int(config["valor_boleta"])

    if form_data["metodo"] == METODO_TRANSFERENCIA:
        ref = form_data.get("referencia", "").strip()
        if not ref:
            raise ValueError("La referencia bancaria es obligatoria para transferencias.")
        banco = form_data.get("banco", "").strip()
        duplicado = _buscar_transferencia_duplicada(ref, banco, exclude_factura_id=factura_id)
        if duplicado:
            msg = f"Ya existe un pago por transferencia con referencia {ref}"
            if banco:
                msg += f" y banco {banco}"
            msg += f" (boleta #{duplicado['_id']:04d})."
            raise ValueError(msg)
        pago = {
            "fecha": form_data["fecha"],
            "valor": valor_abono,
            "metodo": METODO_TRANSFERENCIA,
            "referencia": ref,
            "registrado_en": now_local(),
            "usuario": (current_user() or {}).get("username", USUARIO_SISTEMA),
        }
        if banco:
            pago["banco"] = banco
    else:
        pago = {
            "fecha": form_data["fecha"],
            "valor": valor_abono,
            "metodo": METODO_EFECTIVO,
            "registrado_en": now_local(),
            "usuario": (current_user() or {}).get("username", USUARIO_SISTEMA),
        }
    if factura_id is not None:
        pago["factura_id"] = factura_id
    if valor_abono > 0:
        sobrepasan = boletas.count_documents({
            "_id": {"$in": boleta_ids},
            "total_abonado": {"$gt": valor_boleta - valor_abono},
        })
        if sobrepasan:
            raise ValueError(f"El abono de ${valor_abono:,} excede el saldo pendiente de {sobrepasan} boleta(s).")
    result = boletas.update_many(
        {"_id": {"$in": boleta_ids}, "estado": {"$ne": "pagada"}},
        [
            {
                "$set": {
                    "historial_pagos": {"$concatArrays": [{"$ifNull": ["$historial_pagos", []]}, {"$literal": [pago]}]},
                    "total_abonado": {"$add": [{"$ifNull": ["$total_abonado", 0]}, valor_abono]},
                }
            },
            {
                "$set": {
                    "estado": estado_pipeline_expr(valor_boleta)
                }
            },
        ],
    )
    if result.modified_count < len(boleta_ids):
        skipped = len(boleta_ids) - result.modified_count
        current_app.logger.warning(
            "registrar_abono_lote: %d boleta(s) no se actualizaron "
            "(probablemente ya estaban pagadas)", skipped
        )
    invalidate_dashboard_cache()
    return result


def rollback_pagos_por_factura(factura_id: int, valor_boleta: int) -> None:
    pipeline = [
        {
            "$set": {
                "historial_pagos": {
                    "$filter": {
                        "input": {"$ifNull": ["$historial_pagos", []]},
                        "cond": {"$ne": ["$$this.factura_id", factura_id]},
                    }
                }
            }
        },
        {
            "$set": {
                "total_abonado": {
                    "$reduce": {
                        "input": {"$ifNull": ["$historial_pagos", []]},
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.valor"]},
                    }
                }
            }
        },
    ]
    if valor_boleta is not None:
        pipeline.append({"$set": {"estado": estado_pipeline_expr(valor_boleta)}})
    boletas.update_many(
        {"historial_pagos.factura_id": factura_id},
        pipeline,
    )
    invalidate_dashboard_cache()


def safe_vendedores_snapshot() -> list[dict]:
    try:
        return get_vendedores_snapshot()
    except Exception as exc:
        flash(f"No se pudo cargar el listado de vendedores: {exc}", "danger")
        return [], {"total_asignadas": 0, "total_recaudado": 0, "total_comision": 0, "total_vendedores": 0}


def next_factura_id() -> int:
    result = configuracion.find_one_and_update(
        {"_id": "rifa"},
        {"$inc": {"factura_counter": 1}},
        upsert=True,
        return_document=True,
    )
    return result["factura_counter"] if result else 1



def vendedor_label(vendedor_id, nombres_vendedores):
    if not vendedor_id:
        return "SIN REGISTRAR"
    nombre = nombres_vendedores.get(vendedor_id, "")
    if vendedor_id == VENDEDOR_LOCAL or nombre == VENDEDOR_LOCAL:
        return "VEND. LOCAL"
    return f"VEND. {nombre or vendedor_id}".upper()


def compact_model_payments(payments, slots):
    payments = [payment for payment in payments if int(payment.get("valor", 0) or 0) > 0]
    if len(payments) <= slots:
        return payments

    head = payments[: slots - 1]
    tail = payments[slots - 1 :]
    head.append(
        {
            "fecha": tail[-1].get("fecha", ""),
            "facturero": "VARIOS",
            "valor": sum(int(payment.get("valor", 0) or 0) for payment in tail),
            "metodo": tail[-1].get("metodo", ""),
            "referencia": "VARIOS",
        }
    )
    return head


def append_model_payment_slots(row, payments, slots):
    compacted = compact_model_payments(payments, slots)
    for index in range(slots):
        if index < len(compacted):
            payment = compacted[index]
            row.extend([payment.get("fecha", ""), payment.get("facturero", ""), int(payment.get("valor", 0) or 0)])
        else:
            row.extend(["", "", ""])


def modelo_rifa_report_rows():
    nombres_vendedores = {doc["_id"]: doc.get("nombre", "") for doc in vendedores.find({}, {"nombre": 1})}
    rows = []
    for doc in boletas.find({}).sort("_id", 1):
        cliente = doc.get("cliente") or {}
        historial = doc.get("historial_pagos") or []
        efectivo = [payment for payment in historial if payment.get("metodo") != METODO_TRANSFERENCIA]
        transferencias = [payment for payment in historial if payment.get("metodo") == METODO_TRANSFERENCIA]
        total_efectivo = sum(int(payment.get("valor", 0) or 0) for payment in efectivo)
        total_transferencias = sum(int(payment.get("valor", 0) or 0) for payment in transferencias)
        total_abonado = int(doc.get("total_abonado", 0) or 0)

        row = [
            f"{doc['_id']:04d}",
            total_abonado,
            doc.get("fecha_adquisicion", ""),
            vendedor_label(doc.get("vendedor_id", VENDEDOR_LOCAL), nombres_vendedores),
            cliente.get("nombre", ""),
            cliente.get("direccion", ""),
            cliente.get("telefono", ""),
        ]
        append_model_payment_slots(row, efectivo, 7)
        row.extend([total_efectivo, ""])
        append_model_payment_slots(row, transferencias, 5)
        row.extend([total_transferencias, total_abonado])
        rows.append(row)
    return MODELO_RIFA_HEADERS, rows


def vendor_from_excel(value):
    raw = re.sub(r"\s+", " ", clean_excel_text(value)).strip()
    if not raw:
        return VENDEDOR_LOCAL, VENDEDOR_LOCAL
    nombre = re.sub(r"^VEND\.?\s*", "", raw, flags=re.IGNORECASE).strip() or raw

    ascii_name = unicode_normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii")
    vendedor_id = re.sub(r"[^A-Z0-9]+", "_", ascii_name.upper()).strip("_")
    vendedor_id = vendedor_id[:32].strip("_") or VENDEDOR_LOCAL
    return vendedor_id, nombre.upper()


def is_assignable_vendor_cell(value):
    raw = re.sub(r"\s+", " ", clean_excel_text(value)).strip()
    if not raw:
        return False
    nombre = re.sub(r"^VEND\.?\s*", "", raw, flags=re.IGNORECASE).strip()
    if not nombre:
        return False
    upper = nombre.upper()
    if upper == VENDEDOR_LOCAL:
        return False
    if upper.startswith("CAMION") or upper.startswith("CAMI\u00d3N"):
        return False
    if upper.startswith("PAQUETE"):
        return False
    return True


def read_xlsx_first_sheet_rows(file_obj):
    data = file_obj.read()
    with zipfile.ZipFile(io.BytesIO(data)) as workbook_zip:
        shared_strings = []
        if "xl/sharedStrings.xml" in workbook_zip.namelist():
            shared_root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("main:si", XLSX_NS):
                shared_strings.append("".join(node.text or "" for node in item.iterfind(".//main:t", XLSX_NS)))

        workbook_root = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
        rels_root = ET.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
        rels = {rel.attrib["Id"]: rel.attrib["Target"].replace("/xl/", "") for rel in rels_root.findall("rel:Relationship", XLSX_REL_NS)}
        first_sheet = workbook_root.find("main:sheets/main:sheet", XLSX_NS)
        if first_sheet is None:
            raise ValueError("El archivo no contiene hojas.")

        rel_id = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        sheet_path = "xl/" + rels[rel_id].lstrip("/")
        sheet_root = ET.fromstring(workbook_zip.read(sheet_path))

        rows = []
        for row in sheet_root.findall("main:sheetData/main:row", XLSX_NS):
            values = []
            for cell in row.findall("main:c", XLSX_NS):
                ref = cell.attrib.get("r", "")
                match = re.match(r"([A-Z]+)", ref)
                if not match:
                    continue
                index = col_to_index(match.group(1))
                while len(values) <= index:
                    values.append("")

                if cell.attrib.get("t") == "inlineStr":
                    values[index] = "".join(node.text or "" for node in cell.iterfind(".//main:t", XLSX_NS))
                    continue

                value_node = cell.find("main:v", XLSX_NS)
                value = "" if value_node is None or value_node.text is None else value_node.text
                if cell.attrib.get("t") == "s" and value != "":
                    value = shared_strings[int(value)]
                values[index] = value
            rows.append(values)
        return rows


def row_value(row, index):
    return row[index] if index < len(row) else ""


def parse_asignaciones_vendedores_xlsx(file_obj):
    rows = read_xlsx_first_sheet_rows(file_obj)
    if not rows:
        raise ValueError("El archivo está vacío.")

    headers = [clean_excel_text(value).upper() for value in rows[0]]
    normalized_headers = {header.strip() for header in headers}
    missing = [header for header in {"NUMERO DE BOLETA", "VENDEDOR (A)"} if header not in normalized_headers]
    if missing:
        raise ValueError("El archivo no parece ser el modelo esperado. Faltan columnas: " + ", ".join(missing))

    vendor_assignments = defaultdict(list)
    vendor_names = {}
    invalid_rows = []
    ignored_local = 0
    ignored_camion = 0
    ignored_paquete = 0
    empty_vendor = 0

    for excel_row_number, row in enumerate(rows[1:], start=2):
        numero = parse_excel_boleta(row_value(row, 0))
        if numero is None:
            if any(clean_excel_text(value) for value in row):
                invalid_rows.append(excel_row_number)
            continue

        vendedor_cell = row_value(row, 3)
        if not clean_excel_text(vendedor_cell):
            empty_vendor += 1
            continue
        if not is_assignable_vendor_cell(vendedor_cell):
            raw_v = re.sub(r"\s+", " ", clean_excel_text(vendedor_cell)).strip()
            nombre_v = re.sub(r"^VEND\.?\s*", "", raw_v, flags=re.IGNORECASE).strip().upper()
            if nombre_v.startswith("PAQUETE"):
                ignored_paquete += 1
            elif nombre_v.startswith("CAMION") or nombre_v.startswith("CAMI\u00d3N"):
                ignored_camion += 1
            else:
                ignored_local += 1
            continue

        vendedor_id, vendedor_nombre = vendor_from_excel(vendedor_cell)
        vendor_assignments[vendedor_id].append(numero)
        vendor_names[vendedor_id] = vendedor_nombre

    boleta_to_vendors = defaultdict(set)
    for v_id, ids_list in vendor_assignments.items():
        for num in ids_list:
            boleta_to_vendors[num].add(v_id)
    duplicates = {num: sorted(v_ids) for num, v_ids in boleta_to_vendors.items() if len(v_ids) > 1}
    if duplicates:
        dup_msgs = [f"#{num:04d}: {', '.join(v_ids)}" for num, v_ids in sorted(duplicates.items())[:5]]
        raise ValueError(f"Boletas asignadas a m\u00faltiples vendedores: {'; '.join(dup_msgs)}")

    return vendor_assignments, vendor_names, {
        "boletas_asignadas": sum(len(set(ids)) for ids in vendor_assignments.values()),
        "vendedores": len(vendor_assignments),
        "local_ignoradas": ignored_local,
        "camion_ignoradas": ignored_camion,
        "paquete_ignoradas": ignored_paquete,
        "sin_vendedor": empty_vendor,
        "invalid_rows": invalid_rows[:20],
    }


def importar_modelo_rifa(file_obj):
    require_collections()
    config = get_config()
    valor_boleta = int(config.get("valor_boleta", 10000) or 10000)
    vendor_assignments, vendor_names, summary = parse_asignaciones_vendedores_xlsx(file_obj)

    assigned_ids = sorted({number for ids in vendor_assignments.values() for number in ids})
    for vendedor_id, ids in vendor_assignments.items():
        unique_ids = sorted(set(ids))
        if unique_ids:
            boletas.update_many(
                {"_id": {"$in": unique_ids}},
                {"$set": {"vendedor_id": vendedor_id}},
            )
            boletas.update_many(
                {"_id": {"$in": unique_ids}},
                [{"$set": {
                    "estado": estado_pipeline_expr(valor_boleta)
                }}],
            )

    vendor_ops = []
    for vendedor_id, assigned in vendor_assignments.items():
        vendor_ops.append(
            UpdateOne(
                {"_id": vendedor_id},
                {
                    "$set": {
                        "nombre": vendor_names.get(vendedor_id, vendedor_id),
                    },
                    "$addToSet": {"boletas_asignadas": {"$each": sorted(set(assigned))}},
                    "$setOnInsert": {"telefono": ""},
                },
                upsert=True,
            )
        )
    if vendor_ops:
        vendedores.bulk_write(vendor_ops, ordered=False)

    summary["boletas_actualizadas"] = len(assigned_ids)
    summary["boletas_locales_omitidas"] = 0

    sync_ticket_statuses(valor_boleta)
    invalidate_dashboard_cache()

    return summary


def crear_indices_boletas():
    boletas.create_index([("vendedor_id", 1), ("_id", 1)])
    boletas.create_index([("vendedor_id", 1), ("estado", 1)])
    boletas.create_index([("estado", 1), ("_id", 1)])
    boletas.create_index([("total_abonado", 1), ("_id", 1)])
    boletas.create_index([("historial_pagos.fecha", 1)])
    boletas.create_index("cliente.telefono")
    boletas.create_index("cliente.nombre")
    boletas.create_index("historial_pagos.metodo")
    boletas.create_index("historial_pagos.referencia")
    vendedores.create_index("telefono")
    facturas.create_index([("fecha", -1)])
    facturas.create_index("tipo")
    rifas.create_index("estado")


def crear_nueva_rifa(nombre, valor_boleta, conservar_vendedores,
                     cantidad_boletas=10000, premio_mayor="",
                     estado="activa"):
    require_collections()
    asignaciones = []
    if conservar_vendedores:
        asignaciones = list(vendedores.find({}, {"boletas_asignadas": 1}))

    facturas.delete_many({})
    configuracion.update_one({"_id": CONFIG_ID}, {"$set": {"factura_counter": 0}})

    boletas.delete_many({})
    boletas.insert_many([crear_boleta_base(numero) for numero in range(BOLETA_MIN, BOLETA_MAX + 1)])

    if conservar_vendedores:
        for vendedor in asignaciones:
            ids = [
                number
                for number in vendedor.get("boletas_asignadas", [])
                if isinstance(number, int) and BOLETA_MIN <= number <= BOLETA_MAX
            ]
            if ids:
                boletas.update_many({"_id": {"$in": ids}}, {"$set": {"vendedor_id": vendedor["_id"], "estado": "asignada"}})
    else:
        vendedores.delete_many({})

    crear_indices_boletas()

    rifas.delete_many({})
    rifa_doc = {
        "nombre": nombre,
        "anio": now_local().year,
        "valor_boleta": valor_boleta,
        "cantidad_boletas": cantidad_boletas,
        "premio_mayor": premio_mayor,
        "comisiones_tiers": COMISION_DEFAULT_TIERS,
        "estado": estado,
        "creada_en": now_local(),
    }
    rifas.insert_one(rifa_doc)

    update = {
        "nombre_rifa": nombre,
        "valor_boleta": valor_boleta,
        "cantidad_boletas": cantidad_boletas,
        "premio_mayor": premio_mayor,
        "estado": estado,
        "creada_en": now_local(),
    }
    configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
    invalidate_config_cache()
    invalidate_dashboard_cache()


def register_template_filters(app):
    @app.template_filter("cop")
    def format_cop(value):
        try:
            amount = int(value or 0)
        except (TypeError, ValueError):
            amount = 0

        return f"${amount:,}".replace(",", ".")

    @app.template_filter("pct")
    def format_pct(value):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0
        return f"{number:.2f}".rstrip("0").rstrip(".")


def register_before_request(app):
    @app.before_request
    def load_user_context():
        g.config = get_config()
        g.current_user = current_user()


def register_context_processor(app):
    @app.context_processor
    def inject_globals():
        return {
            "app_config": getattr(g, "config", get_config()),
            "current_user": getattr(g, "current_user", current_user()),
            "can": has_role,
            "alertas": get_alertas,
        }

import csv
import io
import os
import re
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from html import escape
from unicodedata import normalize as unicode_normalize
from xml.etree import ElementTree as ET

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pymongo import UpdateOne

from database import auditoria, boletas, configuracion, vendedores

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave_desarrollo_boleteria")

BOLETA_MIN = 0
BOLETA_MAX = 9999
METODOS_PAGO = {"efectivo", "transferencia"}
OPERACIONES_VENDEDOR = {"guardar", "asignar", "quitar", "reemplazar"}
ESTADOS_BOLETA = {"disponible", "abonado", "pagada"}
ROLES = {"admin", "cajero", "consulta"}
CONSULTA_LIMIT_DEFAULT = 50
CONSULTA_LIMIT_MAX = 200
CONFIG_ID = "rifa"
PUBLIC_ENDPOINTS = {"login", "static"}

DEFAULT_CONFIG = {
    "_id": CONFIG_ID,
    "nombre_rifa": os.getenv("NOMBRE_RIFA", "Rifa Principal"),
    "valor_boleta": int(os.getenv("VALOR_BOLETA", "100000")),
    "premios_adicionales": [],
}
CONFIG_CACHE = {"data": None, "loaded_at": 0}
CONFIG_CACHE_SECONDS = 8


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


def require_collections():
    required = [boletas, vendedores, configuracion, auditoria]
    if any(collection is None for collection in required):
        raise RuntimeError("No hay conexión activa a MongoDB.")


def invalidate_config_cache():
    CONFIG_CACHE["data"] = None
    CONFIG_CACHE["loaded_at"] = 0


def get_config(force=False):
    if not force and CONFIG_CACHE["data"] and time.monotonic() - CONFIG_CACHE["loaded_at"] < CONFIG_CACHE_SECONDS:
        return CONFIG_CACHE["data"].copy()

    config = DEFAULT_CONFIG.copy()
    if configuracion is None:
        return config

    try:
        stored = configuracion.find_one({"_id": CONFIG_ID})
        if not stored:
            configuracion.update_one({"_id": CONFIG_ID}, {"$setOnInsert": config}, upsert=True)
        else:
            config.update(stored)
    except Exception:
        return config

    CONFIG_CACHE["data"] = config.copy()
    CONFIG_CACHE["loaded_at"] = time.monotonic()
    return config.copy()


def normalize_vendedor_id(value):
    vendedor_id = re.sub(r"\s+", "_", value.strip().upper())
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", vendedor_id):
        raise ValueError("El ID del vendedor debe tener 2 a 32 caracteres: letras, números, guion o guion bajo.")
    return vendedor_id


def parse_int_filter(value, field_name, errors, min_value=None, max_value=None):
    if value == "":
        return None

    if not value.isdigit():
        errors.append(f"{field_name} debe ser numérico.")
        return None

    number = int(value)
    if min_value is not None and number < min_value:
        errors.append(f"{field_name} debe ser mayor o igual a {min_value}.")
    if max_value is not None and number > max_value:
        errors.append(f"{field_name} debe ser menor o igual a {max_value}.")
    return number


def ticket_number_query(value, errors):
    raw_value = (value or "").strip()
    if raw_value == "":
        return None, False

    if not raw_value.isdigit():
        errors.append("El número de boleta debe contener solo dígitos.")
        return None, False

    if len(raw_value) > 4:
        errors.append("El número de boleta debe tener máximo 4 dígitos.")
        return None, False

    if len(raw_value) == 4:
        number = int(raw_value)
        if number < BOLETA_MIN or number > BOLETA_MAX:
            errors.append("El número debe estar entre 0000 y 9999.")
            return None, False
        return number, True

    matches = [number for number in range(BOLETA_MIN, BOLETA_MAX + 1) if raw_value in f"{number:04d}"]
    if not matches:
        errors.append("No hay boletas que contengan esos dígitos.")
        return None, False

    return {"$in": matches}, False


def parse_money(value):
    cleaned = re.sub(r"[^\d]", "", value or "")
    return int(cleaned) if cleaned else 0


def parse_float(value, default=0):
    value = (value or "").strip().replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return default


def parse_percentage(value, default=0):
    text = (value or "").strip()
    if text == "":
        return float(default or 0), None

    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return float(default or 0), "La comisión debe ser numérica."

    if number < 0 or number > 100:
        return number, "La comisión debe estar entre 0 y 100."

    return number, None


def parse_boletas_detailed(raw_numbers):
    parts = [part for part in re.split(r"[\s,;]+", (raw_numbers or "").strip()) if part]
    invalid = [part for part in parts if not part.isdigit()]
    numbers = []
    out_of_range = []

    for part in parts:
        if not part.isdigit():
            continue

        number = int(part)
        if number < BOLETA_MIN or number > BOLETA_MAX:
            out_of_range.append(part)
            continue
        numbers.append(number)

    counts = Counter(numbers)
    duplicates = sorted(number for number, count in counts.items() if count > 1)
    unique_numbers = []
    seen = set()

    for number in numbers:
        if number not in seen:
            seen.add(number)
            unique_numbers.append(number)

    return unique_numbers, invalid, out_of_range, duplicates


def parse_boletas(raw_numbers):
    boleta_ids, invalid, out_of_range, _duplicates = parse_boletas_detailed(raw_numbers)
    return boleta_ids, invalid, out_of_range


def estado_para_total(total_abonado, valor_boleta):
    if total_abonado >= valor_boleta:
        return "pagada"
    if total_abonado > 0:
        return "abonado"
    return "disponible"


def sync_ticket_statuses(valor_boleta):
    boletas.update_many(
        {},
        [
            {
                "$set": {
                    "estado": {
                        "$switch": {
                            "branches": [
                                {
                                    "case": {"$gte": [{"$ifNull": ["$total_abonado", 0]}, valor_boleta]},
                                    "then": "pagada",
                                },
                                {
                                    "case": {"$gt": [{"$ifNull": ["$total_abonado", 0]}, 0]},
                                    "then": "abonado",
                                },
                            ],
                            "default": "disponible",
                        }
                    }
                }
            }
        ],
    )


def current_user():
    return {"username": "sistema", "role": "admin", "nombre": "Sistema"}


def has_role(*roles):
    return True


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def calc_comision_por_boleta(vendidas):
    if vendidas < 10:
        return 0
    if vendidas <= 20:
        return 10000
    if vendidas <= 50:
        return 15000
    return 20000


VALOR_MINIMO_PREMIO_ADICIONAL = 20000


def calcular_premios_adicionales(historial_pagos, premios_config):
    if not premios_config or not historial_pagos:
        return [{"nombre": p["nombre"], "fecha_juego": p["fecha_juego"], "participa": False} for p in (premios_config or [])]

    premios_ordenados = sorted(premios_config, key=lambda p: p["fecha_juego"])
    resultado = []

    for i, premio in enumerate(premios_ordenados):
        total_paid_before = sum(
            int(p.get("valor", 0) or 0)
            for p in historial_pagos
            if p.get("fecha", "9999-12-31") <= premio["fecha_juego"]
        )
        participa = total_paid_before >= VALOR_MINIMO_PREMIO_ADICIONAL * (i + 1)
        resultado.append({"nombre": premio["nombre"], "fecha_juego": premio["fecha_juego"], "participa": participa})

    return resultado


def log_action(action, entidad, entidad_id=None, detalles=None):
    if auditoria is None:
        return

    user = current_user() or {"username": "sistema", "role": "admin"}
    try:
        auditoria.insert_one(
            {
                "fecha": datetime.utcnow(),
                "accion": action,
                "entidad": entidad,
                "entidad_id": str(entidad_id) if entidad_id is not None else "",
                "usuario": user.get("username", "sistema"),
                "rol": user.get("role", "admin"),
                "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
                "detalles": detalles or {},
            }
        )
    except Exception:
        return


@app.before_request
def load_user_context():
    g.config = get_config()
    g.current_user = current_user()


@app.context_processor
def inject_globals():
    return {
        "app_config": getattr(g, "config", get_config()),
        "current_user": getattr(g, "current_user", current_user()),
        "can": has_role,
    }


def get_vendedor_options():
    require_collections()
    cursor = vendedores.find({}, {"nombre": 1}).sort("_id", 1)
    return [{"_id": doc["_id"], "nombre": doc.get("nombre", "")} for doc in cursor]


def existing_boleta_ids(boleta_ids):
    if not boleta_ids:
        return []

    cursor = boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1})
    existing = {doc["_id"] for doc in cursor}
    return [boleta_id for boleta_id in boleta_ids if boleta_id in existing]


def get_dashboard_counts():
    require_collections()
    stats = first_aggregate(
        boletas,
        [
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "disponibles": {"$sum": {"$cond": [{"$eq": ["$estado", "disponible"]}, 1, 0]}},
                    "abonadas": {"$sum": {"$cond": [{"$eq": ["$estado", "abonado"]}, 1, 0]}},
                    "pagadas": {"$sum": {"$cond": [{"$eq": ["$estado", "pagada"]}, 1, 0]}},
                    "asignadas": {"$sum": {"$cond": [{"$ne": ["$vendedor_id", "LOCAL"]}, 1, 0]}},
                }
            }
        ],
        {},
    )
    abonadas = int(stats.get("abonadas", 0) or 0)
    pagadas = int(stats.get("pagadas", 0) or 0)
    return {
        "total": int(stats.get("total", 0) or 0),
        "disponibles": int(stats.get("disponibles", 0) or 0),
        "abonadas": abonadas,
        "pagadas": pagadas,
        "vendidas": abonadas + pagadas,
        "asignadas": int(stats.get("asignadas", 0) or 0),
    }


def first_aggregate(collection, pipeline, default=None):
    docs = list(collection.aggregate(pipeline))
    return docs[0] if docs else (default or {})


def get_dashboard_stats():
    require_collections()
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    today = date.today().isoformat()
    counts = get_dashboard_counts()
    total_boletas = int(counts.get("total", 0) or 0)
    vendidas = int(counts.get("vendidas", 0) or 0)

    totals = first_aggregate(
        boletas,
        [
            {
                "$group": {
                    "_id": None,
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
                }
            }
        ],
        {"recaudo_total": 0, "saldo_pendiente": 0},
    )

    today_totals = first_aggregate(
        boletas,
        [
            {"$unwind": "$historial_pagos"},
            {"$match": {"historial_pagos.fecha": today}},
            {"$group": {"_id": None, "recaudo_hoy": {"$sum": "$historial_pagos.valor"}, "pagos_hoy": {"$sum": 1}}},
        ],
        {"recaudo_hoy": 0, "pagos_hoy": 0},
    )

    ranking = list(
        boletas.aggregate(
            [
                {"$match": {"total_abonado": {"$gt": 0}}},
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
    nombres = {doc["_id"]: doc.get("nombre", "") for doc in vendedores.find({}, {"nombre": 1})}
    for item in ranking:
        item["nombre"] = nombres.get(item["_id"], "Oficina local" if item["_id"] == "LOCAL" else "")

    recaudo_total = int(totals.get("recaudo_total", 0) or 0)
    recaudo_potencial = total_boletas * valor_boleta

    return {
        **counts,
        "recaudo_total": recaudo_total,
        "saldo_pendiente": totals.get("saldo_pendiente", 0),
        "recaudo_hoy": today_totals.get("recaudo_hoy", 0),
        "pagos_hoy": today_totals.get("pagos_hoy", 0),
        "ranking": ranking,
        "valor_boleta": valor_boleta,
        "recaudo_potencial": recaudo_potencial,
        "progreso_ventas_pct": round((vendidas / total_boletas) * 100, 1) if total_boletas else 0,
        "progreso_recaudo_pct": round((recaudo_total / recaudo_potencial) * 100, 1) if recaudo_potencial else 0,
    }


def get_vendedores_snapshot(config=None):
    require_collections()
    config = config or get_config()
    valor_boleta = int(config["valor_boleta"])

    stats_docs = list(
        boletas.aggregate(
            [
                {
                    "$group": {
                        "_id": "$vendedor_id",
                        "boletas_en_sistema": {"$sum": 1},
                        "vendidas": {"$sum": {"$cond": [{"$gt": ["$total_abonado", 0]}, 1, 0]}},
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


def build_consulta_context(args):
    filters = {
        "numero": args.get("numero", args.get("buscar_numero", "")).strip(),
        "desde": args.get("desde", "").strip(),
        "hasta": args.get("hasta", "").strip(),
        "estado": args.get("estado", "").strip(),
        "vendedor_id": args.get("vendedor_id", "").strip(),
        "cliente": args.get("cliente", "").strip(),
        "telefono": args.get("telefono", "").strip(),
        "pago_metodo": args.get("pago_metodo", "").strip(),
        "facturero": args.get("facturero", "").strip().upper(),
        "referencia": args.get("referencia", "").strip(),
        "cliente_estado": args.get("cliente_estado", "").strip(),
        "abono_estado": args.get("abono_estado", "").strip(),
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

    if filters["facturero"]:
        query["historial_pagos.facturero"] = filters["facturero"]

    if filters["referencia"]:
        query["historial_pagos.referencia"] = {"$regex": re.escape(filters["referencia"]), "$options": "i"}

    if filters["cliente_estado"] == "con_cliente":
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
        query["estado"] = "pagada"
    elif filters["saldo_estado"]:
        errors.append("Filtro de saldo inválido.")

    page = parse_int_filter(args.get("page", "1").strip(), "Página", errors, 1, None) or 1
    offset = (page - 1) * limite
    has_filters = any(value for key, value in filters.items() if key != "limite")
    return filters, query, errors, page, limite, offset, has_filters, numero_exacto


def build_page_url(endpoint, filters, page):
    params = {key: value for key, value in filters.items() if value}
    params["page"] = page
    return url_for(endpoint, **params)


def validar_form_abono(form):
    form_data = {
        "facturero": form.get("facturero", "").strip().upper(),
        "valor": form.get("valor", "").strip(),
        "fecha": form.get("fecha", "").strip() or date.today().isoformat(),
        "metodo": form.get("metodo", "").strip().lower() or "efectivo",
        "referencia": form.get("referencia", "").strip(),
        "boletas": form.get("boletas", "").strip(),
    }
    errors = []

    if not form_data["facturero"]:
        errors.append("El número de facturero es obligatorio.")

    valor_abono = parse_money(form_data["valor"])
    if valor_abono <= 0:
        errors.append("El valor del abono debe ser mayor que cero.")

    try:
        datetime.strptime(form_data["fecha"], "%Y-%m-%d")
    except ValueError:
        errors.append("La fecha debe tener formato AAAA-MM-DD.")

    if form_data["metodo"] not in METODOS_PAGO:
        errors.append("Selecciona un método de pago válido.")

    if form_data["metodo"] == "transferencia" and not form_data["referencia"]:
        errors.append("La referencia bancaria es obligatoria para transferencias.")

    boleta_ids, invalid, out_of_range, duplicadas = parse_boletas_detailed(form_data["boletas"])
    if invalid:
        errors.append("Hay entradas no numéricas: " + ", ".join(invalid[:8]))
    if out_of_range:
        errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
    if not boleta_ids:
        errors.append("Ingresa al menos una boleta válida.")

    return form_data, valor_abono, boleta_ids, duplicadas, errors


def build_abono_preview(form):
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

    if form_data["metodo"] == "transferencia":
        used_refs = list(
            boletas.find(
                {
                    "historial_pagos": {
                        "$elemMatch": {"metodo": "transferencia", "referencia": form_data["referencia"]}
                    }
                },
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
        doc["nuevo_total"] = nuevo_total
        doc["nuevo_estado"] = estado_para_total(nuevo_total, valor_boleta)
        preview["validas"].append(doc)

    if duplicadas:
        preview["warnings"].append("Se ignorarán números duplicados del bloque.")
    if preview["inexistentes"]:
        preview["warnings"].append("Las boletas inexistentes no serán modificadas.")
    if preview["pagadas"]:
        preview["warnings"].append("Las boletas ya pagadas se omitirán.")

    if not preview["validas"]:
        preview["errors"].append("No hay boletas disponibles para registrar este abono.")

    preview["can_confirm"] = bool(preview["validas"]) and not preview["errors"]
    return form_data, preview


def registrar_abono_lote(boleta_ids, form_data, valor_abono):
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    pago = {
        "facturero": form_data["facturero"],
        "fecha": form_data["fecha"],
        "valor": valor_abono,
        "metodo": form_data["metodo"],
        "referencia": form_data["referencia"] if form_data["metodo"] == "transferencia" else "N/A",
        "registrado_en": datetime.utcnow(),
        "usuario": (current_user() or {}).get("username", "sistema"),
    }
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
                    "estado": {
                        "$cond": [
                            {"$gte": ["$total_abonado", valor_boleta]},
                            "pagada",
                            "abonado",
                        ]
                    }
                }
            },
        ],
    )
    log_action(
        "abono_registrado",
        "boletas",
        form_data["facturero"],
        {
            "boletas": boleta_ids,
            "cantidad": len(boleta_ids),
            "valor": valor_abono,
            "metodo": form_data["metodo"],
            "referencia": pago["referencia"],
        },
    )
    return result


def safe_vendedores_snapshot():
    try:
        return get_vendedores_snapshot()
    except Exception as exc:
        flash(f"No se pudo cargar el listado de vendedores: {exc}", "danger")
        return [], {"total_asignadas": 0, "total_recaudado": 0, "total_comision": 0, "total_vendedores": 0}


def column_letter(index):
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def make_csv_response(filename, headers, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    data = "\ufeff" + output.getvalue()
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
    )


def make_xlsx_response(filename, headers, rows):
    sheet_rows = [headers] + rows
    xml_rows = []
    for r_idx, row in enumerate(sheet_rows, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            cell_ref = f"{column_letter(c_idx)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else ""
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{cell_ref}"{style}><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{cell_ref}"{style} t="inlineStr"><is><t>{escape(str(value or ""))}</t></is></c>')
        xml_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    cols = "".join(f'<col min="{idx}" max="{idx}" width="18" customWidth="1"/>' for idx in range(1, len(headers) + 1))
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>{cols}</cols>'
        f'<sheetData>{"".join(xml_rows)}</sheetData><autoFilter ref="A1:{column_letter(len(headers))}{len(sheet_rows)}"/></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Reporte" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0D6EFD"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )

    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles)
        zf.writestr("xl/worksheets/sheet1.xml", worksheet)
    memory.seek(0)
    return Response(
        memory.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"},
    )


def boleta_report_rows(query=None):
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    headers = [
        "boleta",
        "estado",
        "vendedor_id",
        "cliente",
        "telefono",
        "direccion",
        "total_abonado",
        "saldo_pendiente",
    ]
    rows = []
    for doc in boletas.find(query or {}, {"historial_pagos": 0}).sort("_id", 1):
        cliente = doc.get("cliente") or {}
        total = int(doc.get("total_abonado", 0) or 0)
        rows.append(
            [
                f"{doc['_id']:04d}",
                doc.get("estado", ""),
                doc.get("vendedor_id", ""),
                cliente.get("nombre", ""),
                cliente.get("telefono", ""),
                cliente.get("direccion", ""),
                total,
                max(valor_boleta - total, 0) if total > 0 else 0,
            ]
        )
    return headers, rows


def pagos_report_rows(only_transfer=False, fecha=None):
    headers = ["boleta", "vendedor_id", "cliente", "facturero", "fecha", "valor", "metodo", "referencia", "usuario"]
    rows = []
    query = {"historial_pagos.0": {"$exists": True}}
    for doc in boletas.find(query, {"_id": 1, "vendedor_id": 1, "cliente": 1, "historial_pagos": 1}).sort("_id", 1):
        cliente = (doc.get("cliente") or {}).get("nombre", "")
        for pago in doc.get("historial_pagos", []):
            if only_transfer and pago.get("metodo") != "transferencia":
                continue
            if fecha and pago.get("fecha") != fecha:
                continue
            rows.append(
                [
                    f"{doc['_id']:04d}",
                    doc.get("vendedor_id", ""),
                    cliente,
                    pago.get("facturero", ""),
                    pago.get("fecha", ""),
                    pago.get("valor", 0),
                    pago.get("metodo", ""),
                    pago.get("referencia", ""),
                    pago.get("usuario", ""),
                ]
            )
    return headers, rows


MODELO_RIFA_HEADERS = [
    "NUMERO DE BOLETA ",
    "TOTAL ABONO ",
    "FECHA ADQUISICION",
    "VENDEDOR (A)",
    "COMPRADOR(A)",
    "DIRECCION ",
    "TELEFONO ",
    "FECHA ",
    "FACT",
    "ABONO 1",
    "FECHA",
    "FACT",
    "ABONO 2",
    "FECHA",
    "FACT",
    "ABONO 3",
    "FECHA",
    "FACT",
    "ABONO 4",
    "FECHA",
    "FACT",
    "ABONO 5",
    "FECHA",
    "FACT",
    "ABONO 6",
    "FECHA ",
    "FACT",
    "ABONO 7",
    "TOTAL ABONOS EFECTIVO",
    "VS",
    "FECHA",
    "FACT",
    "TFR 1",
    "FECHA",
    "FACT",
    "TFR 2",
    "FECHA",
    "FACT",
    "TFR 3",
    "FECHA",
    "FACT",
    "TFR 4",
    "FECHA",
    "FACT",
    "TFR 5",
    "PAGOS TOTAL TFR",
    "TOTAL ABONADO ",
]


def vendedor_label(vendedor_id, nombres_vendedores):
    vendedor_id = vendedor_id or "LOCAL"
    nombre = nombres_vendedores.get(vendedor_id, "")
    if vendedor_id == "LOCAL" or nombre == "LOCAL":
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
        efectivo = [payment for payment in historial if payment.get("metodo") != "transferencia"]
        transferencias = [payment for payment in historial if payment.get("metodo") == "transferencia"]
        total_efectivo = sum(int(payment.get("valor", 0) or 0) for payment in efectivo)
        total_transferencias = sum(int(payment.get("valor", 0) or 0) for payment in transferencias)
        total_abonado = int(doc.get("total_abonado", 0) or 0)

        row = [
            f"{doc['_id']:04d}",
            total_abonado,
            doc.get("fecha_adquisicion", ""),
            vendedor_label(doc.get("vendedor_id", "LOCAL"), nombres_vendedores),
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


def vendedores_report_rows():
    vendedores_lista, _totals = get_vendedores_snapshot()
    headers = [
        "vendedor_id",
        "nombre",
        "telefono",
        "boletas_asignadas",
        "boletas_vendidas",
        "boletas_pagadas",
        "pendientes_fisicas",
        "recaudado",
        "saldo_pendiente",
        "comision_por_boleta",
        "comision_total",
    ]
    rows = [
        [
            item["_id"],
            item["nombre"],
            item["telefono"],
            item["cantidad"],
            item["vendidas"],
            item["pagadas"],
            item["pendientes_fisicas"],
            item["recaudado"],
            item["saldo_pendiente"],
            item["comision_por_boleta"],
            item["comision"],
        ]
        for item in vendedores_lista
    ]
    return headers, rows


def vendedor_ventas_report_rows(vendedor_id):
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    vendedor = vendedores.find_one({"_id": vendedor_id}, {"nombre": 1}) if vendedores is not None else None
    nombre_vendedor = (vendedor or {}).get("nombre", vendedor_id)
    headers = [
        "vendedor_id",
        "vendedor",
        "boleta",
        "estado",
        "cliente",
        "telefono",
        "direccion",
        "total_abonado",
        "saldo_pendiente",
        "pagos",
        "ultimo_pago_fecha",
        "ultimo_pago_facturero",
        "ultimo_pago_metodo",
    ]
    rows = []
    query = {"vendedor_id": vendedor_id, "total_abonado": {"$gt": 0}}
    for doc in boletas.find(query).sort("_id", 1):
        cliente = doc.get("cliente") or {}
        historial = doc.get("historial_pagos") or []
        ultimo = historial[-1] if historial else {}
        total = int(doc.get("total_abonado", 0) or 0)
        rows.append(
            [
                vendedor_id,
                nombre_vendedor,
                f"{doc['_id']:04d}",
                doc.get("estado", ""),
                cliente.get("nombre", ""),
                cliente.get("telefono", ""),
                cliente.get("direccion", ""),
                total,
                max(valor_boleta - total, 0) if total > 0 else 0,
                len(historial),
                ultimo.get("fecha", ""),
                ultimo.get("facturero", ""),
                ultimo.get("metodo", ""),
            ]
        )
    return headers, rows


def build_report(tipo, fecha=None, vendedor_id=None):
    if tipo == "ventas_vendedor":
        return vendedores_report_rows()
    if tipo == "ventas_por_vendedor":
        if not vendedor_id:
            raise ValueError("Selecciona un vendedor para este reporte.")
        return vendedor_ventas_report_rows(vendedor_id)
    if tipo == "disponibles":
        return boleta_report_rows({"estado": "disponible"})
    if tipo == "abonadas":
        return boleta_report_rows({"estado": "abonado"})
    if tipo == "pagadas":
        return boleta_report_rows({"estado": "pagada"})
    if tipo == "pagos":
        return pagos_report_rows(False, fecha)
    if tipo == "transferencias":
        return pagos_report_rows(True, fecha)
    return boleta_report_rows({})


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/inicio")
def inicio_alias():
    return redirect(url_for("consultas", **request.args.to_dict(flat=True)))


@app.route("/dashboard")
@role_required("admin", "cajero", "consulta")
def dashboard():
    try:
        stats = get_dashboard_stats()
    except Exception as exc:
        stats = {}
        flash(f"No se pudo cargar el dashboard: {exc}", "danger")
    return render_template("dashboard.html", stats=stats)


@app.route("/consultas")
@role_required("admin", "cajero", "consulta")
def consultas():
    try:
        counts = get_dashboard_counts()
        vendedor_options = get_vendedor_options()
    except Exception as exc:
        counts = {"total": 0, "vendidas": 0, "disponibles": 0, "asignadas": 0, "abonadas": 0, "pagadas": 0}
        vendedor_options = []
        flash(f"No se pudieron cargar las métricas: {exc}", "danger")

    filters, query, errors, page, limite, offset, has_filters, numero_exacto = build_consulta_context(request.args)
    resultados = []
    total_resultados = 0
    boleta_detalle = None

    for error in errors:
        flash(error, "warning")

    if not errors:
        try:
            require_collections()
            projection = {
                "_id": 1,
                "vendedor_id": 1,
                "cliente": 1,
                "estado": 1,
                "total_abonado": 1,
                "historial_pagos": {"$slice": -1},
            }
            total_resultados = boletas.count_documents(query)
            resultados = list(boletas.find(query, projection).sort("_id", 1).skip(offset).limit(limite))
            if numero_exacto and isinstance(query.get("_id"), int):
                boleta_detalle = boletas.find_one({"_id": query["_id"]})
                if boleta_detalle:
                    config = get_config()
                    premios_config = config.get("premios_adicionales", [])
                    boleta_detalle["premios_adicionales"] = calcular_premios_adicionales(
                        boleta_detalle.get("historial_pagos", []), premios_config
                    )
        except Exception as exc:
            flash(f"No se pudo ejecutar la consulta: {exc}", "danger")

    total_pages = max(1, (total_resultados + limite - 1) // limite)
    if page > total_pages and total_resultados:
        return redirect(build_page_url("consultas", filters, total_pages))

    prev_url = build_page_url("consultas", filters, page - 1) if page > 1 else None
    next_url = build_page_url("consultas", filters, page + 1) if page < total_pages else None

    return render_template(
        "inicio.html",
        total=counts["total"],
        vendidas=counts["vendidas"],
        disponibles=counts["disponibles"],
        asignadas=counts["asignadas"],
        vendedor_options=vendedor_options,
        filters=filters,
        resultados=resultados,
        total_resultados=total_resultados,
        page=page,
        total_pages=total_pages,
        limite=limite,
        prev_url=prev_url,
        next_url=next_url,
        has_filters=has_filters,
        boleta=boleta_detalle,
    )


@app.route("/boletas/<int:boleta_id>/cliente", methods=["POST"])
@role_required("admin", "cajero")
def actualizar_cliente(boleta_id):
    if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
        flash("El número de boleta debe estar entre 0000 y 9999.", "warning")
        return redirect(url_for("consultas"))

    cliente = {
        "nombre": request.form.get("nombre", "").strip(),
        "telefono": request.form.get("telefono", "").strip(),
        "direccion": request.form.get("direccion", "").strip(),
    }

    try:
        require_collections()
        result = boletas.update_one({"_id": boleta_id}, {"$set": {"cliente": cliente}})
    except Exception as exc:
        flash(f"No se pudieron guardar los datos del cliente: {exc}", "danger")
        return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

    if result.matched_count:
        log_action("cliente_actualizado", "boletas", boleta_id, {"cliente": cliente})
        flash(f"Cliente actualizado para la boleta #{boleta_id:04d}.", "success")
    else:
        flash(f"No existe la boleta #{boleta_id:04d}.", "warning")

    return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))


@app.route("/abonos", methods=["GET", "POST"])
@app.route("/abono_masivo", methods=["GET", "POST"])
@role_required("admin", "cajero")
def abono_masivo():
    form_data = {
        "facturero": "",
        "valor": "",
        "fecha": date.today().isoformat(),
        "metodo": "efectivo",
        "referencia": "",
        "boletas": "",
    }
    preview = None

    if request.method == "POST":
        action = request.form.get("action", "preview")
        try:
            form_data, preview = build_abono_preview(request.form)
        except Exception as exc:
            flash(f"No se pudo previsualizar el abono: {exc}", "danger")
            return render_template("abono_masivo.html", form=form_data, preview=preview)

        if action == "confirm":
            if not preview["can_confirm"]:
                for error in preview["errors"]:
                    flash(error, "danger")
                return render_template("abono_masivo.html", form=form_data, preview=preview)

            valid_ids = [doc["_id"] for doc in preview["validas"]]
            try:
                result = registrar_abono_lote(valid_ids, form_data, preview["valor_abono"])
            except Exception as exc:
                flash(f"No se pudo registrar el abono masivo: {exc}", "danger")
                return render_template("abono_masivo.html", form=form_data, preview=preview)

            flash(f"Abono confirmado en {result.modified_count} boleta(s).", "success")
            return redirect(url_for("abono_masivo"))

        for error in preview["errors"]:
            flash(error, "danger")

    return render_template("abono_masivo.html", form=form_data, preview=preview)


@app.route("/caja", methods=["GET", "POST"])
@role_required("admin", "cajero")
def caja():
    defaults = session.get("caja_defaults", {})
    form_data = {
        "facturero": defaults.get("facturero", ""),
        "valor": defaults.get("valor", ""),
        "fecha": defaults.get("fecha", date.today().isoformat()),
        "metodo": defaults.get("metodo", "efectivo"),
        "referencia": defaults.get("referencia", ""),
        "boletas": "",
    }
    cliente_form = {
        "boleta": request.args.get("boleta", "").strip(),
        "nombre": "",
        "telefono": "",
        "direccion": "",
        "facturero": defaults.get("facturero", ""),
        "valor": "",
        "fecha": defaults.get("fecha", date.today().isoformat()),
        "metodo": defaults.get("metodo", "efectivo"),
        "referencia": "",
    }

    if request.method == "POST":
        action = request.form.get("action", "pago_rapido")
        if action == "cliente_rapido":
            cliente_form = {
                "boleta": request.form.get("boleta_cliente", "").strip(),
                "nombre": request.form.get("nombre", "").strip().upper(),
                "telefono": request.form.get("telefono", "").strip(),
                "direccion": request.form.get("direccion", "").strip().upper(),
                "facturero": request.form.get("facturero_cliente", "").strip().upper(),
                "valor": request.form.get("valor_cliente", "").strip(),
                "fecha": request.form.get("fecha_cliente", "").strip() or date.today().isoformat(),
                "metodo": request.form.get("metodo_cliente", "").strip().lower() or "efectivo",
                "referencia": request.form.get("referencia_cliente", "").strip(),
            }
            errors = []
            numero, exacto = ticket_number_query(cliente_form["boleta"], errors)
            if not exacto or not isinstance(numero, int):
                errors.append("Ingresa el número completo de la boleta, por ejemplo 0004.")
            if not cliente_form["nombre"]:
                errors.append("El nombre del comprador es obligatorio.")

            if errors:
                for error in errors:
                    flash(error, "danger")
                return render_template("caja.html", form=form_data, cliente_form=cliente_form)

            cliente = {
                "nombre": cliente_form["nombre"],
                "telefono": cliente_form["telefono"],
                "direccion": cliente_form["direccion"],
            }
            valor_abono_cliente = parse_money(cliente_form["valor"])
            pago_form = {
                "facturero": cliente_form["facturero"],
                "valor": cliente_form["valor"],
                "fecha": cliente_form["fecha"],
                "metodo": cliente_form["metodo"],
                "referencia": cliente_form["referencia"],
                "boletas": f"{numero:04d}",
            }
            try:
                require_collections()
                result = boletas.update_one({"_id": numero}, {"$set": {"cliente": cliente}})
                pago_result = None
                if valor_abono_cliente > 0:
                    _form_data, preview = build_abono_preview(pago_form)
                    if not preview["can_confirm"]:
                        for error in preview["errors"]:
                            flash(error, "danger")
                        for warning in preview["warnings"]:
                            flash(warning, "warning")
                        return render_template("caja.html", form=form_data, cliente_form=cliente_form)
                    pago_result = registrar_abono_lote([numero], _form_data, preview["valor_abono"])
                    session["caja_defaults"] = {
                        "facturero": _form_data["facturero"],
                        "valor": _form_data["valor"],
                        "fecha": _form_data["fecha"],
                        "metodo": _form_data["metodo"],
                        "referencia": _form_data["referencia"],
                    }
            except Exception as exc:
                flash(f"No se pudo guardar el comprador: {exc}", "danger")
                return render_template("caja.html", form=form_data, cliente_form=cliente_form)

            if not result.matched_count:
                flash(f"No existe la boleta #{numero:04d}.", "warning")
                return render_template("caja.html", form=form_data, cliente_form=cliente_form)

            log_action("cliente_actualizado_caja", "boletas", numero, {"cliente": cliente})
            if valor_abono_cliente > 0 and pago_result:
                flash(f"Comprador y abono guardados para la boleta #{numero:04d}.", "success")
            else:
                flash(f"Comprador guardado para la boleta #{numero:04d}.", "success")
            return redirect(url_for("caja"))

        try:
            form_data, preview = build_abono_preview(request.form)
            if not preview["can_confirm"]:
                for error in preview["errors"]:
                    flash(error, "danger")
                for warning in preview["warnings"]:
                    flash(warning, "warning")
                return render_template("caja.html", form=form_data, cliente_form=cliente_form)
            result = registrar_abono_lote([doc["_id"] for doc in preview["validas"]], form_data, preview["valor_abono"])
            session["caja_defaults"] = {
                "facturero": form_data["facturero"],
                "valor": form_data["valor"],
                "fecha": form_data["fecha"],
                "metodo": form_data["metodo"],
                "referencia": form_data["referencia"],
            }
            flash(f"Pago rápido registrado en {result.modified_count} boleta(s).", "success")
            return redirect(url_for("caja"))
        except Exception as exc:
            flash(f"No se pudo registrar el pago rápido: {exc}", "danger")
    return render_template("caja.html", form=form_data, cliente_form=cliente_form)


@app.route("/vendedores", methods=["GET", "POST"])
@role_required("admin")
def vendedores_panel():
    config = get_config()
    form_data = {
        "vendedor_id": "",
        "nombre": "",
        "telefono": "",
        "operacion": "guardar",
        "boletas": "",
    }

    if request.method == "POST":
        form_data.update(
            {
                "vendedor_id": request.form.get("vendedor_id", ""),
                "nombre": request.form.get("nombre", "").strip(),
                "telefono": request.form.get("telefono", "").strip(),
                "operacion": request.form.get("operacion", "guardar").strip().lower(),
                "boletas": request.form.get("boletas", "").strip(),
            }
        )

        errors = []
        vendedor_id = ""
        try:
            require_collections()
            vendedor_id = normalize_vendedor_id(form_data["vendedor_id"])
            form_data["vendedor_id"] = vendedor_id
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))

        if form_data["operacion"] not in OPERACIONES_VENDEDOR:
            errors.append("Selecciona una operación válida para el vendedor.")

        boleta_ids, invalid, out_of_range = parse_boletas(form_data["boletas"])
        if invalid:
            errors.append("Hay entradas no numéricas: " + ", ".join(invalid[:8]))
        if out_of_range:
            errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
        if form_data["operacion"] in {"asignar", "quitar"} and not boleta_ids:
            errors.append("Ingresa al menos una boleta para esta operación.")

        if errors:
            for error in errors:
                flash(error, "danger")
            vendedores_lista, resumen = safe_vendedores_snapshot()
            return render_template("vendedores.html", form=form_data, vendedores_lista=vendedores_lista, resumen=resumen)

        perfil_set = {
            "nombre": form_data["nombre"],
            "telefono": form_data["telefono"],
        }
        perfil_update = {"$set": perfil_set, "$setOnInsert": {"boletas_asignadas": []}}

        try:
            if form_data["operacion"] == "guardar":
                vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)
                flash(f"Vendedor {vendedor_id} guardado.", "success")

            elif form_data["operacion"] == "asignar":
                existentes = existing_boleta_ids(boleta_ids)
                faltantes = len(boleta_ids) - len(existentes)
                vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)

                if existentes:
                    vendedores.update_many(
                        {"_id": {"$ne": vendedor_id}},
                        {"$pull": {"boletas_asignadas": {"$in": existentes}}},
                    )
                    vendedores.update_one(
                        {"_id": vendedor_id},
                        {"$addToSet": {"boletas_asignadas": {"$each": existentes}}},
                    )
                    boletas.update_many({"_id": {"$in": existentes}}, {"$set": {"vendedor_id": vendedor_id}})

                mensaje = f"{len(existentes)} boleta(s) asignada(s) a {vendedor_id}."
                if faltantes:
                    mensaje += f" {faltantes} no existían en la colección boletas."
                flash(mensaje, "success" if existentes else "warning")

            elif form_data["operacion"] == "quitar":
                existentes = existing_boleta_ids(boleta_ids)
                faltantes = len(boleta_ids) - len(existentes)
                vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)
                vendedores.update_one({"_id": vendedor_id}, {"$pull": {"boletas_asignadas": {"$in": existentes}}})
                if existentes:
                    boletas.update_many(
                        {"_id": {"$in": existentes}, "vendedor_id": vendedor_id},
                        {"$set": {"vendedor_id": "LOCAL"}},
                    )
                mensaje = f"{len(existentes)} boleta(s) quitada(s) de {vendedor_id}."
                if faltantes:
                    mensaje += f" {faltantes} no existían en la colección boletas."
                flash(mensaje, "success" if existentes else "warning")

            elif form_data["operacion"] == "reemplazar":
                existentes = existing_boleta_ids(boleta_ids)
                faltantes = len(boleta_ids) - len(existentes)
                vendedores.update_many(
                    {"_id": {"$ne": vendedor_id}},
                    {"$pull": {"boletas_asignadas": {"$in": existentes}}},
                )
                vendedores.update_one(
                    {"_id": vendedor_id},
                    {"$set": {**perfil_set, "boletas_asignadas": existentes}},
                    upsert=True,
                )
                boletas.update_many({"vendedor_id": vendedor_id, "_id": {"$nin": existentes}}, {"$set": {"vendedor_id": "LOCAL"}})
                if existentes:
                    boletas.update_many({"_id": {"$in": existentes}}, {"$set": {"vendedor_id": vendedor_id}})
                mensaje = f"Lista de {vendedor_id} reemplazada con {len(existentes)} boleta(s)."
                if faltantes:
                    mensaje += f" {faltantes} no existían en la colección boletas."
                flash(mensaje, "success")

            log_action(
                f"vendedor_{form_data['operacion']}",
                "vendedores",
                vendedor_id,
                {"boletas": boleta_ids, "perfil": perfil_set},
            )
        except Exception as exc:
            flash(f"No se pudo aplicar la operación del vendedor: {exc}", "danger")
            vendedores_lista, resumen = safe_vendedores_snapshot()
            return render_template("vendedores.html", form=form_data, vendedores_lista=vendedores_lista, resumen=resumen)

        return redirect(url_for("vendedores_panel"))

    vendedores_lista, resumen = safe_vendedores_snapshot()
    return render_template("vendedores.html", form=form_data, vendedores_lista=vendedores_lista, resumen=resumen)


@app.route("/clientes")
@role_required("admin", "cajero", "consulta")
def clientes():
    flash("La búsqueda de clientes quedó integrada en Consultas.", "info")
    return redirect(url_for("consultas", cliente_estado="con_cliente"))


@app.route("/api/clientes")
@role_required("admin", "cajero", "consulta")
def api_clientes():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    query = {
        "$or": [
            {"cliente.nombre": {"$regex": re.escape(q), "$options": "i"}},
            {"cliente.telefono": {"$regex": re.escape(q)}},
        ]
    }
    docs = boletas.find(query, {"cliente": 1}).limit(12)
    seen = set()
    items = []
    for doc in docs:
        cliente = doc.get("cliente") or {}
        label = f"{cliente.get('nombre', '')} {cliente.get('telefono', '')}".strip()
        if label and label not in seen:
            seen.add(label)
            items.append({"label": label, "nombre": cliente.get("nombre", ""), "telefono": cliente.get("telefono", "")})
    return jsonify(items)


@app.route("/api/boletas/<int:boleta_id>")
@role_required("admin", "cajero", "consulta")
def api_boleta(boleta_id):
    if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
        return jsonify({"ok": False, "error": "Boleta fuera de rango."}), 400

    try:
        require_collections()
        doc = boletas.find_one(
            {"_id": boleta_id},
            {"_id": 1, "vendedor_id": 1, "cliente": 1, "estado": 1, "total_abonado": 1, "historial_pagos": 1},
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if not doc:
        return jsonify({"ok": False, "error": "No existe la boleta."}), 404

    cliente = doc.get("cliente") or {}
    config = get_config()
    premios_config = config.get("premios_adicionales", [])
    premios = calcular_premios_adicionales(doc.get("historial_pagos", []), premios_config)

    return jsonify(
        {
            "ok": True,
            "boleta": f"{doc['_id']:04d}",
            "vendedor_id": doc.get("vendedor_id", "LOCAL"),
            "estado": doc.get("estado", ""),
            "total_abonado": int(doc.get("total_abonado", 0) or 0),
            "cliente": {
                "nombre": cliente.get("nombre", ""),
                "telefono": cliente.get("telefono", ""),
                "direccion": cliente.get("direccion", ""),
            },
            "premios_adicionales": premios,
        }
    )


@app.route("/reportes")
@role_required("admin", "cajero", "consulta")
def reportes():
    try:
        vendedor_options = get_vendedor_options()
    except Exception as exc:
        vendedor_options = []
        flash(f"No se pudo cargar vendedores para reportes: {exc}", "warning")
    return render_template("reportes.html", fecha=date.today().isoformat(), vendedor_options=vendedor_options)


@app.route("/reportes/exportar")
@role_required("admin", "cajero", "consulta")
def exportar_reporte():
    tipo = request.args.get("tipo", "boletas")
    formato = request.args.get("formato", "csv")
    fecha = request.args.get("fecha", "").strip() or None
    vendedor_id = request.args.get("vendedor_id", "").strip()
    if tipo not in {"ventas_vendedor", "ventas_por_vendedor", "boletas", "disponibles", "abonadas", "pagadas", "pagos", "transferencias"}:
        abort(404)
    try:
        headers, rows = build_report(tipo, fecha, vendedor_id)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("reportes"))
    filename_suffix = f"_{vendedor_id}" if vendedor_id else ""
    filename = f"{tipo}{filename_suffix}_{date.today().isoformat()}"
    log_action("reporte_exportado", "reportes", tipo, {"formato": formato, "fecha": fecha, "vendedor_id": vendedor_id, "filas": len(rows)})
    if formato == "xlsx":
        return make_xlsx_response(filename, headers, rows)
    return make_csv_response(filename, headers, rows)


@app.route("/reportes/modelo-rifa.xlsx")
@role_required("admin", "cajero", "consulta")
def exportar_modelo_rifa():
    try:
        headers, rows = modelo_rifa_report_rows()
    except Exception as exc:
        flash(f"No se pudo generar el modelo de rifa: {exc}", "danger")
        return redirect(url_for("reportes"))

    filename = f"modelo_rifa_{date.today().isoformat()}"
    log_action("modelo_rifa_exportado", "reportes", "modelo_rifa", {"filas": len(rows)})
    return make_xlsx_response(filename, headers, rows)


@app.route("/auditoria")
@role_required("admin")
def auditoria_panel():
    action = request.args.get("accion", "").strip()
    query = {"accion": action} if action else {}
    registros = []
    try:
        registros = list(auditoria.find(query).sort("fecha", -1).limit(150))
    except Exception as exc:
        flash(f"No se pudo cargar auditoría: {exc}", "danger")
    acciones = sorted(auditoria.distinct("accion")) if auditoria is not None else []
    return render_template("auditoria.html", registros=registros, acciones=acciones, accion=action)


XLSX_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_REL_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def col_to_index(column):
    index = 0
    for char in column:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def clean_excel_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return text.strip()


def parse_excel_number(value):
    text = clean_excel_text(value).replace("$", "").replace(",", "")
    if text == "":
        return 0
    try:
        return int(float(text))
    except ValueError:
        digits = re.sub(r"[^\d-]", "", text)
        return int(digits) if digits not in {"", "-"} else 0


def parse_excel_boleta(value):
    text = clean_excel_text(value).strip().lstrip("'\"")
    if text == "":
        return None
    try:
        number = int(float(text))
    except ValueError:
        digits = re.sub(r"\D", "", text)
        number = int(digits) if digits else None
    if number is None or number < BOLETA_MIN or number > BOLETA_MAX:
        return None
    return number


def parse_excel_date(value):
    text = clean_excel_text(value)
    if not text:
        return ""
    try:
        serial = float(text)
        if 0 < serial < 100000:
            return (datetime(1899, 12, 30) + timedelta(days=int(serial))).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def normalize_phone(value):
    text = clean_excel_text(value)
    if not text:
        return ""
    try:
        return str(int(float(text)))
    except ValueError:
        return re.sub(r"\s+", " ", text)


def vendor_from_excel(value):
    raw = re.sub(r"\s+", " ", clean_excel_text(value)).strip()
    if not raw:
        return "LOCAL", "LOCAL"
    nombre = re.sub(r"^VEND\.?\s*", "", raw, flags=re.IGNORECASE).strip() or raw

    ascii_name = unicode_normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii")
    vendedor_id = re.sub(r"[^A-Z0-9]+", "_", ascii_name.upper()).strip("_")
    vendedor_id = vendedor_id[:32].strip("_") or "LOCAL"
    return vendedor_id, nombre.upper()


def is_assignable_vendor_cell(value):
    raw = re.sub(r"\s+", " ", clean_excel_text(value)).strip()
    if not raw:
        return False
    nombre = re.sub(r"^VEND\.?\s*", "", raw, flags=re.IGNORECASE).strip()
    if not nombre:
        return False
    return nombre.upper() != "LOCAL"


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


def payment_from_columns(row, date_index, fact_index, value_index, metodo):
    valor = parse_excel_number(row_value(row, value_index))
    if valor <= 0:
        return None

    facturero = clean_excel_text(row_value(row, fact_index)).upper() or "IMPORTADO"
    fecha = parse_excel_date(row_value(row, date_index)) or parse_excel_date(row_value(row, 2)) or date.today().isoformat()
    referencia = facturero if metodo == "transferencia" else "N/A"
    return {
        "facturero": facturero,
        "fecha": fecha,
        "valor": valor,
        "metodo": metodo,
        "referencia": referencia,
        "registrado_en": datetime.utcnow(),
        "usuario": (current_user() or {}).get("username", "importador"),
        "origen": "xlsx_modelo_rifa",
    }


def parse_modelo_rifa_xlsx(file_obj, valor_boleta):
    rows = read_xlsx_first_sheet_rows(file_obj)
    if not rows:
        raise ValueError("El archivo está vacío.")

    headers = [clean_excel_text(value).upper() for value in rows[0]]
    required_headers = {"NUMERO DE BOLETA", "TOTAL ABONO", "VENDEDOR (A)", "COMPRADOR(A)", "TOTAL ABONADO"}
    normalized_headers = {header.strip() for header in headers}
    missing = [header for header in required_headers if header not in normalized_headers]
    if missing:
        raise ValueError("El archivo no parece ser el modelo esperado. Faltan columnas: " + ", ".join(missing))

    efectivo_groups = [(7, 8, 9), (10, 11, 12), (13, 14, 15), (16, 17, 18), (19, 20, 21), (22, 23, 24), (25, 26, 27)]
    transferencia_groups = [(30, 31, 32), (33, 34, 35), (36, 37, 38), (39, 40, 41), (42, 43, 44)]
    docs = []
    vendor_assignments = defaultdict(list)
    vendor_names = {}
    invalid_rows = []
    imported_payments = 0

    for excel_row_number, row in enumerate(rows[1:], start=2):
        numero = parse_excel_boleta(row_value(row, 0))
        if numero is None:
            if any(clean_excel_text(value) for value in row):
                invalid_rows.append(excel_row_number)
            continue

        vendedor_id, vendedor_nombre = vendor_from_excel(row_value(row, 3))
        vendor_assignments[vendedor_id].append(numero)
        vendor_names[vendedor_id] = vendedor_nombre

        historial = []
        for group in efectivo_groups:
            payment = payment_from_columns(row, *group, metodo="efectivo")
            if payment:
                historial.append(payment)
        for group in transferencia_groups:
            payment = payment_from_columns(row, *group, metodo="transferencia")
            if payment:
                historial.append(payment)

        total_modelo = parse_excel_number(row_value(row, 46)) or parse_excel_number(row_value(row, 1))
        total_historial = sum(payment["valor"] for payment in historial)
        ajuste = total_modelo - total_historial
        if ajuste > 0:
            historial.append(
                {
                    "facturero": "IMPORTADO",
                    "fecha": parse_excel_date(row_value(row, 2)) or date.today().isoformat(),
                    "valor": ajuste,
                    "metodo": "efectivo",
                    "referencia": "AJUSTE XLSX",
                    "registrado_en": datetime.utcnow(),
                    "usuario": (current_user() or {}).get("username", "importador"),
                    "origen": "xlsx_modelo_rifa",
                }
            )
        total_abonado = max(total_modelo, total_historial)
        imported_payments += len(historial)

        docs.append(
            {
                "_id": numero,
                "vendedor_id": vendedor_id,
                "cliente": {
                    "nombre": clean_excel_text(row_value(row, 4)).upper(),
                    "telefono": normalize_phone(row_value(row, 6)),
                    "direccion": clean_excel_text(row_value(row, 5)).upper(),
                },
                "fecha_adquisicion": parse_excel_date(row_value(row, 2)),
                "estado": estado_para_total(total_abonado, valor_boleta),
                "total_abonado": total_abonado,
                "historial_pagos": historial,
                "importado_en": datetime.utcnow(),
            }
        )

    if not docs:
        raise ValueError("No se encontraron boletas válidas en el archivo.")

    return docs, vendor_assignments, vendor_names, {
        "boletas": len(docs),
        "vendedores": len(vendor_assignments),
        "pagos": imported_payments,
        "clientes": sum(1 for doc in docs if doc["cliente"]["nombre"]),
        "invalid_rows": invalid_rows[:20],
    }


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
            ignored_local += 1
            continue

        vendedor_id, vendedor_nombre = vendor_from_excel(vendedor_cell)
        vendor_assignments[vendedor_id].append(numero)
        vendor_names[vendedor_id] = vendedor_nombre

    return vendor_assignments, vendor_names, {
        "boletas_asignadas": sum(len(set(ids)) for ids in vendor_assignments.values()),
        "vendedores": len(vendor_assignments),
        "local_ignoradas": ignored_local,
        "sin_vendedor": empty_vendor,
        "invalid_rows": invalid_rows[:20],
    }


def importar_modelo_rifa(file_obj):
    require_collections()
    vendor_assignments, vendor_names, summary = parse_asignaciones_vendedores_xlsx(file_obj)

    assigned_ids = sorted({number for ids in vendor_assignments.values() for number in ids})
    for vendedor_id, ids in vendor_assignments.items():
        unique_ids = sorted(set(ids))
        if unique_ids:
            boletas.update_many(
                {"_id": {"$in": unique_ids}},
                {"$set": {"vendedor_id": vendedor_id}},
            )

    vendor_ops = []
    for vendedor_id, assigned in vendor_assignments.items():
        vendor_ops.append(
            UpdateOne(
                {"_id": vendedor_id},
                {
                    "$set": {
                        "nombre": vendor_names.get(vendedor_id, vendedor_id),
                        "boletas_asignadas": sorted(set(assigned)),
                    },
                    "$setOnInsert": {"telefono": ""},
                },
                upsert=True,
            )
        )
    if vendor_ops:
        vendedores.bulk_write(vendor_ops, ordered=False)

    summary["boletas_actualizadas"] = len(assigned_ids)
    summary["boletas_locales_omitidas"] = 0
    log_action("asignaciones_importadas_xlsx", "boletas", "modelo_rifa", summary)
    return summary


def crear_boleta_base(numero):
    return {
        "_id": numero,
        "vendedor_id": "LOCAL",
        "cliente": {"nombre": "", "telefono": "", "direccion": ""},
        "estado": "disponible",
        "total_abonado": 0,
        "historial_pagos": [],
    }


def crear_nueva_rifa(nombre, valor_boleta, conservar_vendedores):
    require_collections()
    asignaciones = []
    if conservar_vendedores:
        asignaciones = list(vendedores.find({}, {"boletas_asignadas": 1}))

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
                boletas.update_many({"_id": {"$in": ids}}, {"$set": {"vendedor_id": vendedor["_id"]}})
    else:
        vendedores.delete_many({})

    update = {
        "nombre_rifa": nombre,
        "valor_boleta": valor_boleta,
        "creada_en": datetime.utcnow(),
        "premios_adicionales": [],
    }
    configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
    invalidate_config_cache()
    log_action(
        "nueva_rifa",
        "configuracion",
        CONFIG_ID,
        {
            "nombre_rifa": nombre,
            "valor_boleta": valor_boleta,
            "conservar_vendedores": conservar_vendedores,
        },
    )


@app.route("/configuracion", methods=["GET", "POST"])
@role_required("admin")
def configuracion_panel():
    config = get_config()
    if request.method == "POST":
        if "premio_nombre[]" in request.form:
            premios_adicionales = []
            nombres = request.form.getlist("premio_nombre[]")
            fechas = request.form.getlist("premio_fecha[]")
            for nombre_p, fecha_p in zip(nombres, fechas):
                nombre_p = nombre_p.strip().upper()
                fecha_p = fecha_p.strip()
                if nombre_p and fecha_p:
                    premios_adicionales.append({"nombre": nombre_p, "fecha_juego": fecha_p})
            configuracion.update_one({"_id": CONFIG_ID}, {"$set": {"premios_adicionales": premios_adicionales}}, upsert=True)
            invalidate_config_cache()
            log_action("premios_adicionales_actualizados", "configuracion", CONFIG_ID, {"premios": premios_adicionales})
            flash("Premios adicionales guardados.", "success")
            return redirect(url_for("configuracion_panel"))

        valor_boleta = parse_money(request.form.get("valor_boleta", ""))
        nombre = request.form.get("nombre_rifa", "").strip() or DEFAULT_CONFIG["nombre_rifa"]

        errors = []
        if valor_boleta <= 0:
            errors.append("El valor de la boleta debe ser mayor que cero.")

        if errors:
            for error in errors:
                flash(error, "danger")
        else:
            update = {
                "nombre_rifa": nombre,
                "valor_boleta": valor_boleta,
            }
            configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
            invalidate_config_cache()
            sync_ticket_statuses(valor_boleta)
            log_action("configuracion_actualizada", "configuracion", CONFIG_ID, update)
            flash("Configuración guardada y estados de boletas sincronizados.", "success")
            return redirect(url_for("configuracion_panel"))

    return render_template("configuracion.html", config=get_config())


@app.route("/rifas/nueva", methods=["POST"])
@role_required("admin")
def nueva_rifa():
    nombre = request.form.get("nombre_rifa_nueva", "").strip() or f"Rifa {date.today().isoformat()}"
    valor_boleta = parse_money(request.form.get("valor_boleta_nueva", ""))
    conservar_vendedores = request.form.get("conservar_vendedores") == "on"
    confirmacion = request.form.get("confirmacion", "").strip().upper()

    errors = []
    if valor_boleta <= 0:
        errors.append("El valor de la nueva rifa debe ser mayor que cero.")
    if confirmacion != "NUEVA RIFA":
        errors.append("Escribe NUEVA RIFA para confirmar la reinicialización.")

    if errors:
        for error in errors:
            flash(error, "danger")
        return redirect(url_for("configuracion_panel"))

    try:
        crear_nueva_rifa(nombre, valor_boleta, conservar_vendedores)
    except Exception as exc:
        flash(f"No se pudo crear la nueva rifa: {exc}", "danger")
        return redirect(url_for("configuracion_panel"))

    flash("Nueva rifa creada correctamente.", "success")
    return redirect(url_for("dashboard"))


@app.route("/rifas/importar", methods=["POST"])
@role_required("admin")
def importar_rifa_excel():
    archivo = request.files.get("archivo_rifa")
    confirmacion = request.form.get("confirmacion_importacion", "").strip().upper()

    if confirmacion != "IMPORTAR":
        flash("Escribe IMPORTAR para confirmar la actualización desde Excel.", "danger")
        return redirect(url_for("configuracion_panel"))

    if not archivo or not archivo.filename:
        flash("Selecciona un archivo .xlsx para importar.", "danger")
        return redirect(url_for("configuracion_panel"))

    if not archivo.filename.lower().endswith(".xlsx"):
        flash("El archivo debe tener formato .xlsx.", "danger")
        return redirect(url_for("configuracion_panel"))

    try:
        summary = importar_modelo_rifa(archivo.stream)
    except Exception as exc:
        flash(f"No se pudo importar el modelo de rifa: {exc}", "danger")
        return redirect(url_for("configuracion_panel"))

    message = (
        f"Asignaciones actualizadas: {summary['boletas_asignadas']} boleta(s) procesada(s), "
        f"{summary['vendedores']} vendedor(es), {summary['boletas_actualizadas']} actualizada(s), "
        f"{summary['boletas_locales_omitidas']} LOCAL omitida(s)."
    )
    if summary["invalid_rows"]:
        message += " Filas omitidas: " + ", ".join(str(row) for row in summary["invalid_rows"])
    flash(message, "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(debug=True)

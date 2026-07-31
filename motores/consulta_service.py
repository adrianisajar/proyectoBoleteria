import re

from flask import url_for

from motores.constants import (
    BOLETA_MAX,
    BOLETA_MIN,
    CONSULTA_LIMIT_DEFAULT,
    CONSULTA_LIMIT_MAX,
    ESTADOS_BOLETA,
    METODOS_PAGO,
    VENDEDOR_LOCAL,
)
from motores.validacion import parse_int_filter, parse_money, ticket_number_query

ABONADO_OP_MAP = {"gte": "$gte", "lte": "$lte", "eq": "$eq"}


def build_consulta_context(args: dict) -> dict:
    """Build MongoDB query filters + pagination from request.args."""
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
    """Build paginated url_for with current filters."""
    params = {key: value for key, value in filters.items() if value is not None and value != ""}
    params["page"] = page
    return url_for(endpoint, **params)

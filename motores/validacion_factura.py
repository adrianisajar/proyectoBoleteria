"""Realtime validation for invoice forms (cliente/vendedor).

This module mirrors the validation rules enforced by the atomic POST flows in
``facturacion_cliente.py`` / ``facturacion_vendedor.py`` but performs NO writes
to the database. It is the single source of truth used by the frontend via
``POST /api/validar-factura`` so that every detectable error surfaces before the
user presses "Generar factura".
"""

import re
from collections import Counter
from datetime import datetime

from database import boletas, vendedores
from motores.constants import METODO_EFECTIVO, METODO_TRANSFERENCIA, VENDEDOR_LOCAL
from motores.fechas import now_local
from motores.payment_service import buscar_transferencia_duplicada
from motores.shared import get_config, require_collections
from motores.validacion import es_boleta_completa, parse_money


def _filas_desde_payload(payload: dict) -> list[dict]:
    """Extract the filas array from a validation payload."""
    filas = payload.get("filas") or []
    if not isinstance(filas, list):
        return []
    return filas


def _normalizar_metodo(valor: str) -> str:
    return (valor or "").strip().lower() or METODO_EFECTIVO


def _validar_fecha(fecha: str, campo_errores: dict) -> None:
    """Validate date presence, format and that it is not in the future."""
    if not fecha:
        campo_errores["fecha"].append("Debe indicar la fecha del abono.")
        return
    try:
        fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
        if fecha_dt.date() > now_local().date():
            campo_errores["fecha"].append("La fecha no puede ser posterior a hoy.")
    except ValueError:
        campo_errores["fecha"].append("Formato de fecha inv\u00e1lido.")


def _dividir_boletas(raw: str) -> list[str]:
    """Split a ticket field into digit tokens (vendedor: multi per row)."""
    return [p.strip() for p in re.split(r"[\s,;]+", (raw or "").strip()) if p.strip()]


def _resolver_duplicados_globales(filas: list[dict]) -> set[int]:
    """Return the set of ticket numbers that appear in more than one fila."""
    conteo = Counter()
    for fila in filas:
        for part in _dividir_boletas(fila.get("boletas", "")):
            if es_boleta_completa(part):
                conteo[int(part)] += 1
    return {num for num, count in conteo.items() if count > 1}


def _nombres_vendedores(ids: set[str]) -> dict[str, str]:
    """Map vendor ids to their display names (id used as fallback)."""
    if not ids:
        return {}
    nombres = {}
    for v in vendedores.find({"_id": {"$in": list(ids)}}, {"_id": 1, "nombre": 1}):
        nombres[v["_id"]] = v.get("nombre") or v["_id"]
    return nombres


def _validar_filas_vendedor(filas: list[dict], valor_boleta: int, vendedor_id: str, duplicados: set[int]) -> list[dict]:
    """Validate vendedor rows: boletas parse/range/exist/ownership, montos, transferencias."""
    resultados = []
    for fila in filas:
        res = {"boletas": [], "monto": [], "referencia": [], "banco": []}
        boletas_raw = fila.get("boletas", "")
        monto = parse_money(fila.get("monto", ""))
        metodo = _normalizar_metodo(fila.get("metodo", ""))
        referencia = (fila.get("referencia") or "").strip()
        banco = (fila.get("banco") or "").strip()

        partes_raw = _dividir_boletas(boletas_raw)
        no_digitos = [p for p in partes_raw if not p.isdigit()]
        incompletas = [p for p in partes_raw if p.isdigit() and not es_boleta_completa(p)]
        partes = [p for p in partes_raw if es_boleta_completa(p)]
        if no_digitos:
            res["boletas"].append(f"Valores no num\u00e9ricos: {', '.join(no_digitos[:8])}.")
        if incompletas:
            res["boletas"].append(f"Boleta(s) incompleta(s), escriba los 4 d\u00edgitos: {', '.join(incompletas[:8])}.")
        duplicadas_fila = {int(p) for p in partes if int(p) in duplicados}
        if duplicadas_fila:
            res["boletas"].append("Boleta(s) duplicada(s) en el abono: " + ", ".join(f"#{n:04d}" for n in sorted(duplicadas_fila)) + ".")

        if not partes and not no_digitos and not incompletas:
            res["boletas"].append("Ingrese al menos un n\u00famero de boleta.")
        if monto <= 0:
            res["monto"].append("El valor del abono debe ser mayor que cero.")
        elif partes and monto > valor_boleta * len(partes):
            res["monto"].append(f"El monto ${monto:,} para {len(partes)} boleta(s) supera el m\u00e1ximo de ${valor_boleta * len(partes):,}.")

        if metodo == METODO_TRANSFERENCIA:
            if not referencia:
                res["referencia"].append("La referencia es obligatoria para transferencia.")
            if not banco:
                res["banco"].append("El banco es obligatorio para transferencia.")
            if referencia:
                dup = buscar_transferencia_duplicada(referencia, banco)
                if dup:
                    res["referencia"].append(f"Ya existe un pago por transferencia con referencia {referencia} y banco {banco} (boleta #{dup['_id']:04d}).")

        resultados.append(res)

    return resultados


def _validar_filas_cliente(filas: list[dict], valor_boleta: int, campo_errores: dict, duplicados: set[int]) -> list[dict]:
    """Validate cliente rows: boleta parse/range/exist/estado, monto, transferencias."""
    resultados = []
    for fila in filas:
        res = {"boletas": [], "monto": [], "referencia": [], "banco": []}
        boletas_raw = fila.get("boletas", "")
        monto = parse_money(fila.get("monto", ""))
        metodo = _normalizar_metodo(fila.get("metodo", ""))
        referencia = (fila.get("referencia") or "").strip()
        banco = (fila.get("banco") or "").strip()

        raw = (boletas_raw or "").strip()
        if not raw:
            res["boletas"].append("Ingrese un n\u00famero de boleta.")
        elif not raw.isdigit():
            res["boletas"].append(f"'{raw}' no es un n\u00famero de boleta v\u00e1lido.")
        elif not es_boleta_completa(raw):
            res["boletas"].append("Debe escribir los 4 d\u00edgitos de la boleta (ej: 0042).")
        else:
            num = int(raw)
            if num in duplicados:
                res["boletas"].append(f"#{num:04d} est\u00e1 repetida en la factura.")
            else:
                if monto > valor_boleta:
                    res["monto"].append(f"El monto ${monto:,} supera el valor de la boleta (${valor_boleta:,}).")

        if metodo == METODO_TRANSFERENCIA:
            if not referencia:
                res["referencia"].append("La referencia es obligatoria para transferencia.")
            if not banco:
                res["banco"].append("El banco es obligatorio para transferencia.")
            if referencia:
                dup = buscar_transferencia_duplicada(referencia, banco)
                if dup:
                    res["referencia"].append(f"Ya existe un pago por transferencia con referencia {referencia} y banco {banco} (boleta #{dup['_id']:04d}).")

        resultados.append(res)
    return resultados


def _verificar_boletas_en_db(filas: list[dict], resultados: list[dict], tipo: str, vendedor_id: str = "", duplicados: set[int] | None = None) -> None:
    """Cross-check ticket existence, state and (vendedor) ownership against the DB."""
    duplicados = duplicados or set()
    ids = set()
    for fila in filas:
        for part in _dividir_boletas(fila.get("boletas", "")):
            if es_boleta_completa(part) and int(part) not in duplicados:
                ids.add(int(part))
    if not ids:
        return

    docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": list(ids)}})}
    for fila, res in zip(filas, resultados, strict=False):
        for part in _dividir_boletas(fila.get("boletas", "")):
            if not es_boleta_completa(part):
                continue
            num = int(part)
            if num in duplicados:
                continue
            if num not in docs:
                res["boletas"].append(f"#{num:04d} no existe.")
                continue
            doc = docs[num]
            if doc.get("estado") == "pagada":
                res["boletas"].append(f"#{num:04d} ya est\u00e1 pagada.")
            elif tipo == "vendedor" and vendedor_id and doc.get("vendedor_id", "") != vendedor_id:
                actual = doc.get("vendedor_id", "")
                v_nombre = actual
                if actual and actual != VENDEDOR_LOCAL:
                    v = _nombres_vendedores({actual}).get(actual)
                    v_nombre = v or actual
                res["boletas"].append(f"#{num:04d} pertenece a {v_nombre or 'sin asignar'} y no a este vendedor.")


def validar_factura(payload: dict) -> dict:
    """Validate a full invoice form without writing.

    Returns a structured dict with ``ok`` (no errors) and per-field/per-row errors
    keyed by the same indexes the frontend uses.
    """
    require_collections()
    tipo = (payload.get("tipo") or "cliente").strip().lower()
    config = get_config()
    valor_boleta = int(config.get("valor_boleta", 10000) or 10000)

    campo_errores = {"vendedor": [], "fecha": [], "nombre": [], "telefono": [], "direccion": []}
    filas = _filas_desde_payload(payload)
    duplicados = _resolver_duplicados_globales(filas)

    if tipo == "vendedor":
        vendedor_id = (payload.get("vendedor_id") or "").strip()
        if not vendedor_id:
            campo_errores["vendedor"].append("Debe seleccionar un vendedor.")
        _validar_fecha(payload.get("fecha", ""), campo_errores)
        resultados = _validar_filas_vendedor(filas, valor_boleta, vendedor_id, duplicados)
        _verificar_boletas_en_db(filas, resultados, "vendedor", vendedor_id, duplicados)
    else:
        nombre = (payload.get("nombre") or "").strip()
        telefono = (payload.get("telefono") or "").strip()
        direccion = (payload.get("direccion") or "").strip()
        if not nombre:
            campo_errores["nombre"].append("El nombre del cliente es obligatorio.")
        if len(nombre) > 100:
            campo_errores["nombre"].append("El nombre no puede tener m\u00e1s de 100 caracteres.")
        if telefono and not all(c in "0123456789 +-()" for c in telefono):
            campo_errores["telefono"].append("El tel\u00e9fono contiene caracteres inv\u00e1lidos. Use solo d\u00edgitos, espacios, guiones o par\u00e9ntesis.")
        if len(telefono) > 30:
            campo_errores["telefono"].append("El tel\u00e9fono no puede tener m\u00e1s de 30 caracteres.")
        if len(direccion) > 200:
            campo_errores["direccion"].append("La direcci\u00f3n no puede tener m\u00e1s de 200 caracteres.")
        _validar_fecha(payload.get("fecha", ""), campo_errores)
        resultados = _validar_filas_cliente(filas, valor_boleta, campo_errores, duplicados)
        _verificar_boletas_en_db(filas, resultados, "cliente", duplicados=duplicados)

    if not any(fila.get("boletas", "").strip() for fila in filas):
        campo_errores.setdefault("boletas", []).append("Ingrese al menos una boleta.")

    errores_totales = sum(len(v) for v in campo_errores.values())
    errores_totales += sum(len(res["boletas"]) + len(res["monto"]) + len(res["referencia"]) + len(res["banco"]) for res in resultados)
    campo_errores = {k: v for k, v in campo_errores.items() if v}

    return {
        "ok": errores_totales == 0,
        "can_submit": errores_totales == 0,
        "total_errores": errores_totales,
        "campo_errores": campo_errores,
        "filas": resultados,
    }

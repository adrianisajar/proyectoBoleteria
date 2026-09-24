import uuid
from datetime import datetime

from pymongo.results import BulkWriteResult

from database import boletas, configuracion, facturas, vendedores
from motores.auth import current_user
from motores.cache import invalidate_dashboard_cache
from motores.config_service import get_config, require_collections
from motores.constants import (
    METODO_EFECTIVO,
    METODO_PAGO_DELIO,
    METODO_TRANSFERENCIA,
    METODOS_PAGO,
    MOV_PAGO,
    MOVIMIENTOS_FIELD,
    USUARIO_SISTEMA,
    VENDEDOR_LOCAL,
)
from motores.fechas import now_local
from motores.ticket_service import estado_para_total, estado_pipeline_expr, movimiento_neto_expr
from motores.validacion import boletas_incompletas, parse_boletas_detailed, parse_money


def _pago_match(extra: dict | None = None) -> dict:
    """Build an $elemMatch filter that only matches real payments (tipo pago).

    Includes legacy entries missing the ``tipo`` field ($exists: False).
    """
    match: dict = {"$or": [{"tipo": MOV_PAGO}, {"tipo": {"$exists": False}}]}
    if extra:
        match.update(extra)
    return {"historial_movimientos": {"$elemMatch": match}}


def buscar_transferencia_duplicada(ref: str, banco: str = "", exclude_factura_id: int | None = None) -> dict | None:
    """Check if a transfer reference+banco was already used in another payment."""
    elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": ref}
    if banco:
        elem_match["banco"] = banco
    if exclude_factura_id is not None:
        elem_match["factura_id"] = {"$ne": exclude_factura_id}
    return boletas.find_one(_pago_match(elem_match), {"_id": 1})


def build_factura_detalle(boleta_ids: list[int], factura_id: int) -> list[dict]:
    """Build invoice detail lines from historial_movimientos of given tickets."""
    docs = list(boletas.find({"_id": {"$in": boleta_ids}}, {MOVIMIENTOS_FIELD: 1}, sort=[("_id", 1)]))
    detalle = []
    for doc in docs:
        for pago in doc.get(MOVIMIENTOS_FIELD) or []:
            if pago.get("tipo") not in (None, MOV_PAGO):
                continue
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


def build_boletas_info_snapshot(boleta_ids: list[int], valor_boleta: int) -> dict:
    """Build static per-ticket receipt info for a cliente invoice (creation/backfill only).

    Only certifies what the receipt needs: ticket value and vendor. Ticket state
    (total, saldo, estado) belongs to the ticket, not to the receipt.
    """
    docs = list(boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1, "vendedor_id": 1}))
    vendedores_vistos = {d.get("vendedor_id") for d in docs if d.get("vendedor_id") and d.get("vendedor_id") != VENDEDOR_LOCAL}
    vid_cache: dict = {}
    if vendedores_vistos:
        for v in vendedores.find({"_id": {"$in": list(vendedores_vistos)}}, {"nombre": 1}):
            vid_cache[v["_id"]] = v.get("nombre", v["_id"])

    info = {}
    for doc in docs:
        bid = doc["_id"]
        vendedor_id = doc.get("vendedor_id", VENDEDOR_LOCAL) or VENDEDOR_LOCAL
        info[str(bid)] = {
            "valor_boleta": valor_boleta,
            "vendedor_id": vendedor_id,
            "vendedor_nombre": vid_cache.get(vendedor_id, VENDEDOR_LOCAL),
        }
    return info


def validar_form_abono(form: dict) -> tuple[dict, int, list[int], list[int], list[str]]:
    """Normalize an abono form and return (form_data, valor, boletas, not_found, errors)."""
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
        errors.append("La fecha debe tener formato válido (aaaa-mm-dd).")

    if form_data["metodo"] not in METODOS_PAGO:
        errors.append("Selecciona un m\u00e9todo de pago v\u00e1lido.")

    if form_data["metodo"] == METODO_TRANSFERENCIA and not form_data["referencia"]:
        errors.append("La referencia bancaria es obligatoria para transferencias.")

    boleta_ids, invalid, out_of_range, duplicadas = parse_boletas_detailed(form_data["boletas"])
    if invalid:
        errors.append("Hay entradas no num\u00e9ricas: " + ", ".join(invalid[:8]))
    incompletas = boletas_incompletas(form_data["boletas"])
    if incompletas:
        errors.append("Hay boletas incompletas, escribe los 4 d\u00edgitos: " + ", ".join(incompletas[:8]))
    if out_of_range:
        errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
    if not boleta_ids:
        errors.append("Ingresa al menos una boleta v\u00e1lida.")

    return form_data, valor_abono, boleta_ids, duplicadas, errors


def build_abono_preview(form: dict, factura_id: int | None = None, confirmar_pagadas: bool = False) -> tuple[dict, dict]:
    """Validate an abono form and return a preview with per-ticket results.

    Tickets already `pagada` are skipped unless `confirmar_pagadas` is set,
    in which case the payment is accepted as excedente (estado stays pagada).
    """
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

    # Tope por pago individual: un solo abono no puede superar el valor de la
    # boleta. El acumulado sí puede excederlo (varios abonos parciales).
    if valor_abono > valor_boleta:
        preview["errors"].append(f"El abono de ${valor_abono:,} supera el valor de la boleta (${valor_boleta:,}).")
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
        used_refs = list(boletas.find(_pago_match(elem_match), {"_id": 1}).limit(10))
        preview["referencias_usadas"] = [doc["_id"] for doc in used_refs]
        if used_refs:
            preview["errors"].append("La referencia bancaria ya existe en otro pago.")

    for number in boleta_ids:
        doc = docs_by_id.get(number)
        if not doc:
            continue
        if doc.get("estado") == "pagada" and not confirmar_pagadas:
            preview["pagadas"].append(doc)
            continue

        nuevo_total = int(doc.get("total_abonado", 0) or 0) + valor_abono
        doc["nuevo_total"] = nuevo_total
        doc["nuevo_estado"] = estado_para_total(
            nuevo_total,
            valor_boleta,
            vendedor_id=doc.get("vendedor_id"),
            cliente_nombre=(doc.get("cliente") or {}).get("nombre", ""),
        )
        preview["validas"].append(doc)

    if duplicadas:
        preview["warnings"].append("Se ignorar\u00e1n n\u00fameros duplicados del bloque.")
    if preview["inexistentes"]:
        preview["warnings"].append("Las boletas inexistentes no ser\u00e1n modificadas.")
    if preview["pagadas"]:
        preview["warnings"].append("Las boletas ya pagadas se omitirán (confirme para registrarlas como excedente).")

    if not preview["validas"]:
        preview["errors"].append("No hay boletas disponibles para registrar este abono.")

    preview["can_confirm"] = bool(preview["validas"]) and not preview["errors"]
    return form_data, preview


def registrar_abono_lote(
    boleta_ids: list[int], form_data: dict, valor_abono: int, factura_id: int | None = None, permitir_pagadas: bool = False
) -> BulkWriteResult:
    """Register the same abono on many tickets atomically (checks duplicates/overpay).

    With `permitir_pagadas`, tickets already `pagada` also receive the payment
    (excedente); otherwise they are skipped by the atomic filter.
    """
    if not boleta_ids:
        raise ValueError("No hay boletas para registrar el abono.")
    boleta_ids = list(dict.fromkeys(boleta_ids))
    if valor_abono <= 0:
        raise ValueError("El valor del abono debe ser mayor que cero.")
    config = get_config()
    valor_boleta = int(config["valor_boleta"])
    if valor_abono > valor_boleta:
        raise ValueError(f"El abono de ${valor_abono:,} supera el valor de la boleta (${valor_boleta:,}).")

    ref = ""
    banco = ""
    if form_data["metodo"] == METODO_TRANSFERENCIA:
        ref = form_data.get("referencia", "").strip()
        if not ref:
            raise ValueError("La referencia bancaria es obligatoria para transferencias.")
        banco = form_data.get("banco", "").strip()
        duplicado = buscar_transferencia_duplicada(ref, banco, exclude_factura_id=factura_id)
        if duplicado:
            msg = f"Ya existe un pago por transferencia con referencia {ref}"
            if banco:
                msg += f" y banco {banco}"
            msg += f" (boleta #{duplicado['_id']:04d})."
            raise ValueError(msg)
        pago = {
            "tipo": MOV_PAGO,
            "fecha": form_data["fecha"],
            "valor": valor_abono,
            "metodo": METODO_TRANSFERENCIA,
            "referencia": ref,
            "registrado_en": now_local(),
            "usuario": (current_user() or {}).get("username", USUARIO_SISTEMA),
        }
        if banco:
            pago["banco"] = banco
    elif form_data["metodo"] == METODO_PAGO_DELIO:
        pago = {
            "tipo": MOV_PAGO,
            "fecha": form_data["fecha"],
            "valor": valor_abono,
            "metodo": METODO_PAGO_DELIO,
            "referencia": "SIN REGISTRO",
            "registrado_en": now_local(),
            "usuario": (current_user() or {}).get("username", USUARIO_SISTEMA),
        }
    else:
        pago = {
            "tipo": MOV_PAGO,
            "fecha": form_data["fecha"],
            "valor": valor_abono,
            "metodo": METODO_EFECTIVO,
            "registrado_en": now_local(),
            "usuario": (current_user() or {}).get("username", USUARIO_SISTEMA),
        }
    if factura_id is not None:
        pago["factura_id"] = factura_id
    elif boleta_ids:
        # Generar ID temporal único para rollback parcial.
        pago["_temp_batch_id"] = f"batch_{uuid.uuid4().hex[:8]}"
    # El filtro atómico solo excluye boletas ya pagadas (salvo confirmación
    # expresa): el acumulado de abonos sí puede superar el valor de la
    # boleta (el tope es por pago individual, validado arriba).
    filtro: dict = {"_id": {"$in": boleta_ids}}
    if not permitir_pagadas:
        filtro["estado"] = {"$ne": "pagada"}
    result = boletas.update_many(
        filtro,
        [
            {
                "$set": {
                    MOVIMIENTOS_FIELD: {"$concatArrays": [{"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}, {"$literal": [pago]}]},
                    "total_abonado": {"$add": [{"$ifNull": ["$total_abonado", 0]}, valor_abono]},
                }
            },
            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
        ],
    )
    if result.matched_count < len(boleta_ids):
        omitidas = len(boleta_ids) - result.matched_count
        if factura_id is not None:
            # Revertir pagos parciales de la factura y notificar al usuario.
            rollback_pagos_por_factura(factura_id, valor_boleta)
            raise ValueError(
                f"{omitidas} boleta(s) cambiaron de estado mientras se registraba (ya pagadas). Se revirtieron los pagos de la factura, intente de nuevo."
            )
        # Revertir pagos parciales usando el batch_id temporal.
        temp_id = pago.get("_temp_batch_id")
        if temp_id:
            _rollback_temp_batch(temp_id, valor_boleta)
        raise ValueError(f"{omitidas} boleta(s) no se actualizaron (ya pagadas). Se revirtieron los pagos aplicados, intente de nuevo.")
    # Verificación post-escritura: si otra factura registró la misma referencia
    # entre nuestro pre-chequeo y el update, revertimos y fallamos (exactly-once).
    if factura_id is not None and form_data["metodo"] == METODO_TRANSFERENCIA:
        conflicto = buscar_transferencia_duplicada(ref, banco, exclude_factura_id=factura_id)
        if conflicto:
            rollback_pagos_por_factura(factura_id, valor_boleta)
            raise ValueError(f"La referencia {ref} fue registrada por otra factura mientras se procesaba el pago. Se revirtieron los pagos, intente de nuevo.")
    # Limpiar _temp_batch_id de los documentos afectados.
    temp_id = pago.get("_temp_batch_id")
    if temp_id:
        boletas.update_many(
            {MOVIMIENTOS_FIELD + "._temp_batch_id": temp_id},
            {"$unset": {MOVIMIENTOS_FIELD + "._temp_batch_id": ""}},
        )
    invalidate_dashboard_cache()
    return result


def rollback_pagos_por_factura(factura_id: int, valor_boleta: int) -> None:
    """Remove payments tied to a factura from tickets and recompute totals/estado."""
    movimientos = {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}
    pipeline = [
        {
            "$set": {
                MOVIMIENTOS_FIELD: {
                    "$filter": {
                        "input": movimientos,
                        "cond": {
                            "$not": [
                                {
                                    "$and": [
                                        {"$eq": [{"$ifNull": ["$$this.factura_id", None]}, factura_id]},
                                        {"$eq": [{"$ifNull": ["$$this.tipo", MOV_PAGO]}, MOV_PAGO]},
                                    ]
                                }
                            ]
                        },
                    }
                }
            }
        },
        {"$set": {"total_abonado": movimiento_neto_expr()}},
    ]
    if valor_boleta is not None:
        pipeline.append({"$set": {"estado": estado_pipeline_expr(valor_boleta)}})
    boletas.update_many(
        {MOVIMIENTOS_FIELD + ".factura_id": factura_id},
        pipeline,
    )
    invalidate_dashboard_cache()


def _rollback_temp_batch(temp_batch_id: str, valor_boleta: int) -> None:
    """Remove movements tagged with a temporary batch id and recompute totals."""
    movimientos = {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}
    pipeline = [
        {
            "$set": {
                MOVIMIENTOS_FIELD: {
                    "$filter": {
                        "input": movimientos,
                        "cond": {"$ne": ["$$this._temp_batch_id", temp_batch_id]},
                    }
                }
            }
        },
        {"$set": {"total_abonado": movimiento_neto_expr()}},
    ]
    if valor_boleta is not None:
        pipeline.append({"$set": {"estado": estado_pipeline_expr(valor_boleta)}})
    boletas.update_many(
        {MOVIMIENTOS_FIELD + "._temp_batch_id": temp_batch_id},
        pipeline,
    )
    invalidate_dashboard_cache()


def next_factura_id() -> int:
    """Return the next invoice id, skipping ids already present.

    The stored counter can drift below existing ids after a DB restore/import
    (e.g. ``facturas`` restored without a matching ``factura_counter``); in that
    case we keep incrementing until a free id is found (self-healing).
    """
    for _retry in range(50):
        result = configuracion.find_one_and_update(
            {"_id": "rifa"},
            {"$inc": {"factura_counter": 1}},
            upsert=True,
            return_document=True,
        )
        candidate = int(result["factura_counter"] if result else 1)
        if facturas.count_documents({"_id": candidate}) == 0:
            return candidate
    raise RuntimeError("No se pudo obtener un id de factura libre tras 50 intentos.")

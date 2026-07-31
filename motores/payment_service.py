from datetime import datetime

from flask import current_app
from pymongo.results import BulkWriteResult

from database import boletas, configuracion
from motores.auth import current_user
from motores.cache import invalidate_dashboard_cache
from motores.config_service import get_config, require_collections
from motores.constants import (
    METODO_EFECTIVO,
    METODO_TRANSFERENCIA,
    METODOS_PAGO,
    USUARIO_SISTEMA,
)
from motores.fechas import now_local
from motores.ticket_service import estado_para_total, estado_pipeline_expr
from motores.validacion import parse_boletas_detailed, parse_money


def buscar_transferencia_duplicada(ref: str, banco: str = "", exclude_factura_id: int | None = None) -> dict | None:
    """Check if a transfer reference+banco was already used in another payment."""
    elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": ref}
    if banco:
        elem_match["banco"] = banco
    if exclude_factura_id is not None:
        elem_match["factura_id"] = {"$ne": exclude_factura_id}
    return boletas.find_one({"historial_pagos": {"$elemMatch": elem_match}}, {"_id": 1})


def build_factura_detalle(boleta_ids: list[int], factura_id: int) -> list[dict]:
    """Build invoice detail lines from historial_pagos of given tickets."""
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
        errors.append("La fecha debe tener formato AAAA-MM-DD.")

    if form_data["metodo"] not in METODOS_PAGO:
        errors.append("Selecciona un m\u00e9todo de pago v\u00e1lido.")

    if form_data["metodo"] == METODO_TRANSFERENCIA and not form_data["referencia"]:
        errors.append("La referencia bancaria es obligatoria para transferencias.")

    boleta_ids, invalid, out_of_range, duplicadas = parse_boletas_detailed(form_data["boletas"])
    if invalid:
        errors.append("Hay entradas no num\u00e9ricas: " + ", ".join(invalid[:8]))
    if out_of_range:
        errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
    if not boleta_ids:
        errors.append("Ingresa al menos una boleta v\u00e1lida.")

    return form_data, valor_abono, boleta_ids, duplicadas, errors


def build_abono_preview(form: dict, factura_id: int | None = None) -> dict:
    """Validate an abono form and return a preview with per-ticket results."""
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
            preview["excesos"] = [*preview.get("excesos", []), doc]
            continue
        doc["nuevo_total"] = nuevo_total
        doc["nuevo_estado"] = estado_para_total(nuevo_total, valor_boleta)
        preview["validas"].append(doc)

    if duplicadas:
        preview["warnings"].append("Se ignorar\u00e1n n\u00fameros duplicados del bloque.")
    if preview["inexistentes"]:
        preview["warnings"].append("Las boletas inexistentes no ser\u00e1n modificadas.")
    if preview["pagadas"]:
        preview["warnings"].append("Las boletas ya pagadas se omitir\u00e1n.")

    if preview.get("excesos"):
        preview["warnings"].append(f"Se omitieron {len(preview['excesos'])} boleta(s) porque el abono supera su saldo pendiente.")
    if not preview["validas"]:
        preview["errors"].append("No hay boletas disponibles para registrar este abono.")

    preview["can_confirm"] = bool(preview["validas"]) and not preview["errors"]
    return form_data, preview


def registrar_abono_lote(boleta_ids: list[int], form_data: dict, valor_abono: int, factura_id: int | None = None) -> BulkWriteResult:
    """Register the same abono on many tickets atomically (checks duplicates/overpay)."""
    config = get_config()
    valor_boleta = int(config["valor_boleta"])

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
        sobrepasan = boletas.count_documents(
            {
                "_id": {"$in": boleta_ids},
                "total_abonado": {"$gt": valor_boleta - valor_abono},
            }
        )
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
            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
        ],
    )
    if result.modified_count < len(boleta_ids):
        skipped = len(boleta_ids) - result.modified_count
        current_app.logger.warning("registrar_abono_lote: %d boleta(s) no se actualizaron (probablemente ya estaban pagadas)", skipped)
    invalidate_dashboard_cache()
    return result


def rollback_pagos_por_factura(factura_id: int, valor_boleta: int) -> None:
    """Remove payments tied to a factura from tickets and recompute totals/estado."""
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


def next_factura_id() -> int:
    """Atomically increment and return the next invoice id from configuracion."""
    result = configuracion.find_one_and_update(
        {"_id": "rifa"},
        {"$inc": {"factura_counter": 1}},
        upsert=True,
        return_document=True,
    )
    return result["factura_counter"] if result else 1

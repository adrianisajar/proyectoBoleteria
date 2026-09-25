import contextlib
import re
from datetime import datetime

from flask import Flask, Response
from pymongo import UpdateOne
from werkzeug.exceptions import BadRequest

from motores.constants import (
    BOLETA_MAX,
    BOLETA_MIN,
    METODO_TRANSFERENCIA,
    MOV_EGRESO,
    MOV_PAGO,
    MOVIMIENTOS_FIELD,
    OPERACIONES_VENDEDOR,
    TIPOS_EGRESO,
    VENDEDOR_LOCAL,
    VENDEDOR_LOCAL_LABEL,
    VENDEDOR_SIN_ASIGNAR,
)
from motores.errores import safe_error_message
from motores.fechas import now_local
from motores.shared import (
    boletas,
    estado_pipeline_expr,
    flash,
    get_config,
    invalidate_dashboard_cache,
    jsonify,
    next_vendedor_id,
    normalize_vendedor_id,
    redirect,
    render_template,
    request,
    require_collections,
    reservas,
    role_required,
    safe_vendedores_snapshot,
    url_for,
    vendedores,
)
from motores.validacion import boletas_incompletas, parse_boletas, sanitizar_texto
from motores.validacion import safe_error_message as _safe_flash_error


def _actualizar_estado_boletas(
    filtro: dict,
    nuevo_vendedor_id: str,
    valor_boleta: int,
    limpiar_cliente: bool = False,
    fecha_adquisicion: str | None = None,
    limpiar_fecha: bool = False,
) -> None:
    """Reassign tickets matching filtro to a vendor and recalc their estado.

    With limpiar_cliente=True the buyer data is also wiped, so released
    tickets fall back to `disponible` instead of `separada`. With
    fecha_adquisicion set, the date travels with the transfer (explicit
    date always overwrites). When removing a vendor (VENDEDOR_SIN_ASIGNAR)
    without an explicit date, fecha_adquisicion is always cleared.
    """
    pipeline = [
        {"$set": {"vendedor_id": nuevo_vendedor_id}},
    ]
    if limpiar_cliente:
        pipeline.append({"$set": {"cliente": {"nombre": "", "telefono": "", "direccion": ""}}})

    # Date logic: explicit date overwrites; removal without date clears;
    # limpiar_fecha explicitly clears.
    if fecha_adquisicion:
        pipeline.append({"$set": {"fecha_adquisicion": fecha_adquisicion}})
    elif limpiar_fecha or nuevo_vendedor_id == VENDEDOR_SIN_ASIGNAR:
        pipeline.append({"$set": {"fecha_adquisicion": None}})

    pipeline.append({"$set": {"estado": estado_pipeline_expr(valor_boleta)}})
    boletas.update_many(filtro, pipeline)


VENDEDORES_SORT_WHITELIST = {"_id", "nombre", "cantidad", "vendidas", "pendientes_fisicas", "recaudado"}
VENDEDORES_SORT_NUMERICOS = {"cantidad", "vendidas", "pendientes_fisicas", "recaudado"}


def _render_vendedores(form_data: dict) -> str:
    """Render the vendor panel with the current snapshot and the submitted form data."""
    vendedores_lista, resumen = safe_vendedores_snapshot()
    sort_by = request.args.get("sort_by", "_id").strip()
    sort_dir = request.args.get("sort_dir", "asc").strip()
    if sort_by not in VENDEDORES_SORT_WHITELIST:
        sort_by = "_id"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"

    def _sort_key(vendedor: dict):
        if sort_by in VENDEDORES_SORT_NUMERICOS:
            return int(vendedor.get(sort_by) or 0)
        if sort_by == "nombre":
            return str(vendedor.get("nombre") or vendedor.get("_id") or "").lower()
        return str(vendedor.get(sort_by) or "").lower()

    vendedores_lista = sorted(vendedores_lista, key=_sort_key, reverse=(sort_dir == "desc"))
    return render_template(
        "vendedores.html",
        form=form_data,
        vendedores_lista=vendedores_lista,
        resumen=resumen,
        now_local_date=now_local().strftime("%Y-%m-%d"),
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


def _validar_form_vendedor(form_data: dict) -> tuple[str, list[int], list[str]]:
    """Validate the vendor form and return (vendedor_id, boleta_ids, errors)."""
    errors = []
    vendedor_id = ""
    try:
        require_collections()
        raw_id = form_data.get("vendedor_id", "").strip()
        if not raw_id:
            if form_data["operacion"] == "guardar":
                raw_id = next_vendedor_id()
            else:
                raise ValueError("Selecciona un vendedor existente de la lista de sugerencias.")
        vendedor_id = normalize_vendedor_id(raw_id)
        form_data["vendedor_id"] = vendedor_id
    except (RuntimeError, ValueError) as exc:
        errors.append(str(exc))

    if form_data["operacion"] not in OPERACIONES_VENDEDOR:
        errors.append("Selecciona una operaci\u00f3n v\u00e1lida para el vendedor.")

    boleta_ids = []
    if form_data["operacion"] in {"asignar", "quitar"}:
        boleta_ids, invalid, out_of_range = parse_boletas(form_data["boletas"])
        if invalid:
            errors.append("Hay entradas no num\u00e9ricas: " + ", ".join(invalid[:8]))
        if boletas_incompletas(form_data["boletas"]):
            incompletas = boletas_incompletas(form_data["boletas"])
            errors.append("Hay boletas incompletas, escribe los 4 d\u00edgitos: " + ", ".join(incompletas[:8]))
        if out_of_range:
            errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
        if not boleta_ids:
            errors.append("Ingresa al menos una boleta para esta operaci\u00f3n.")

    if form_data["operacion"] == "asignar" and form_data.get("fecha_asignacion"):
        try:
            fecha_dt = datetime.strptime(form_data["fecha_asignacion"], "%Y-%m-%d")
        except ValueError:
            errors.append("La fecha de adquisición debe tener formato aaaa-mm-dd.")
        else:
            if fecha_dt.date() > now_local().date():
                errors.append("La fecha de adquisición no puede ser posterior a hoy.")

    return vendedor_id, boleta_ids, errors


def _procesar_guardar(vendedor_id: str, perfil_update: dict) -> None:
    """Create or update a vendor profile with the given $set document."""
    if vendedor_id == VENDEDOR_LOCAL:
        raise ValueError("LOCAL es un vendedor del sistema y no se puede editar.")
    existe = vendedores.find_one({"_id": vendedor_id}, {"_id": 1})
    vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)
    if existe:
        flash(f"Vendedor {vendedor_id} actualizado.", "info")
    else:
        flash(f"Vendedor {vendedor_id} creado.", "success")


def _boletas_con_pagos(ids: list[int]) -> list[int]:
    """Return list of ticket ids that have payments (total_abonado > 0)."""
    return [b["_id"] for b in boletas.find({"_id": {"$in": ids}, "total_abonado": {"$gt": 0}}, {"_id": 1})]


def _procesar_asignar(
    vendedor_id: str,
    boleta_ids: list[int],
    perfil_update: dict,
    valor_boleta: int,
    forzar_con_pagos: bool = False,
    fecha_adquisicion: str = "",
) -> None:
    """Assign tickets to a vendor, handling reasignment and paid tickets on confirmation.

    An explicit fecha_adquisicion travels with the transfer (overwrites any
    previous date). When empty, tickets changing owner get a blank date
    (the old one belonged to the previous holder); the rest keep theirs.
    """
    docs = {
        d["_id"]: d
        for d in boletas.find(
            {"_id": {"$in": boleta_ids}},
            {"_id": 1, "vendedor_id": 1, "total_abonado": 1},
        )
    }
    existentes = [b for b in boleta_ids if b in docs]
    faltantes = len(boleta_ids) - len(existentes)

    if not existentes:
        flash("No se encontraron boletas v\u00e1lidas para asignar.", "warning")
        return

    con_pagos = [b for b in existentes if (docs[b].get("total_abonado") or 0) > 0]
    if con_pagos and not forzar_con_pagos:
        ids_str = ", ".join(f"#{b:04d}" for b in con_pagos)
        raise ValueError(
            f"No se pueden asignar boletas con pagos registrados: {ids_str}. Confirme la operación para reasignarlas con sus pagos al nuevo vendedor."
        )

    fecha_adquisicion = (fecha_adquisicion or "").strip()
    if fecha_adquisicion:
        try:
            fecha_dt = datetime.strptime(fecha_adquisicion, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("La fecha de adquisición debe tener formato aaaa-mm-dd.") from exc
        if fecha_dt.date() > now_local().date():
            raise ValueError("La fecha de adquisición no puede ser posterior a hoy.")

    de_otro = {b: docs[b]["vendedor_id"] for b in existentes if (docs[b].get("vendedor_id") or "") not in ("", vendedor_id)}
    if de_otro:
        detalles = ", ".join(f"#{b:04d} → {o}" for b, o in de_otro.items())
        flash(f"Atención: estas boletas pertenecen a otro vendedor y serán reasignadas: {detalles}", "warning")

    old_vendor_ids = list({docs[b].get("vendedor_id") for b in existentes if docs[b].get("vendedor_id") and docs[b].get("vendedor_id") != VENDEDOR_LOCAL})
    if old_vendor_ids:
        vendedores.update_many(
            {"_id": {"$in": old_vendor_ids}},
            {"$pull": {"boletas_asignadas": {"$in": existentes}}},
        )
    if vendedor_id != VENDEDOR_LOCAL:
        vendedores.update_one(
            {"_id": vendedor_id},
            {
                "$set": perfil_update["$set"],
                "$addToSet": {"boletas_asignadas": {"$each": existentes}},
            },
            upsert=True,
        )
    if fecha_adquisicion:
        _actualizar_estado_boletas(
            {"_id": {"$in": existentes}},
            vendedor_id,
            valor_boleta,
            fecha_adquisicion=fecha_adquisicion,
        )
    else:
        # Sin fecha: las boletas que venían de otro vendedor quedan sin fecha
        # (la anterior era del dueño anterior). Las que ya eran del vendedor
        # o vienen de stock conservan la suya.
        cambian = {b for b in existentes if (docs[b].get("vendedor_id") or "") not in ("", None, VENDEDOR_LOCAL, vendedor_id)}
        if cambian:
            _actualizar_estado_boletas(
                {"_id": {"$in": sorted(cambian)}},
                vendedor_id,
                valor_boleta,
                limpiar_fecha=True,
            )
        iguales = [b for b in existentes if b not in cambian]
        if iguales:
            _actualizar_estado_boletas({"_id": {"$in": iguales}}, vendedor_id, valor_boleta)
    invalidate_dashboard_cache()
    mensaje = f"{len(existentes)} boleta(s) asignada(s) a {vendedor_id}."
    if fecha_adquisicion:
        mensaje += f" Fecha de adquisición {fecha_adquisicion} registrada."
    if con_pagos:
        mensaje += f" {len(con_pagos)} con pagos registrados conservaron sus abonos."
    if faltantes:
        mensaje += f" {faltantes} no exist\u00edan en la colecci\u00f3n boletas."
    flash(mensaje, "success")


def _procesar_quitar(vendedor_id: str, boleta_ids: list[int], perfil_update: dict, valor_boleta: int) -> None:
    """Remove ticket assignments from a vendor, rejecting foreign or paid tickets."""
    docs = {
        d["_id"]: d
        for d in boletas.find(
            {"_id": {"$in": boleta_ids}},
            {"_id": 1, "vendedor_id": 1, "total_abonado": 1},
        )
    }
    existentes = [b for b in boleta_ids if b in docs]
    faltantes = len(boleta_ids) - len(existentes)

    if vendedor_id != VENDEDOR_LOCAL and not vendedores.find_one({"_id": vendedor_id}, {"_id": 1}):
        flash(f"El vendedor {vendedor_id} no existe.", "danger")
        return

    if not existentes:
        flash("No se encontraron boletas v\u00e1lidas para quitar.", "warning")
        return

    # Validate: tickets must belong to this vendor
    ajenas = [b for b in existentes if (docs[b].get("vendedor_id") or "") != vendedor_id]
    if ajenas:
        ids_ajenas = ", ".join(f"#{b:04d}" for b in ajenas)
        raise ValueError(f"Las siguientes boletas no pertenecen a {vendedor_id}: {ids_ajenas}. Solo puedes quitar boletas asignadas a este vendedor.")

    # Validate: cannot remove tickets with payments
    con_pagos = [b for b in existentes if (docs[b].get("total_abonado") or 0) > 0]
    if con_pagos:
        ids_str = ", ".join(f"#{b:04d}" for b in con_pagos)
        raise ValueError(f"No se pueden quitar boletas con pagos registrados: {ids_str}")

    if vendedor_id != VENDEDOR_LOCAL:
        vendedores.update_one({"_id": vendedor_id}, {"$pull": {"boletas_asignadas": {"$in": existentes}}})
    _actualizar_estado_boletas({"_id": {"$in": existentes}, "vendedor_id": vendedor_id}, VENDEDOR_SIN_ASIGNAR, valor_boleta)
    invalidate_dashboard_cache()
    mensaje = f"{len(existentes)} boleta(s) quitada(s) de {vendedor_id}."
    if faltantes:
        mensaje += f" {faltantes} no exist\u00edan en la colecci\u00f3n boletas."
    flash(mensaje, "success")


def _procesar_eliminar(vendedor_id: str, valor_boleta: int) -> None:
    """Delete a vendor after releasing their assigned tickets (blocks paid tickets)."""
    if vendedor_id == VENDEDOR_LOCAL:
        raise ValueError("LOCAL es un vendedor del sistema y no se puede eliminar.")
    vendedor_doc = vendedores.find_one({"_id": vendedor_id}, {"_id": 1, "nombre": 1, "boletas_asignadas": 1})
    if not vendedor_doc:
        flash(f"El vendedor {vendedor_id} no existe.", "danger")
        return

    asignadas_doc = [num for num in vendedor_doc.get("boletas_asignadas", []) if isinstance(num, int) and BOLETA_MIN <= num <= BOLETA_MAX]
    por_vendedor_id = [d["_id"] for d in boletas.find({"vendedor_id": vendedor_id}, {"_id": 1})]
    boletas_ids_vendor = sorted(set(asignadas_doc) | set(por_vendedor_id))

    # Validate: cannot delete vendor if they have tickets with payments
    if boletas_ids_vendor:
        con_pagos = _boletas_con_pagos(boletas_ids_vendor)
        if con_pagos:
            ids_str = ", ".join(f"#{b:04d}" for b in con_pagos)
            raise ValueError(
                f"No se puede eliminar el vendedor porque tiene boletas con pagos registrados: {ids_str}. "
                "Transfiérelas a otro vendedor o procesa los pagos antes de eliminar."
            )

    if boletas_ids_vendor:
        _actualizar_estado_boletas(
            {"_id": {"$in": boletas_ids_vendor}, "vendedor_id": vendedor_id},
            VENDEDOR_SIN_ASIGNAR,
            valor_boleta,
            limpiar_cliente=True,
        )
    vendedores.delete_one({"_id": vendedor_id})
    invalidate_dashboard_cache()
    flash(f"Vendedor {vendedor_id} ({vendedor_doc.get('nombre', '')}) eliminado con {len(boletas_ids_vendor)} boleta(s) liberada(s).", "success")


def _procesar_cambiar_nombre(vendedor_id: str, nombre_nuevo: str) -> None:
    """Rename a vendor (id unchanged; tickets and invoice snapshots untouched)."""
    if vendedor_id == VENDEDOR_LOCAL:
        raise ValueError("LOCAL es un vendedor del sistema y no se puede renombrar.")
    vendedor_doc = vendedores.find_one({"_id": vendedor_id}, {"_id": 1, "nombre": 1})
    if not vendedor_doc:
        raise ValueError(f"El vendedor {vendedor_id} no existe.")
    nombre_nuevo = sanitizar_texto(nombre_nuevo, "name")
    if not nombre_nuevo:
        raise ValueError("El nombre nuevo no puede estar vacío.")
    if nombre_nuevo == (vendedor_doc.get("nombre") or ""):
        flash("El nombre es igual al actual, sin cambios.", "info")
        return
    vendedores.update_one({"_id": vendedor_id}, {"$set": {"nombre": nombre_nuevo}})
    invalidate_dashboard_cache()
    flash(f"Vendedor {vendedor_id}: nombre cambiado a {nombre_nuevo}.", "success")


def _procesar_registrar_fecha(vendedor_id: str, form_data: dict) -> None:
    """Register the acquisition date on tickets that belong to the given vendor."""
    fecha_raw = form_data.get("fecha_adquisicion", "").strip()
    if not fecha_raw:
        raise ValueError("La fecha de adquisición es obligatoria.")
    try:
        fecha_dt = datetime.strptime(fecha_raw, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("La fecha de adquisición debe tener formato AAAA-MM-DD.") from exc
    if fecha_dt.date() > now_local().date():
        raise ValueError("La fecha de adquisición no puede ser posterior a hoy.")

    try:
        boleta_ids = [int(b) for b in form_data.getlist("boletas_fecha[]") if str(b).strip()]
    except (ValueError, TypeError) as exc:
        raise ValueError("Boleta(s) inválida(s) en la selección.") from exc
    if not boleta_ids:
        raise ValueError("Seleccione al menos una boleta del vendedor.")

    if vendedor_id != VENDEDOR_LOCAL and not vendedores.find_one({"_id": vendedor_id}, {"_id": 1}):
        raise ValueError(f"El vendedor {vendedor_id} no existe.")

    docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1, "vendedor_id": 1})}
    ajenas = [b for b in boleta_ids if (docs.get(b) or {}).get("vendedor_id", "") != vendedor_id]
    if ajenas:
        ids_ajenas = ", ".join(f"#{b:04d}" for b in sorted(ajenas))
        raise ValueError(f"No se pueden registrar fechas en boletas que no pertenecen a {vendedor_id}: {ids_ajenas}")
    existentes = [b for b in boleta_ids if b in docs]
    faltantes = len(boleta_ids) - len(existentes)

    result = boletas.update_many(
        {"_id": {"$in": existentes}, "vendedor_id": vendedor_id},
        {"$set": {"fecha_adquisicion": fecha_raw}},
    )
    invalidate_dashboard_cache()
    mensaje = f"Fecha de adquisición {fecha_raw} registrada en {result.modified_count} boleta(s) de {vendedor_id}."
    if faltantes:
        mensaje += f" {faltantes} no existían en la colección boletas."
    flash(mensaje, "success")


def register_routes(app: Flask) -> None:
    """Register the vendor panel, API and validation routes."""

    @app.route("/vendedores", methods=["GET", "POST"])
    @role_required("admin")
    def vendedores_panel() -> str | Response:
        """Vendor CRUD panel: create, assign/remove ticket blocks, delete."""
        config = get_config()
        valor_boleta = int(config["valor_boleta"])
        form_data = {
            "vendedor_id": "",
            "nombre": "",
            "telefono": "",
            "operacion": "",
            "boletas": "",
            "fecha_asignacion": "",
        }

        if request.method == "POST":
            form_data.update(
                {
                    "vendedor_id": request.form.get("vendedor_id", ""),
                    "nombre": sanitizar_texto(request.form.get("nombre", ""), "name"),
                    "telefono": sanitizar_texto(request.form.get("telefono", ""), "numbers"),
                    "operacion": request.form.get("operacion", "").strip().lower(),
                    "boletas": request.form.get("boletas", "").strip(),
                    "fecha_asignacion": request.form.get("fecha_asignacion", "").strip(),
                }
            )

            vendedor_id, boleta_ids, errors = _validar_form_vendedor(form_data)

            if errors:
                for error in errors:
                    flash(error, "danger")
                return _render_vendedores(form_data)

            perfil_set = {
                "nombre": form_data["nombre"],
                "telefono": form_data["telefono"],
            }
            perfil_update = {"$set": perfil_set, "$setOnInsert": {"boletas_asignadas": []}}

            try:
                operacion = form_data["operacion"]
                if operacion == "guardar":
                    _procesar_guardar(vendedor_id, perfil_update)
                elif operacion == "asignar":
                    forzar = request.form.get("confirmar_pagos", "") == "1"
                    _procesar_asignar(
                        vendedor_id,
                        boleta_ids,
                        perfil_update,
                        valor_boleta,
                        forzar_con_pagos=forzar,
                        fecha_adquisicion=form_data.get("fecha_asignacion", ""),
                    )
                elif operacion == "quitar":
                    _procesar_quitar(vendedor_id, boleta_ids, perfil_update, valor_boleta)
                elif operacion == "eliminar":
                    _procesar_eliminar(vendedor_id, valor_boleta)
                elif operacion == "cambiar_nombre":
                    _procesar_cambiar_nombre(vendedor_id, request.form.get("nuevo_nombre", ""))
                elif operacion == "registrar_fecha_adquisicion":
                    _procesar_registrar_fecha(vendedor_id, request.form)
            except Exception as exc:
                flash(_safe_flash_error(exc), "danger")
                return _render_vendedores(form_data)

            return redirect(
                url_for(
                    "vendedores_panel",
                    sort_by=request.args.get("sort_by", "_id"),
                    sort_dir=request.args.get("sort_dir", "asc"),
                )
            )

        return _render_vendedores(form_data)

    @app.route("/api/vendedores")
    @role_required("admin", "cajero")
    def api_vendedores() -> Response | tuple[Response, int]:
        """JSON autocomplete of vendors by id or name."""
        try:
            require_collections()
            q = request.args.get("q", "").strip()
            query = {}
            if q:
                query["$or"] = [
                    {"_id": {"$regex": re.escape(q), "$options": "i"}},
                    {"nombre": {"$regex": re.escape(q), "$options": "i"}},
                ]
            docs = list(vendedores.find(query, {"nombre": 1, "telefono": 1}).sort("_id", 1).limit(20))
            local_entry = {
                "_id": VENDEDOR_LOCAL,
                "nombre": VENDEDOR_LOCAL_LABEL,
                "telefono": "",
            }
            if not q or VENDEDOR_LOCAL.lower() in q.lower() or VENDEDOR_LOCAL_LABEL.lower() in q.lower():
                docs.insert(0, local_entry)
            return jsonify([{"_id": d["_id"], "nombre": d.get("nombre", ""), "telefono": d.get("telefono", "")} for d in docs])
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

    @app.route("/api/vendedores/<vendedor_id>/boletas")
    @role_required("admin", "cajero")
    def api_vendedor_boletas(vendedor_id: str) -> Response | tuple[Response, int]:
        """JSON list of a vendor's assigned tickets with state/amount/client."""
        try:
            require_collections()
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500
        try:
            include_movements = request.args.get("movimientos", "1") == "1"
            projection = {"_id": 1, "estado": 1, "total_abonado": 1, "cliente": 1, "fecha_adquisicion": 1}
            if include_movements:
                projection[MOVIMIENTOS_FIELD] = 1
            docs = list(
                boletas.find(
                    {"vendedor_id": vendedor_id},
                    projection,
                )
                .sort("_id", 1)
                .limit(5000)
            )
            boletas_list = []
            for d in docs:
                cliente = d.get("cliente") or {}
                egresos = [
                    {
                        "label": TIPOS_EGRESO.get(m.get("egreso_tipo"), m.get("egreso_tipo") or "Egreso"),
                        "valor": int(m.get("valor") or 0),
                    }
                    for m in (d.get(MOVIMIENTOS_FIELD) or [])
                    if m.get("tipo") == MOV_EGRESO
                ]
                boletas_list.append(
                    {
                        "numero": f"{d['_id']:04d}",
                        "estado": d.get("estado", "disponible"),
                        "abonado": int(d.get("total_abonado", 0) or 0),
                        "cliente": cliente.get("nombre", ""),
                        "fecha_adquisicion": d.get("fecha_adquisicion") or "",
                        "egresos": egresos,
                        "total_egresado": sum(e["valor"] for e in egresos),
                    }
                )
            return jsonify({"ok": True, "total": len(boletas_list), "boletas": boletas_list})
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

    @app.route("/api/validar-boletas-vendedor", methods=["POST"])
    @role_required("admin")
    def api_validar_boletas_vendedor() -> Response | tuple[Response, int]:
        """Pre-validate tickets for assign/remove operations (per-ticket result)."""
        try:
            data = request.get_json(force=True) or {}
            boletas_list = data.get("boletas", [])
            operacion = data.get("operacion", "").strip()
            vendedor_id = data.get("vendedor_id", "").strip()
        except BadRequest:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not isinstance(boletas_list, list) or operacion not in ("asignar", "quitar"):
            return jsonify({"ok": False, "error": "Par\u00e1metros inv\u00e1lidos."}), 400
        try:
            require_collections()
            int_ids = [int(b) for b in boletas_list if isinstance(b, int) and BOLETA_MIN <= b <= BOLETA_MAX]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Boleta(s) inv\u00e1lida(s)."}), 400
        if not int_ids:
            return jsonify({"ok": True, "resultados": []})
        docs = {
            d["_id"]: d
            for d in boletas.find(
                {"_id": {"$in": int_ids}},
                {"_id": 1, "vendedor_id": 1, "estado": 1, "total_abonado": 1},
            )
        }
        reservas_map = {}
        if reservas is not None:
            with contextlib.suppress(Exception):
                reservas_map = {
                    d["_id"]: (d.get("cliente") or {}).get("nombre", "") for d in reservas.find({"_id": {"$in": int_ids}}, {"_id": 1, "cliente.nombre": 1})
                }
        v_ids = {d["vendedor_id"] for d in docs.values() if d.get("vendedor_id") and d["vendedor_id"] not in ("", VENDEDOR_LOCAL)}
        v_nombres = {}
        if v_ids:
            for v in vendedores.find({"_id": {"$in": list(v_ids)}}, {"_id": 1, "nombre": 1}):
                v_nombres[v["_id"]] = v.get("nombre", v["_id"])
        resultados = []
        for b in int_ids:
            doc = docs.get(b)
            item = {"boleta": f"{b:04d}"}
            if not doc:
                item["ok"] = False
                item["error"] = "No existe"
            elif operacion == "asignar":
                if doc.get("total_abonado", 0) > 0:
                    item["ok"] = True
                    item["con_pagos"] = True
                    item["abonado"] = int(doc.get("total_abonado") or 0)
                    parts = [f"Tiene pagos (${int(doc.get('total_abonado') or 0):,}) - se reasignará con sus pagos"]
                    vid = doc.get("vendedor_id") or ""
                    if vid and vid not in ("", VENDEDOR_LOCAL, vendedor_id):
                        v_nom = v_nombres.get(vid, vid)
                        parts.append(f"Pertenece a {v_nom} ({vid})")
                    item["aviso"] = " | ".join(parts)
                elif doc.get("vendedor_id") and doc["vendedor_id"] not in ("", VENDEDOR_LOCAL, vendedor_id):
                    v_id = doc["vendedor_id"]
                    v_nom = v_nombres.get(v_id, v_id)
                    item["ok"] = True
                    item["aviso"] = f"Pertenece a {v_nom} ({v_id}) - será reasignada"
                else:
                    item["ok"] = True
                if b in reservas_map:
                    item["ok"] = True
                    reserva_aviso = f"Reservada para {reservas_map[b] or 'cliente fijo'} (número fijo)"
                    item["aviso"] = f"{item.get('aviso')} | {reserva_aviso}" if item.get("aviso") else reserva_aviso
            elif operacion == "quitar":
                if doc.get("vendedor_id") != vendedor_id:
                    item["ok"] = False
                    item["error"] = "No pertenece a este vendedor"
                elif doc.get("total_abonado", 0) > 0:
                    item["ok"] = False
                    item["error"] = "Tiene pagos registrados"
                else:
                    item["ok"] = True
            resultados.append(item)
        return jsonify({"ok": True, "resultados": resultados})

    @app.route("/api/validar-referencias-vendedor", methods=["POST"])
    @role_required("admin", "cajero")
    def api_validar_referencias_vendedor() -> Response | tuple[Response, int]:
        """Detect transfer references already used in other payments."""
        try:
            data = request.get_json(force=True) or {}
            rows = data.get("rows", [])
        except BadRequest:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not isinstance(rows, list):
            return jsonify({"ok": False, "error": "Par\u00e1metros inv\u00e1lidos."}), 400
        try:
            require_collections()
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500
        try:
            results = []
            ref_rows: list[tuple[int, str]] = []
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                metodo = (row.get("metodo") or "").strip()
                if metodo != METODO_TRANSFERENCIA:
                    continue
                ref = (row.get("referencia") or "").strip()
                if not ref:
                    continue
                ref_rows.append((i, ref))

            if ref_rows:
                unique_refs = list({ref for _, ref in ref_rows})
                existing_docs = {
                    doc["_id"]: doc
                    for doc in boletas.find(
                        {"historial_movimientos.referencia": {"$in": unique_refs}},
                        {"_id": 1, MOVIMIENTOS_FIELD: 1},
                    )
                }
                ref_to_boleta: dict[str, int] = {}
                for doc in existing_docs.values():
                    for mov in doc.get(MOVIMIENTOS_FIELD) or []:
                        if mov.get("tipo") in (None, MOV_PAGO) and mov.get("metodo") == METODO_TRANSFERENCIA and mov.get("referencia"):
                            ref_to_boleta[str(mov["referencia"]).strip()] = doc["_id"]
                for i, ref in ref_rows:
                    if ref in ref_to_boleta:
                        results.append(
                            {"index": i, "referencia": ref, "error": f"La referencia '{ref}' ya existe en otro pago (boleta #{ref_to_boleta[ref]:04d})."}
                        )

            return jsonify({"ok": True, "resultados": results})
        except Exception as e:
            return jsonify({"ok": False, "error": safe_error_message(e)}), 500

    def _resolve_vendedor(vid: str, nombre: str, cache: dict[str, str]) -> str | None:
        """Resolve a vendor by ID or name, creating a new vendor if needed.

        *cache* is a shared name→id mapping to avoid repeated DB lookups.
        Returns the vendor ID or None if the input is empty.
        """
        if vid and vid != VENDEDOR_LOCAL and vendedores.count_documents({"_id": vid}):
            return vid
        norm_name = re.sub(r"\s+", " ", nombre.strip()).upper()
        if not norm_name or norm_name == VENDEDOR_LOCAL:
            return None
        cache_key = norm_name
        if cache_key in cache:
            return cache[cache_key]
        existing = vendedores.find_one({"nombre": {"$regex": f"^{re.escape(norm_name)}$", "$options": "i"}})
        if existing:
            cache[cache_key] = existing["_id"]
            return existing["_id"]
        new_id = next_vendedor_id()
        vendedores.insert_one({"_id": new_id, "nombre": norm_name, "boletas_asignadas": []})
        cache[cache_key] = new_id
        return new_id

    def _bulk_resolve_vendedores(assignments_raw: list) -> list[dict]:
        """Resolve all vendor assignments in bulk with minimal DB queries.

        Pre-fetches existing vendors by ID and name in 1-2 queries,
        then resolves each row from in-memory maps. Creates new vendors as needed.
        """
        seen_ids = set()
        seen_names = set()
        for item in assignments_raw:
            if not isinstance(item, dict):
                continue
            vid = (item.get("vendedor_id") or "").strip()
            vnombre = (item.get("vendedor_nombre") or "").strip()
            if vid and vid != VENDEDOR_LOCAL:
                seen_ids.add(vid)
            norm = re.sub(r"\s+", " ", vnombre.strip()).upper()
            if norm and norm != VENDEDOR_LOCAL:
                seen_names.add(norm)

        id_map: dict[str, bool] = {}
        if seen_ids:
            for doc in vendedores.find({"_id": {"$in": list(seen_ids)}}, {"_id": 1}):
                id_map[doc["_id"]] = True

        name_map: dict[str, str] = {}
        if seen_names:
            regex_patterns = [f"^{re.escape(n)}$" for n in seen_names]
            for doc in vendedores.find({"nombre": {"$in": [{"$regex": p, "$options": "i"} for p in regex_patterns]}}, {"_id": 1, "nombre": 1}):
                norm = re.sub(r"\s+", " ", doc.get("nombre", "")).upper()
                name_map[norm] = doc["_id"]

        cache: dict[str, str] = dict(name_map)
        results = []
        for item in assignments_raw:
            if not isinstance(item, dict):
                continue
            vid = (item.get("vendedor_id") or "").strip()
            vnombre = (item.get("vendedor_nombre") or "").strip()
            boleta = item.get("boleta")
            if boleta is None:
                continue
            try:
                b = int(boleta)
            except (ValueError, TypeError):
                continue
            if not (BOLETA_MIN <= b <= BOLETA_MAX):
                continue

            if vid and vid != VENDEDOR_LOCAL and id_map.get(vid):
                results.append({"vendedor_id": vid, "boleta": b})
                continue

            norm_name = re.sub(r"\s+", " ", vnombre.strip()).upper()
            if not norm_name or norm_name == VENDEDOR_LOCAL:
                continue

            if norm_name in cache:
                results.append({"vendedor_id": cache[norm_name], "boleta": b})
                continue

            new_id = next_vendedor_id()
            vendedores.insert_one({"_id": new_id, "nombre": norm_name, "boletas_asignadas": []})
            cache[norm_name] = new_id
            results.append({"vendedor_id": new_id, "boleta": b})

        return results

    @app.route("/vendedores/api/asignar-rapido", methods=["POST"])
    @role_required("admin")
    def api_asignar_rapido() -> Response:
        """Bulk-assign tickets to vendors via AJAX (quick-entry tab)."""
        try:
            data = request.get_json(force=True) or {}
            assignments_raw = data.get("assignments", [])
        except BadRequest:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400

        if not isinstance(assignments_raw, list) or not assignments_raw:
            return jsonify({"ok": False, "error": "Ingresa al menos una asignacion."}), 400

        try:
            require_collections()
            config = get_config()
            valor_boleta = int(config["valor_boleta"])

            assignments = _bulk_resolve_vendedores(assignments_raw)

            if not assignments:
                return jsonify({"ok": False, "error": "No se detectaron asignaciones validas."}), 400

            boleta_ids = sorted({a["boleta"] for a in assignments})
            docs = {
                d["_id"]: d
                for d in boletas.find(
                    {"_id": {"$in": boleta_ids}},
                    {"_id": 1, "vendedor_id": 1, "total_abonado": 1},
                )
            }

            warnings = []
            skipped = []
            valid = []

            for a in assignments:
                b = a["boleta"]
                vid = a["vendedor_id"]
                doc = docs.get(b)
                if not doc:
                    skipped.append(f"#{b:04d} no existe")
                    continue
                if (doc.get("total_abonado") or 0) > 0:
                    skipped.append(f"#{b:04d} tiene pagos")
                    continue
                old_vid = doc.get("vendedor_id") or ""
                if old_vid and old_vid != vid and old_vid != VENDEDOR_LOCAL:
                    warnings.append(f"#{b:04d} reasignada de {old_vid} a {vid}")
                valid.append({"boleta": b, "vendedor_id": vid})

            if valid:
                boleta_ids_to_update = [v["boleta"] for v in valid]

                old_vendors = {
                    d["vendedor_id"]
                    for d in vendedores.find(
                        {"boletas_asignadas": {"$in": boleta_ids_to_update}},
                        {"_id": 1},
                    )
                    if d.get("vendedor_id") and d.get("vendedor_id") != VENDEDOR_LOCAL
                }
                if old_vendors:
                    vendedores.update_many(
                        {"_id": {"$in": list(old_vendors)}},
                        {"$pull": {"boletas_asignadas": {"$in": boleta_ids_to_update}}},
                    )

                vendor_ops = []
                vendor_buckets = {}
                for v in valid:
                    vid = v["vendedor_id"]
                    if vid != VENDEDOR_LOCAL:
                        vendor_buckets.setdefault(vid, []).append(v["boleta"])
                for vid, bids in vendor_buckets.items():
                    vendor_ops.append(
                        UpdateOne(
                            {"_id": vid},
                            {"$addToSet": {"boletas_asignadas": {"$each": bids}}},
                            upsert=True,
                        )
                    )
                if vendor_ops:
                    vendedores.bulk_write(vendor_ops, ordered=False)

                ticket_ops = [
                    UpdateOne(
                        {"_id": v["boleta"]},
                        [
                            {"$set": {"vendedor_id": v["vendedor_id"]}},
                            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
                        ],
                    )
                    for v in valid
                ]
                boletas.bulk_write(ticket_ops, ordered=False)
                invalidate_dashboard_cache()

            return jsonify(
                {
                    "ok": True,
                    "assigned": len(valid),
                    "skipped": skipped,
                    "warnings": warnings,
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

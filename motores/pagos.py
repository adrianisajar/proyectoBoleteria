import contextlib
import re
from datetime import datetime

from flask import Flask, Response

from motores.constants import (
    BOLETA_MAX,
    BOLETA_MIN,
    METODO_TRANSFERENCIA,
    MOV_PAGO,
    OPERACIONES_VENDEDOR,
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
    normalize_vendedor_id,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    safe_vendedores_snapshot,
    url_for,
    vendedores,
)
from motores.validacion import boletas_incompletas, parse_boletas, sanitizar_texto


def _actualizar_estado_boletas(filtro: dict, nuevo_vendedor_id: str, valor_boleta: int) -> None:
    """Reassign tickets matching filtro to a vendor and recalc their estado."""
    pipeline = [
        {"$set": {"vendedor_id": nuevo_vendedor_id}},
        {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
    ]
    if nuevo_vendedor_id == VENDEDOR_SIN_ASIGNAR:
        pipeline.append({"$set": {"fecha_adquisicion": None}})
    boletas.update_many(filtro, pipeline)


def _render_vendedores(form_data: dict) -> str:
    """Render the vendor panel with the current snapshot and the submitted form data."""
    vendedores_lista, resumen = safe_vendedores_snapshot()
    return render_template(
        "vendedores.html",
        form=form_data,
        vendedores_lista=vendedores_lista,
        resumen=resumen,
        now_local_date=now_local().strftime("%Y-%m-%d"),
    )


def _validar_form_vendedor(form_data: dict) -> tuple[str, list[int], list[str]]:
    """Validate the vendor form and return (vendedor_id, boleta_ids, errors)."""
    errors = []
    vendedor_id = ""
    try:
        require_collections()
        raw_id = form_data.get("vendedor_id", "").strip()
        if not raw_id:
            raw_id = form_data["nombre"]
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


def _procesar_asignar(vendedor_id: str, boleta_ids: list[int], perfil_update: dict, valor_boleta: int) -> None:
    """Assign tickets to a vendor, blocking tickets with payments and handling reasignment."""
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
    if con_pagos:
        ids_str = ", ".join(f"#{b:04d}" for b in con_pagos)
        raise ValueError(f"No se pueden asignar boletas con pagos registrados: {ids_str}")

    de_otro = {b: docs[b]["vendedor_id"] for b in existentes if (docs[b].get("vendedor_id") or "") not in ("", vendedor_id)}
    if de_otro:
        detalles = ", ".join(f"#{b:04d} → {o}" for b, o in de_otro.items())
        flash(f"Atención: estas boletas pertenecen a otro vendedor y serán reasignadas: {detalles}", "warning")

    vendedores.update_many(
        {"_id": {"$ne": vendedor_id}},
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
    _actualizar_estado_boletas({"_id": {"$in": existentes}}, vendedor_id, valor_boleta)
    invalidate_dashboard_cache()
    mensaje = f"{len(existentes)} boleta(s) asignada(s) a {vendedor_id}."
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
    vendedor_doc = vendedores.find_one({"_id": vendedor_id})
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
        _actualizar_estado_boletas({"_id": {"$in": boletas_ids_vendor}, "vendedor_id": vendedor_id}, VENDEDOR_SIN_ASIGNAR, valor_boleta)
    vendedores.delete_one({"_id": vendedor_id})
    invalidate_dashboard_cache()
    flash(f"Vendedor {vendedor_id} ({vendedor_doc.get('nombre', '')}) eliminado con {len(boletas_ids_vendor)} boleta(s) liberada(s).", "success")


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
        }

        if request.method == "POST":
            form_data.update(
                {
                    "vendedor_id": request.form.get("vendedor_id", ""),
                    "nombre": sanitizar_texto(request.form.get("nombre", ""), "name"),
                    "telefono": sanitizar_texto(request.form.get("telefono", ""), "numbers"),
                    "operacion": request.form.get("operacion", "").strip().lower(),
                    "boletas": request.form.get("boletas", "").strip(),
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
                    _procesar_asignar(vendedor_id, boleta_ids, perfil_update, valor_boleta)
                elif operacion == "quitar":
                    _procesar_quitar(vendedor_id, boleta_ids, perfil_update, valor_boleta)
                elif operacion == "eliminar":
                    _procesar_eliminar(vendedor_id, valor_boleta)
                elif operacion == "registrar_fecha_adquisicion":
                    _procesar_registrar_fecha(vendedor_id, request.form)
            except Exception as exc:
                flash(f"No se pudo aplicar la operaci\u00f3n del vendedor: {exc}", "danger")
                return _render_vendedores(form_data)

            return redirect(url_for("vendedores_panel"))

        return _render_vendedores(form_data)

    @app.route("/api/vendedores")
    @role_required("admin", "cajero", "consulta")
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
    @role_required("admin", "cajero", "consulta")
    def api_vendedor_boletas(vendedor_id: str) -> Response | tuple[Response, int]:
        """JSON list of a vendor's assigned tickets with state/amount/client."""
        require_collections()
        try:
            docs = list(
                boletas.find(
                    {"vendedor_id": vendedor_id},
                    {"_id": 1, "estado": 1, "total_abonado": 1, "cliente": 1, "fecha_adquisicion": 1},
                ).sort("_id", 1)
            )
            boletas_list = []
            for d in docs:
                cliente = d.get("cliente") or {}
                boletas_list.append(
                    {
                        "numero": f"{d['_id']:04d}",
                        "estado": d.get("estado", "disponible"),
                        "abonado": int(d.get("total_abonado", 0) or 0),
                        "cliente": cliente.get("nombre", ""),
                        "fecha_adquisicion": d.get("fecha_adquisicion") or "",
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
        except Exception:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not isinstance(boletas_list, list) or operacion not in ("asignar", "quitar"):
            return jsonify({"ok": False, "error": "Par\u00e1metros inv\u00e1lidos."}), 400
        try:
            require_collections()
            int_ids = [int(b) for b in boletas_list if BOLETA_MIN <= int(b) <= BOLETA_MAX]
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
                    item["ok"] = False
                    item["error"] = "Tiene pagos registrados"
                elif doc.get("vendedor_id") and doc["vendedor_id"] not in ("", VENDEDOR_LOCAL, vendedor_id):
                    v_id = doc["vendedor_id"]
                    v_nom = v_nombres.get(v_id, v_id)
                    item["ok"] = False
                    item["error"] = f"Pertenece a {v_nom} ({v_id})"
                else:
                    item["ok"] = True
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
        except Exception:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not isinstance(rows, list):
            return jsonify({"ok": False, "error": "Par\u00e1metros inv\u00e1lidos."}), 400
        with contextlib.suppress(Exception):
            require_collections()
        try:
            results = []
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                metodo = (row.get("metodo") or "").strip()
                if metodo != METODO_TRANSFERENCIA:
                    continue
                ref = (row.get("referencia") or "").strip()
                if not ref:
                    continue
                elem_match = {"tipo": {"$in": [None, MOV_PAGO]}, "metodo": METODO_TRANSFERENCIA, "referencia": ref}
                dup = boletas.find_one({"historial_movimientos": {"$elemMatch": elem_match}}, {"_id": 1})
                if dup:
                    results.append({"index": i, "referencia": ref, "error": f"La referencia '{ref}' ya existe en otro pago (boleta #{dup['_id']:04d})."})
            return jsonify({"ok": True, "resultados": results})
        except Exception as e:
            return jsonify({"ok": False, "error": safe_error_message(e)}), 500

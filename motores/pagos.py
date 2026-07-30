import re

from motores.validacion import parse_boletas
from motores.constants import OPERACIONES_VENDEDOR, BOLETA_MIN, BOLETA_MAX, VENDEDOR_SIN_ASIGNAR, VENDEDOR_LOCAL, METODO_TRANSFERENCIA

from motores.shared import (
    boletas, vendedores,
    request, flash, redirect, render_template, url_for, jsonify,
    get_config, require_collections, role_required,
    safe_vendedores_snapshot,
    normalize_vendedor_id, existing_boleta_ids,
    estado_pipeline_expr, invalidate_dashboard_cache,
)


def _actualizar_estado_boletas(filtro, nuevo_vendedor_id, valor_boleta):
    boletas.update_many(
        filtro,
        [
            {"$set": {"vendedor_id": nuevo_vendedor_id}},
            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
        ],
    )


def _render_vendedores(form_data):
    vendedores_lista, resumen = safe_vendedores_snapshot()
    return render_template("vendedores.html", form=form_data, vendedores_lista=vendedores_lista, resumen=resumen)


def _validar_form_vendedor(form_data):
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
        if out_of_range:
            errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
        if not boleta_ids:
            errors.append("Ingresa al menos una boleta para esta operaci\u00f3n.")

    return vendedor_id, boleta_ids, errors


def _procesar_guardar(vendedor_id, perfil_update):
    existe = vendedores.find_one({"_id": vendedor_id}, {"_id": 1})
    vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)
    if existe:
        flash(f"Vendedor {vendedor_id} actualizado.", "info")
    else:
        flash(f"Vendedor {vendedor_id} creado.", "success")


def _boletas_con_pagos(ids):
    """Return list of ticket ids that have payments (total_abonado > 0)."""
    return [
        b["_id"]
        for b in boletas.find({"_id": {"$in": ids}, "total_abonado": {"$gt": 0}}, {"_id": 1})
    ]


def _boletas_de_otro_vendedor(ids, vendedor_id):
    """Return dict {ticket_id: owner_id} for tickets assigned to a DIFFERENT vendor."""
    docs = boletas.find(
        {"_id": {"$in": ids}, "vendedor_id": {"$exists": True, "$ne": None, "$nin": ["", vendedor_id]}},
        {"_id": 1, "vendedor_id": 1},
    )
    return {d["_id"]: d["vendedor_id"] for d in docs}


def _procesar_asignar(vendedor_id, boleta_ids, perfil_update, valor_boleta):
    existentes = existing_boleta_ids(boleta_ids)
    faltantes = len(boleta_ids) - len(existentes)

    if not existentes:
        flash("No se encontraron boletas v\u00e1lidas para asignar.", "warning")
        return

    con_pagos = _boletas_con_pagos(existentes)
    if con_pagos:
        ids_str = ", ".join(f"#{b:04d}" for b in con_pagos)
        raise ValueError(f"No se pueden asignar boletas con pagos registrados: {ids_str}")

    de_otro = _boletas_de_otro_vendedor(existentes, vendedor_id)
    if de_otro:
        detalles = ", ".join(f"#{b:04d} → {o}" for b, o in de_otro.items())
        flash(f"Atención: estas boletas pertenecen a otro vendedor y serán reasignadas: {detalles}", "warning")

    vendedores.update_many(
        {"_id": {"$ne": vendedor_id}},
        {"$pull": {"boletas_asignadas": {"$in": existentes}}},
    )
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


def _procesar_quitar(vendedor_id, boleta_ids, perfil_update, valor_boleta):
    existentes = existing_boleta_ids(boleta_ids)
    faltantes = len(boleta_ids) - len(existentes)

    if not vendedores.find_one({"_id": vendedor_id}, {"_id": 1}):
        flash(f"El vendedor {vendedor_id} no existe.", "danger")
        return

    if not existentes:
        flash("No se encontraron boletas v\u00e1lidas para quitar.", "warning")
        return

    # Validate: tickets must belong to this vendor
    ajenas = list(boletas.find(
        {"_id": {"$in": existentes}, "vendedor_id": {"$ne": vendedor_id}},
        {"_id": 1},
    ))
    if ajenas:
        ids_ajenas = ", ".join(f"#{b['_id']:04d}" for b in ajenas)
        raise ValueError(
            f"Las siguientes boletas no pertenecen a {vendedor_id}: {ids_ajenas}. "
            "Solo puedes quitar boletas asignadas a este vendedor."
        )

    # Validate: cannot remove tickets with payments
    con_pagos = _boletas_con_pagos(existentes)
    if con_pagos:
        ids_str = ", ".join(f"#{b:04d}" for b in con_pagos)
        raise ValueError(f"No se pueden quitar boletas con pagos registrados: {ids_str}")

    vendedores.update_one({"_id": vendedor_id}, {"$pull": {"boletas_asignadas": {"$in": existentes}}})
    _actualizar_estado_boletas({"_id": {"$in": existentes}, "vendedor_id": vendedor_id}, VENDEDOR_SIN_ASIGNAR, valor_boleta)
    invalidate_dashboard_cache()
    mensaje = f"{len(existentes)} boleta(s) quitada(s) de {vendedor_id}."
    if faltantes:
        mensaje += f" {faltantes} no exist\u00edan en la colecci\u00f3n boletas."
    flash(mensaje, "success")


def _procesar_eliminar(vendedor_id, valor_boleta):
    vendedor_doc = vendedores.find_one({"_id": vendedor_id})
    if not vendedor_doc:
        flash(f"El vendedor {vendedor_id} no existe.", "danger")
        return

    boletas_ids_vendor = [
        num for num in vendedor_doc.get("boletas_asignadas", [])
        if isinstance(num, int) and BOLETA_MIN <= num <= BOLETA_MAX
    ]

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


def register_routes(app):
    @app.route("/vendedores", methods=["GET", "POST"])
    @role_required("admin")
    def vendedores_panel():
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
                    "nombre": request.form.get("nombre", "").strip(),
                    "telefono": request.form.get("telefono", "").strip(),
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
            except Exception as exc:
                flash(f"No se pudo aplicar la operaci\u00f3n del vendedor: {exc}", "danger")
                return _render_vendedores(form_data)

            return redirect(url_for("vendedores_panel"))

        return _render_vendedores(form_data)

    @app.route("/api/vendedores")
    @role_required("admin", "cajero", "consulta")
    def api_vendedores():
        require_collections()
        q = request.args.get("q", "").strip()
        query = {}
        if q:
            query["$or"] = [
                {"_id": {"$regex": re.escape(q), "$options": "i"}},
                {"nombre": {"$regex": re.escape(q), "$options": "i"}},
            ]
        docs = list(vendedores.find(query, {"nombre": 1, "telefono": 1}).sort("_id", 1).limit(20))
        return jsonify([{"_id": d["_id"], "nombre": d.get("nombre", ""), "telefono": d.get("telefono", "")} for d in docs])

    @app.route("/api/vendedores/<vendedor_id>/boletas")
    @role_required("admin", "cajero", "consulta")
    def api_vendedor_boletas(vendedor_id):
        require_collections()
        try:
            docs = list(boletas.find(
                {"vendedor_id": vendedor_id},
                {"_id": 1, "estado": 1, "total_abonado": 1, "cliente": 1},
            ).sort("_id", 1))
            boletas_list = []
            for d in docs:
                cliente = d.get("cliente") or {}
                boletas_list.append({
                    "numero": f"{d['_id']:04d}",
                    "estado": d.get("estado", "disponible"),
                    "abonado": int(d.get("total_abonado", 0) or 0),
                    "cliente": cliente.get("nombre", ""),
                })
            return jsonify({"ok": True, "total": len(boletas_list), "boletas": boletas_list})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/validar-boletas-vendedor", methods=["POST"])
    @role_required("admin")
    def api_validar_boletas_vendedor():
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
        docs = {d["_id"]: d for d in boletas.find(
            {"_id": {"$in": int_ids}},
            {"_id": 1, "vendedor_id": 1, "estado": 1, "total_abonado": 1},
        )}
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
                    item["error"] = f"No pertenece a este vendedor"
                elif doc.get("total_abonado", 0) > 0:
                    item["ok"] = False
                    item["error"] = "Tiene pagos registrados"
                else:
                    item["ok"] = True
            resultados.append(item)
        return jsonify({"ok": True, "resultados": resultados})

    @app.route("/api/validar-referencias-vendedor", methods=["POST"])
    @role_required("admin")
    def api_validar_referencias_vendedor():
        try:
            data = request.get_json(force=True) or {}
            rows = data.get("rows", [])
        except Exception:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not isinstance(rows, list):
            return jsonify({"ok": False, "error": "Par\u00e1metros inv\u00e1lidos."}), 400
        try:
            require_collections()
        except Exception:
            pass
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
                elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": ref}
                dup = boletas.find_one({"historial_pagos": {"$elemMatch": elem_match}}, {"_id": 1})
                if dup:
                    results.append({
                        "index": i,
                        "referencia": ref,
                        "error": f"La referencia '{ref}' ya existe en otro pago (boleta #{dup['_id']:04d})."
                    })
            return jsonify({"ok": True, "resultados": results})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500



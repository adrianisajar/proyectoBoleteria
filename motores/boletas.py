import re

from motores.constants import BOLETA_MIN, BOLETA_MAX, VENDEDOR_LOCAL

from motores.shared import (
    boletas, vendedores,
    request, flash, redirect, render_template, url_for, jsonify,
    get_config, require_collections, role_required,
    calcular_premios_adicionales,
    build_consulta_context, build_page_url,
    get_dashboard_counts, get_vendedor_options,
)


def register_routes(app):

    @app.route("/consultas")
    @role_required("admin", "cajero", "consulta")
    def consultas():
        try:
            config = get_config()
            valor_boleta = int(config["valor_boleta"])
            counts = get_dashboard_counts(valor_boleta=valor_boleta)
            vendedor_options = get_vendedor_options()
        except Exception as exc:
            counts = {"total": 0, "vendidas": 0, "disponibles": 0, "asignadas": 0, "separadas": 0, "abonando": 0, "pagadas": 0}
            vendedor_options = []
            flash(f"No se pudieron cargar las m├®tricas: {exc}", "danger")

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
            separadas=counts.get("separadas", 0),
            abonando=counts.get("abonando", 0),
            pagadas=counts.get("pagadas", 0),
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
            flash("El n├║mero de boleta debe estar entre 0000 y 9999.", "warning")
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
            flash(f"Cliente actualizado para la boleta #{boleta_id:04d}.", "success")
        else:
            flash(f"No existe la boleta #{boleta_id:04d}.", "warning")

        return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

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

        vendedor_id = doc.get("vendedor_id", VENDEDOR_LOCAL)
        v_nombre = vendedor_id
        if vendedor_id and vendedor_id != VENDEDOR_LOCAL:
            v_doc = vendedores.find_one({"_id": vendedor_id}, {"nombre": 1})
            if v_doc:
                v_nombre = v_doc.get("nombre", vendedor_id)

        return jsonify(
            {
                "ok": True,
                "boleta": f"{doc['_id']:04d}",
                "vendedor_id": vendedor_id,
                "vendedor_nombre": v_nombre,
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

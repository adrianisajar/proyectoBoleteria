import csv
import io
import re

from flask import Response

from motores.constants import BOLETA_MIN, BOLETA_MAX, VENDEDOR_LOCAL

from motores.shared import (
    boletas, vendedores,
    request, flash, redirect, render_template, url_for, jsonify,
    get_config, require_collections, role_required,
    calcular_premios_adicionales,
    build_consulta_context, build_page_url,
    get_dashboard_counts, get_vendedor_options,
)

SORT_WHITELIST = {"_id", "vendedor_id", "estado", "total_abonado", "cliente.nombre"}


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
        sort_by = request.args.get("sort_by", "_id").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_by not in SORT_WHITELIST:
            sort_by = "_id"
        sort_direction = 1 if sort_dir == "asc" else -1
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
                resultados = list(boletas.find(query, projection).sort(sort_by, sort_direction).skip(offset).limit(limite))
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

        filtered_counts = None
        if has_filters and not errors:
            try:
                group_stage = {
                    "$group": {
                        "_id": "$estado",
                        "count": {"$sum": 1},
                        "total_abonado_sum": {"$sum": {"$ifNull": ["$total_abonado", 0]}},
                    }
                }
                raw = list(boletas.aggregate([{"$match": query}, group_stage]))
                fc = {r["_id"]: r for r in raw}
                filtered_counts = {
                    "total": sum(r["count"] for r in raw),
                    "disponibles": fc.get("disponible", {}).get("count", 0),
                    "vendidas": sum(
                        fc.get(s, {}).get("count", 0)
                        for s in ("asignada", "separada", "abonando", "pagada")
                    ),
                    "asignadas": fc.get("asignada", {}).get("count", 0),
                    "separadas": fc.get("separada", {}).get("count", 0),
                    "abonando": fc.get("abonando", {}).get("count", 0),
                    "pagadas": fc.get("pagada", {}).get("count", 0),
                }
            except Exception:
                pass

        prev_url = build_page_url("consultas", filters, page - 1) if page > 1 else None
        next_url = build_page_url("consultas", filters, page + 1) if page < total_pages else None

        vendedor_label = ""
        vf = filters.get("vendedor_id", "")
        if vf == "__":
            vendedor_label = "Sin asignar"
        elif vf == VENDEDOR_LOCAL:
            vendedor_label = "LOCAL"
        elif vf:
            for v in vendedor_options:
                if v["_id"] == vf:
                    vendedor_label = v.get("nombre", vf)
                    break
            if not vendedor_label:
                vendedor_label = vf

        return render_template(
            "inicio.html",
            total=counts["total"],
            vendidas=counts["vendidas"],
            disponibles=counts["disponibles"],
            asignadas=counts["asignadas"],
            separadas=counts.get("separadas", 0),
            abonando=counts.get("abonando", 0),
            pagadas=counts.get("pagadas", 0),
            filtered_counts=filtered_counts,
            vendedor_options=vendedor_options,
            filters=filters,
            vendedor_label=vendedor_label,
            resultados=resultados,
            total_resultados=total_resultados,
            page=page,
            total_pages=total_pages,
            limite=limite,
            prev_url=prev_url,
            next_url=next_url,
            has_filters=has_filters,
            boleta=boleta_detalle,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    @app.route("/consultas/exportar")
    @role_required("admin", "cajero", "consulta")
    def exportar_consultas():
        try:
            config = get_config()
        except Exception:
            config = {"valor_boleta": "100000"}
        exclude_sort = request.args.get("_", "")
        filters, query, errors, _page, _limite, _offset, _has_filters, _numero_exacto = build_consulta_context(request.args)
        if errors:
            flash("Error al exportar: corrija los filtros.", "danger")
            return redirect(url_for("consultas"))
        try:
            require_collections()
            docs = boletas.find(query, {"_id": 1, "vendedor_id": 1, "cliente": 1, "estado": 1, "total_abonado": 1, "historial_pagos": {"$slice": -1}}).sort("_id", 1)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Boleta", "Vendedor", "Estado", "Cliente", "Telefono", "Abonado", "UltimoPago"])
            for doc in docs:
                ultimo = doc.get("historial_pagos") or []
                ultimo_pago = ultimo[0].get("fecha", "") if ultimo else ""
                writer.writerow([
                    f"{doc['_id']:04d}",
                    doc.get("vendedor_id", ""),
                    doc.get("estado", ""),
                    (doc.get("cliente") or {}).get("nombre", ""),
                    (doc.get("cliente") or {}).get("telefono", ""),
                    doc.get("total_abonado", 0),
                    ultimo_pago,
                ])
            csv_content = output.getvalue()
            output.close()
            return Response(
                csv_content,
                mimetype="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=resultados_consultas.csv"},
            )
        except Exception as exc:
            flash(f"Error al exportar: {exc}", "danger")
            return redirect(url_for("consultas"))

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

    @app.route("/api/validar-boleta-vendedor", methods=["POST"])
    @role_required("admin", "cajero")
    def validar_boleta_vendedor():
        try:
            data = request.get_json(force=True) or {}
            boletas_list = data.get("boletas", [])
            vendedor_id = data.get("vendedor_id", "").strip()
        except Exception:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not boletas_list or not isinstance(boletas_list, list):
            return jsonify({"ok": False, "error": "Se requiere una lista de boletas."}), 400
        try:
            require_collections()
            int_ids = [int(b) for b in boletas_list if BOLETA_MIN <= int(b) <= BOLETA_MAX]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Boleta(s) inv\u00e1lida(s)."}), 400
        docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": int_ids}}, {"_id": 1, "vendedor_id": 1, "estado": 1, "total_abonado": 1})}
        resultados = []
        for b in int_ids:
            doc = docs.get(b)
            if not doc:
                resultados.append({"boleta": b, "ok": False, "error": "No existe"})
            elif doc.get("estado") == "pagada":
                resultados.append({"boleta": b, "ok": False, "error": "Pagada"})
            elif vendedor_id and doc.get("vendedor_id") != vendedor_id:
                resultados.append({"boleta": b, "ok": False, "error": "Vendedor distinto", "actual": doc.get("vendedor_id")})
            else:
                resultados.append({"boleta": b, "ok": True})
        return jsonify({"ok": True, "resultados": resultados})

    @app.route("/api/validar-boleta-cliente", methods=["POST"])
    @role_required("admin", "cajero")
    def validar_boleta_cliente():
        try:
            data = request.get_json(force=True) or {}
            boletas_list = data.get("boletas", [])
        except Exception:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        if not boletas_list or not isinstance(boletas_list, list):
            return jsonify({"ok": False, "error": "Se requiere una lista de boletas."}), 400
        try:
            require_collections()
            int_ids = [int(b) for b in boletas_list if BOLETA_MIN <= int(b) <= BOLETA_MAX]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Boleta(s) inv\u00e1lida(s)."}), 400
        docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": int_ids}}, {"_id": 1, "estado": 1, "vendedor_id": 1, "total_abonado": 1, "cliente": 1})}
        resultados = []
        for b in int_ids:
            doc = docs.get(b)
            if not doc:
                resultados.append({"boleta": b, "ok": False, "error": "No existe"})
            elif doc.get("estado") == "pagada":
                resultados.append({"boleta": b, "ok": False, "error": "Pagada", "warning": True})
            else:
                resultados.append({"boleta": b, "ok": True, "estado": doc.get("estado"), "vendedor_id": doc.get("vendedor_id"), "cliente": (doc.get("cliente") or {}).get("nombre", "")})
        return jsonify({"ok": True, "resultados": resultados})

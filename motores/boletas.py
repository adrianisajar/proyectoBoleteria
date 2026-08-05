import contextlib
import csv
import io
import re

from flask import Flask, Response

from motores.constants import BOLETA_MAX, BOLETA_MIN, VENDEDOR_LOCAL
from motores.errores import safe_error_message
from motores.shared import (
    boletas,
    build_consulta_context,
    build_page_url,
    estado_pipeline_expr,
    facturas,
    flash,
    get_config,
    get_dashboard_counts,
    get_vendedor_options,
    invalidate_dashboard_cache,
    jsonify,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    url_for,
    vendedores,
)

SORT_WHITELIST = {"_id", "vendedor_id", "estado", "total_abonado", "cliente.nombre"}


def register_routes(app: Flask) -> None:
    """Register the ticket search and ticket management routes."""

    @app.route("/consultas")
    @role_required("admin", "cajero", "consulta")
    def consultas() -> str | Response:
        """Ticket search page with filters, pagination and state metrics."""
        filters, query, errors, page, limite, offset, has_filters, numero_exacto = build_consulta_context(request.args)

        counts = {"total": 0, "vendidas": 0, "disponibles": 0, "asignadas": 0, "separadas": 0, "abonando": 0, "pagadas": 0}
        vendedor_options = []
        with contextlib.suppress(Exception):
            vendedor_options = get_vendedor_options()
        if not numero_exacto:
            try:
                config = get_config()
                valor_boleta = int(config["valor_boleta"])
                counts = get_dashboard_counts(valor_boleta=valor_boleta)
            except Exception as exc:
                flash(f"No se pudieron cargar las m├®tricas: {exc}", "danger")
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
                    "historial_pagos": 1,
                }

                total_resultados = boletas.count_documents(query)
                resultados = list(boletas.find(query, projection).sort(sort_by, sort_direction).skip(offset).limit(limite))

                if numero_exacto and isinstance(query.get("_id"), int):
                    boleta_detalle = boletas.find_one({"_id": query["_id"]})
                    if boleta_detalle and boleta_detalle.get("vendedor_id"):
                        v = vendedores.find_one({"_id": boleta_detalle["vendedor_id"]}, {"nombre": 1})
                        boleta_detalle["vendedor_nombre"] = v["nombre"] if v else None
            except Exception as exc:
                flash(f"No se pudo ejecutar la consulta: {exc}", "danger")

        total_pages = 1 if limite == 0 else max(1, (total_resultados + limite - 1) // limite)
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
                    "vendidas": sum(fc.get(s, {}).get("count", 0) for s in ("asignada", "separada", "abonando", "pagada")),
                    "asignadas": fc.get("asignada", {}).get("count", 0),
                    "separadas": fc.get("separada", {}).get("count", 0),
                    "abonando": fc.get("abonando", {}).get("count", 0),
                    "pagadas": fc.get("pagada", {}).get("count", 0),
                }
            except Exception:
                pass

        prev_url = build_page_url("consultas", filters, page - 1) if page > 1 else None
        next_url = build_page_url("consultas", filters, page + 1) if page < total_pages else None

        export_params = {k: v for k, v in filters.items() if v}
        export_url = url_for("exportar_consultas", **export_params)

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

        vendedor_nombres = {v["_id"]: v.get("nombre") or v["_id"] for v in vendedor_options}

        return render_template(
            "inicio.html",
            total=counts["total"],
            disponibles=counts["disponibles"],
            asignadas=counts["asignadas"],
            separadas=counts.get("separadas", 0),
            abonando=counts.get("abonando", 0),
            pagadas=counts.get("pagadas", 0),
            filtered_counts=filtered_counts,
            vendedor_options=vendedor_options,
            filters=filters,
            vendedor_label=vendedor_label,
            vendedor_nombres=vendedor_nombres,
            resultados=resultados,
            total_resultados=total_resultados,
            page=page,
            total_pages=total_pages,
            limite=limite,
            prev_url=prev_url,
            next_url=next_url,
            export_url=export_url,
            has_filters=has_filters,
            boleta=boleta_detalle,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    @app.route("/consultas/exportar")
    @role_required("admin", "cajero", "consulta")
    def exportar_consultas() -> Response:
        """Export filtered ticket results as a semicolon-separated CSV."""
        _filters, query, errors, _page, _limite, _offset, _has_filters, _numero_exacto = build_consulta_context(request.args)
        if errors:
            flash("Error al exportar: corrija los filtros.", "danger")
            return redirect(url_for("consultas"))
        try:
            require_collections()
            projection = {"_id": 1, "vendedor_id": 1, "cliente": 1, "estado": 1, "total_abonado": 1, "historial_pagos": 1}
            docs = list(boletas.find(query, projection).sort("_id", 1))

            output = io.StringIO()
            writer = csv.writer(output, delimiter=";")
            writer.writerow(["Boleta", "Vendedor", "Estado", "Cliente", "Telefono", "Abonado", "UltimoPago"])
            for doc in docs:
                ultimo = doc.get("historial_pagos") or []
                ultimo_pago = ultimo[-1].get("fecha", "") if ultimo else ""
                writer.writerow(
                    [
                        f"{doc['_id']:04d}",
                        doc.get("vendedor_id", ""),
                        doc.get("estado", ""),
                        (doc.get("cliente") or {}).get("nombre", ""),
                        (doc.get("cliente") or {}).get("telefono", ""),
                        doc.get("total_abonado", 0),
                        ultimo_pago,
                    ]
                )
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
    def actualizar_cliente(boleta_id: int) -> Response:
        """Legacy alias that forwards to guardar_boleta."""
        return redirect(url_for("guardar_boleta", boleta_id=boleta_id), code=307)

    @app.route("/boletas/<int:boleta_id>/guardar", methods=["POST"])
    @role_required("admin", "cajero")
    def guardar_boleta(boleta_id: int) -> Response:
        """Save client data (and optional vendor) on a ticket."""
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("El n\u00famero de boleta debe estar entre 0000 y 9999.", "warning")
            return redirect(url_for("consultas"))

        try:
            require_collections()

            nombre = request.form.get("nombre", "").strip().upper()
            telefono = request.form.get("telefono", "").strip()
            direccion = request.form.get("direccion", "").strip().upper()
            vendedor_id = request.form.get("vendedor_id", "").strip()

            set_fields = {"cliente": {"nombre": nombre, "telefono": telefono, "direccion": direccion}}

            if vendedor_id:
                v_exists = vendedores.find_one({"_id": vendedor_id}, {"_id": 1})
                if v_exists:
                    set_fields["vendedor_id"] = vendedor_id

            boletas.update_one({"_id": boleta_id}, {"$set": set_fields})

            if nombre and not vendedor_id:
                boletas.update_one(
                    {
                        "_id": boleta_id,
                        "$or": [{"vendedor_id": {"$in": ["", None]}}, {"vendedor_id": VENDEDOR_LOCAL}],
                        "total_abonado": 0,
                        "estado": {"$nin": ["pagada", "abonando"]},
                    },
                    {"$set": {"vendedor_id": VENDEDOR_LOCAL}},
                )

            config_local = get_config()
            valor_boleta_local = int(config_local.get("valor_boleta", 10000) or 10000)
            boletas.update_one(
                {"_id": boleta_id},
                [{"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}}],
            )

            invalidate_dashboard_cache()
            flash(f"Datos guardados para #{boleta_id:04d}.", "success")
        except Exception as exc:
            flash(f"Error al guardar: {exc}", "danger")

        return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

    @app.route("/boletas/<int:boleta_id>/pago/<int:idx>/eliminar", methods=["POST"])
    @role_required("admin", "cajero")
    def eliminar_pago_boleta(boleta_id: int, idx: int) -> Response:
        """Remove one payment from a ticket's history and recalc totals (updates factura)."""
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("N\u00famero de boleta inv\u00e1lido.", "warning")
            return redirect(url_for("consultas"))

        try:
            require_collections()
            doc = boletas.find_one({"_id": boleta_id}, {"historial_pagos": 1})
            if not doc:
                flash(f"No existe la boleta #{boleta_id:04d}.", "warning")
                return redirect(url_for("consultas"))

            pagos = doc.get("historial_pagos") or []
            if idx < 0 or idx >= len(pagos):
                flash("\u00cdndice de pago inv\u00e1lido.", "danger")
                return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

            pago = pagos[idx]
            factura_id = pago.get("factura_id")
            valor = int(pago.get("valor", 0) or 0)

            config_local = get_config()
            valor_boleta_local = int(config_local.get("valor_boleta", 10000) or 10000)

            boletas.update_one(
                {"_id": boleta_id},
                [
                    {
                        "$set": {
                            "historial_pagos": {
                                "$concatArrays": [
                                    {"$slice": ["$historial_pagos", idx]},
                                    {"$slice": ["$historial_pagos", {"$add": [idx, 1]}, {"$size": "$historial_pagos"}]},
                                ]
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
                    {"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}},
                ],
            )

            if factura_id:
                metodo = pago.get("metodo", "")
                facturas.update_one(
                    {"_id": factura_id},
                    {"$pull": {"detalle": {"boleta": boleta_id, "valor": valor, "metodo": metodo}}},
                )
                factura_doc = facturas.find_one({"_id": factura_id}, {"detalle": 1})
                if factura_doc:
                    detalle_restante = factura_doc.get("detalle") or []
                    if not detalle_restante:
                        facturas.delete_one({"_id": factura_id})
                    else:
                        nuevo_total = sum(d.get("valor", 0) or 0 for d in detalle_restante)
                        facturas.update_one({"_id": factura_id}, {"$set": {"valor_total": nuevo_total}})

            invalidate_dashboard_cache()
            flash(f"Pago eliminado de #{boleta_id:04d}.", "success")
        except Exception as exc:
            flash(f"Error al eliminar pago: {exc}", "danger")

        return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

    @app.route("/boletas/<int:boleta_id>/recalcular", methods=["POST"])
    @role_required("admin", "cajero")
    def recalcular_boleta(boleta_id: int) -> Response:
        """Recompute total_abonado and estado from a ticket's payment history."""
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("N\u00famero de boleta inv\u00e1lido.", "warning")
            return redirect(url_for("consultas"))

        try:
            require_collections()
            if not boletas.find_one({"_id": boleta_id}, {"_id": 1}):
                flash(f"No existe la boleta #{boleta_id:04d}.", "warning")
                return redirect(url_for("consultas"))

            config_local = get_config()
            valor_boleta_local = int(config_local.get("valor_boleta", 10000) or 10000)

            result = boletas.update_one(
                {"_id": boleta_id},
                [
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
                    {"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}},
                ],
            )

            invalidate_dashboard_cache()
            if result.modified_count:
                flash(f"#{boleta_id:04d} recalcular: OK.", "success")
            else:
                flash(f"#{boleta_id:04d} sin cambios.", "info")
        except Exception as exc:
            flash(f"Error al recalcular: {exc}", "danger")

        return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

    @app.route("/boletas/<int:boleta_id>/limpiar", methods=["POST"])
    @role_required("admin", "cajero")
    def limpiar_boleta(boleta_id: int) -> Response:
        """Clear the client data saved on a ticket."""
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("N\u00famero de boleta inv\u00e1lido.", "warning")
            return redirect(url_for("consultas"))

        try:
            require_collections()
            if not boletas.find_one({"_id": boleta_id}, {"_id": 1}):
                flash(f"No existe la boleta #{boleta_id:04d}.", "warning")
                return redirect(url_for("consultas"))

            boletas.update_one(
                {"_id": boleta_id},
                {"$set": {"cliente": {"nombre": "", "telefono": "", "direccion": ""}}},
            )

            invalidate_dashboard_cache()
            flash(f"Datos del cliente eliminados de #{boleta_id:04d}.", "success")
        except Exception as exc:
            flash(f"Error al limpiar cliente: {exc}", "danger")

        return redirect(url_for("consultas", numero=f"{boleta_id:04d}"))

    @app.route("/api/clientes")
    @role_required("admin", "cajero", "consulta")
    def api_clientes() -> Response | tuple[Response, int]:
        """Autocomplete endpoint for saved clients (name/phone, min 2 chars)."""
        try:
            require_collections()
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
                    items.append(
                        {
                            "label": label,
                            "nombre": cliente.get("nombre", ""),
                            "telefono": cliente.get("telefono", ""),
                            "direccion": cliente.get("direccion", ""),
                        }
                    )
            return jsonify(items)
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

    @app.route("/api/boletas/<int:boleta_id>")
    @role_required("admin", "cajero", "consulta")
    def api_boleta(boleta_id: int) -> Response | tuple[Response, int]:
        """JSON lookup of a single ticket with vendor/client info."""
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            return jsonify({"ok": False, "error": "Boleta fuera de rango."}), 400

        try:
            require_collections()
            doc = boletas.find_one(
                {"_id": boleta_id},
                {"_id": 1, "vendedor_id": 1, "cliente": 1, "estado": 1, "total_abonado": 1, "historial_pagos": 1},
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

        if not doc:
            return jsonify({"ok": False, "error": "No existe la boleta."}), 404

        cliente = doc.get("cliente") or {}

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
            }
        )

    @app.route("/api/validar-boleta-vendedor", methods=["POST"])
    @role_required("admin", "cajero")
    def validar_boleta_vendedor() -> Response | tuple[Response, int]:
        """Pre-validate a list of tickets before a vendor payment (state/ownership)."""
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
        try:
            docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": int_ids}}, {"_id": 1, "vendedor_id": 1, "estado": 1, "total_abonado": 1})}
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500
        resultados = []
        for b in int_ids:
            doc = docs.get(b)
            boleta_str = f"{b:04d}"
            if not doc:
                resultados.append({"boleta": boleta_str, "ok": False, "error": "No existe"})
            elif doc.get("estado") == "pagada":
                resultados.append({"boleta": boleta_str, "ok": False, "error": "Pagada"})
            elif vendedor_id and doc.get("vendedor_id") != vendedor_id:
                resultados.append({"boleta": boleta_str, "ok": False, "error": "Vendedor distinto", "actual": doc.get("vendedor_id")})
            else:
                resultados.append({"boleta": boleta_str, "ok": True})
        return jsonify({"ok": True, "resultados": resultados})

    @app.route("/api/validar-boleta-cliente", methods=["POST"])
    @role_required("admin", "cajero")
    def validar_boleta_cliente() -> Response | tuple[Response, int]:
        """Pre-validate a list of tickets before a customer payment (existence/state)."""
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
        try:
            docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": int_ids}}, {"_id": 1, "estado": 1, "vendedor_id": 1, "total_abonado": 1, "cliente": 1})}
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500
        resultados = []
        for b in int_ids:
            doc = docs.get(b)
            boleta_str = f"{b:04d}"
            if not doc:
                resultados.append({"boleta": boleta_str, "ok": False, "error": "No existe"})
            elif doc.get("estado") == "pagada":
                resultados.append({"boleta": boleta_str, "ok": False, "error": "Pagada", "warning": True})
            else:
                resultados.append(
                    {
                        "boleta": boleta_str,
                        "ok": True,
                        "estado": doc.get("estado"),
                        "vendedor_id": doc.get("vendedor_id"),
                        "cliente": (doc.get("cliente") or {}).get("nombre", ""),
                    }
                )
        return jsonify({"ok": True, "resultados": resultados})

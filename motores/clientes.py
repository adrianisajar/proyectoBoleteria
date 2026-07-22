from motores.constants import BOLETA_MIN, BOLETA_MAX, VENDEDOR_LOCAL, ESTADO_SEPARADA

from motores.shared import (
    boletas, vendedores, facturas,
    request, flash, redirect, render_template, url_for,
    get_config, require_collections, role_required,
    estado_pipeline_expr,
)


def register_routes(app):

    @app.route("/clientes", methods=["GET"])
    @role_required("admin", "cajero", "consulta")
    def clientes():
        require_collections()
        config = get_config()
        valor_boleta = int(config.get("valor_boleta", 10000) or 10000)
        busqueda = request.args.get("busqueda", "").strip()
        boleta_info = None

        if busqueda:
            try:
                num = int(busqueda)
            except (ValueError, TypeError):
                flash("Ingrese un n\u00famero de boleta v\u00e1lido (0000-9999).", "warning")
            else:
                if num < BOLETA_MIN or num > BOLETA_MAX:
                    flash(f"La boleta debe estar entre {BOLETA_MIN:04d} y {BOLETA_MAX:04d}.", "warning")
                else:
                    doc = boletas.find_one({"_id": num})
                    if doc:
                        cliente = doc.get("cliente") or {}
                        v_id = doc.get("vendedor_id", "")
                        v_nombre = v_id
                        if v_id and v_id not in ("", VENDEDOR_LOCAL):
                            v_doc = vendedores.find_one({"_id": v_id}, {"nombre": 1})
                            if v_doc:
                                v_nombre = v_doc.get("nombre", v_id)
                        pagos = []
                        for idx, p in enumerate(doc.get("historial_pagos") or []):
                            pagos.append({
                                "idx": idx,
                                "fecha": str(p.get("fecha", "")),
                                "valor": int(p.get("valor", 0) or 0),
                                "metodo": p.get("metodo", ""),
                                "referencia": p.get("referencia", ""),
                                "factura_id": p.get("factura_id"),
                            })
                        boleta_info = {
                            "numero": f"{num:04d}",
                            "estado": doc.get("estado", ""),
                            "vendedor_id": v_id,
                            "vendedor_nombre": v_nombre,
                            "total_abonado": int(doc.get("total_abonado", 0) or 0),
                            "cliente": {
                                "nombre": cliente.get("nombre", ""),
                                "telefono": cliente.get("telefono", ""),
                                "direccion": cliente.get("direccion", ""),
                            },
                            "historial_pagos": pagos,
                            "valor_boleta": int(config.get("valor_boleta", 10000) or 10000),
                        }
                    else:
                        flash(f"No existe la boleta #{num:04d}.", "warning")

        return render_template(
            "clientes.html",
            busqueda=busqueda,
            boleta=boleta_info,
        )

    @app.route("/clientes/guardar", methods=["POST"])
    @role_required("admin", "cajero")
    def clientes_guardar():
        require_collections()
        boleta_id = request.form.get("boleta_id", "").strip()
        if not boleta_id:
            flash("No se especific\u00f3 una boleta.", "danger")
            return redirect(url_for("clientes"))

        try:
            num = int(boleta_id)
        except (ValueError, TypeError):
            flash("N\u00famero de boleta inv\u00e1lido.", "danger")
            return redirect(url_for("clientes"))

        if num < BOLETA_MIN or num > BOLETA_MAX:
            flash(f"La boleta debe estar entre {BOLETA_MIN:04d} y {BOLETA_MAX:04d}.", "warning")
            return redirect(url_for("clientes"))

        doc = boletas.find_one({"_id": num}, {"_id": 1})
        if not doc:
            flash(f"No existe la boleta #{num:04d}.", "warning")
            return redirect(url_for("clientes"))

        nombre = request.form.get("nombre", "").strip().upper()
        telefono = request.form.get("telefono", "").strip()
        direccion = request.form.get("direccion", "").strip().upper()

        cliente_data = {"nombre": nombre, "telefono": telefono, "direccion": direccion}

        try:
            boletas.update_one({"_id": num}, {"$set": {"cliente": cliente_data}})

            if nombre:
                boletas.update_one(
                    {
                        "_id": num,
                        "$or": [{"vendedor_id": {"$in": ["", None]}}, {"vendedor_id": VENDEDOR_LOCAL}],
                        "total_abonado": 0,
                        "estado": {"$nin": ["pagada", "abonando"]},
                    },
                    {"$set": {"vendedor_id": VENDEDOR_LOCAL, "estado": ESTADO_SEPARADA}},
                )

        except Exception as exc:
            flash(f"Error al guardar: {exc}", "danger")
            return redirect(url_for("clientes", busqueda=f"{num:04d}"))

        flash(f"Cliente guardado para #{num:04d}.", "success")
        return redirect(url_for("clientes", busqueda=f"{num:04d}"))

    @app.route("/clientes/<int:boleta_id>/pago/<int:idx>/eliminar", methods=["POST"])
    @role_required("admin", "cajero")
    def clientes_eliminar_pago(boleta_id, idx):
        require_collections()
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("N\u00famero de boleta inv\u00e1lido.", "warning")
            return redirect(url_for("clientes"))

        doc = boletas.find_one({"_id": boleta_id}, {"historial_pagos": 1, "cliente": 1, "vendedor_id": 1})
        if not doc:
            flash(f"No existe la boleta #{boleta_id:04d}.", "warning")
            return redirect(url_for("clientes"))

        pagos = doc.get("historial_pagos") or []
        if idx < 0 or idx >= len(pagos):
            flash(f"\u00cdndice de pago inv\u00e1lido.", "danger")
            return redirect(url_for("clientes", busqueda=f"{boleta_id:04d}"))

        pago = pagos[idx]
        factura_id = pago.get("factura_id")
        valor = int(pago.get("valor", 0) or 0)

        try:
            config = get_config()
            valor_boleta = int(config.get("valor_boleta", 10000) or 10000)

            result = boletas.update_one(
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
                    {
                        "$set": {
                            "estado": estado_pipeline_expr(valor_boleta)
                        }
                    },
                ],
            )

            if factura_id:
                facturas.update_one(
                    {"_id": factura_id},
                    {"$pull": {"detalle": {"boleta": boleta_id, "valor": valor}}},
                )
                factura_doc = facturas.find_one({"_id": factura_id}, {"detalle": 1})
                if factura_doc:
                    nuevo_total = sum(d.get("valor", 0) or 0 for d in (factura_doc.get("detalle") or []))
                    facturas.update_one({"_id": factura_id}, {"$set": {"valor_total": nuevo_total}})

            msg = f"Pago de ${valor:,} eliminado de #{boleta_id:04d}."
            if factura_id:
                msg += f" (Factura #{factura_id:05d})"
            msg += f" ({result.modified_count} boleta(s) afectada(s))"
            flash(msg, "success")

        except Exception as exc:
            flash(f"Error al eliminar pago: {exc}", "danger")

        return redirect(url_for("clientes", busqueda=f"{boleta_id:04d}"))

    @app.route("/clientes/<int:boleta_id>/recalcular", methods=["POST"])
    @role_required("admin", "cajero")
    def clientes_recalcular(boleta_id):
        require_collections()
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("N\u00famero de boleta inv\u00e1lido.", "warning")
            return redirect(url_for("clientes"))

        if not boletas.find_one({"_id": boleta_id}, {"_id": 1}):
            flash(f"No existe la boleta #{boleta_id:04d}.", "warning")
            return redirect(url_for("clientes"))

        try:
            config = get_config()
            valor_boleta = int(config.get("valor_boleta", 10000) or 10000)

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
                    {
                        "$set": {
                            "estado": estado_pipeline_expr(valor_boleta)
                        }
                    },
                ],
            )

            if result.modified_count:
                flash(f"#{boleta_id:04d} recalcular: OK.", "success")
            else:
                flash(f"#{boleta_id:04d} sin cambios.", "info")

        except Exception as exc:
            flash(f"Error al recalcular: {exc}", "danger")

        return redirect(url_for("clientes", busqueda=f"{boleta_id:04d}"))

    @app.route("/clientes/<int:boleta_id>/limpiar", methods=["POST"])
    @role_required("admin", "cajero")
    def clientes_limpiar(boleta_id):
        require_collections()
        if boleta_id < BOLETA_MIN or boleta_id > BOLETA_MAX:
            flash("N\u00famero de boleta inv\u00e1lido.", "warning")
            return redirect(url_for("clientes"))

        doc = boletas.find_one({"_id": boleta_id}, {"_id": 1})
        if not doc:
            flash(f"No existe la boleta #{boleta_id:04d}.", "warning")
            return redirect(url_for("clientes"))

        try:
            boletas.update_one(
                {"_id": boleta_id},
                {"$set": {"cliente": {"nombre": "", "telefono": "", "direccion": ""}}},
            )
        except Exception as exc:
            flash(f"Error al limpiar cliente: {exc}", "danger")
            return redirect(url_for("clientes", busqueda=f"{boleta_id:04d}"))

        flash(f"Datos del cliente eliminados de #{boleta_id:04d}.", "success")
        return redirect(url_for("clientes", busqueda=f"{boleta_id:04d}"))

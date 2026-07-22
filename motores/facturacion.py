import re
from datetime import datetime

from motores.constants import METODO_TRANSFERENCIA, REFERENCIA_N_A, USUARIO_SISTEMA
from motores.fechas import now_local

from motores.shared import (
    boletas, vendedores, facturas,
    request, flash, redirect, render_template, url_for, abort,
    require_collections, role_required,
    get_config,
    calcular_premios_adicionales,
    estado_pipeline_expr,
)


def register_routes(app):

    @app.route("/facturas")
    @role_required("admin", "cajero", "consulta")
    def facturas_list():
        require_collections()
        q = request.args.get("q", "").strip()
        query = {}
        if q:
            try:
                query["_id"] = int(q)
            except ValueError:
                query["$or"] = [
                    {"cliente.nombre": {"$regex": re.escape(q), "$options": "i"}},
                    {"vendedor_nombre": {"$regex": re.escape(q), "$options": "i"}},
                    {"vendedor_id": {"$regex": re.escape(q), "$options": "i"}},
                ]
        lista = list(facturas.find(query).sort("_id", -1).limit(100))
        return render_template("facturas_list.html", facturas=lista, q=q)

    @app.route("/facturas/cliente")
    @role_required("admin", "cajero", "consulta")
    def facturas_cliente():
        require_collections()
        lista = list(facturas.find({"tipo": "cliente"}).sort("_id", -1).limit(100))
        return render_template("facturas_cliente.html", facturas=lista)

    @app.route("/facturas/vendedor")
    @role_required("admin", "cajero", "consulta")
    def facturas_vendedor():
        require_collections()
        lista = list(facturas.find({"tipo": "vendedor"}).sort("_id", -1).limit(100))
        return render_template("facturas_vendedor.html", facturas=lista)

    @app.route("/facturas/<int:factura_id>")
    @role_required("admin", "cajero", "consulta")
    def ver_factura(factura_id):
        require_collections()
        factura = facturas.find_one({"_id": factura_id})
        if not factura:
            abort(404)

        ctx = {"factura": factura, "config": get_config(), "imprimir": request.args.get("imprimir")}

        if factura.get("tipo") == "cliente":
            boletas_ids = factura.get("boletas", [])
            docs = list(boletas.find({"_id": {"$in": boletas_ids}}))
            config = ctx["config"]
            valor_boleta = int(config.get("valor_boleta", 10000))
            premios_config = config.get("premios_adicionales", [])
            # Solo el próximo premio adicional (fecha >= hoy)
            hoy = now_local().date()
            next_premio = None
            for p in sorted(premios_config, key=lambda x: x.get("fecha_juego", "9999-12-31")):
                try:
                    if datetime.strptime(p["fecha_juego"], "%Y-%m-%d").date() >= hoy:
                        next_premio = p
                        break
                except (ValueError, KeyError):
                    continue
            if next_premio:
                ctx["premios_config"] = [next_premio]
                single_premio_config = [next_premio]
            else:
                ctx["premios_config"] = []
                single_premio_config = []
            boletas_info = {}
            vid_cache = {v["_id"]: v.get("nombre", v["_id"]) for v in vendedores.find({}, {"nombre": 1})}
            for doc in docs:
                bid = doc["_id"]
                historial_completo = doc.get("historial_pagos") or []
                # Todos los pagos hasta la fecha de la factura (para total_abonado, saldo e historial)
                fecha_factura_dt = factura["fecha"]
                if not isinstance(fecha_factura_dt, datetime):
                    fecha_factura_dt = datetime.strptime(str(fecha_factura_dt)[:19], "%Y-%m-%d %H:%M:%S")
                historial_hasta_factura = [
                    p for p in historial_completo
                    if p.get("registrado_en", fecha_factura_dt) <= fecha_factura_dt
                ]
                # Pagos de esta factura (para tabla "PAGOS DE ESTA FACTURA")
                historial_esta_factura = [
                    p for p in historial_hasta_factura
                    if p.get("factura_id") == factura_id
                ]
                total_hasta_factura = sum(int(p.get("valor", 0) or 0) for p in historial_hasta_factura)
                saldo_hasta_factura = max(valor_boleta - total_hasta_factura, 0)
                boletas_info[bid] = {
                    "total_abonado": total_hasta_factura,
                    "saldo_pendiente": saldo_hasta_factura,
                    "estado": doc.get("estado", "disponible"),
                    "valor_boleta": valor_boleta,
                    "vendedor_id": doc.get("vendedor_id", "LOCAL"),
                    "vendedor_nombre": vid_cache.get(doc.get("vendedor_id", "LOCAL"), "LOCAL"),
                    "premios_adicionales": calcular_premios_adicionales(historial_completo, single_premio_config),
                    "historial_pagos": historial_hasta_factura,
                    "pagos_factura": historial_esta_factura,
                }
            ctx["boletas_info"] = boletas_info

        if factura.get("tipo") == "vendedor":
            total_efectivo = 0
            total_transferencia = 0
            referencias = []
            for d in factura.get("detalle") or []:
                valor = int(d.get("valor", 0) or 0)
                if d.get("metodo") == METODO_TRANSFERENCIA:
                    total_transferencia += valor
                    ref = d.get("referencia", "").strip()
                    if ref and ref.upper() not in (REFERENCIA_N_A, "", "NINGUNA") and ref not in referencias:
                        referencias.append(ref)
                else:
                    total_efectivo += valor
            ctx["total_efectivo"] = total_efectivo
            ctx["total_transferencia"] = total_transferencia
            ctx["referencias_transferencia"] = referencias

        template_map = {"cliente": "factura_cliente.html", "vendedor": "factura_vendedor.html"}
        template = template_map.get(factura.get("tipo", ""), "factura_cliente.html")
        return render_template(template, **ctx)

    @app.route("/facturas/<int:factura_id>/anular", methods=["POST"])
    @role_required("admin")
    def anular_factura(factura_id):
        require_collections()
        factura = facturas.find_one({"_id": factura_id})
        if not factura:
            abort(404)
        if factura.get("anulada"):
            flash("La factura ya fue anulada.", "warning")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        motivo = request.form.get("motivo", "").strip()
        if not motivo:
            flash("Debe indicar el motivo de la anulaci\u00f3n.", "danger")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        user = (current_user() or {}).get("username", USUARIO_SISTEMA)

        config_local = get_config()
        valor_boleta_local = int(config_local["valor_boleta"])

        for boleta_id in factura.get("boletas", []):
            boletas.update_one(
                {"_id": boleta_id},
                [
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
                    {
                        "$set": {
                            "estado": estado_pipeline_expr(valor_boleta_local)
                        }
                    },
                ],
            )

        facturas.update_one(
            {"_id": factura_id},
            {"$set": {
                "anulada": True,
                "anulada_en": now_local(),
                "anulada_por": user,
                "motivo_anulacion": motivo,
            }},
        )
        flash(f"Factura N\u00b0 {factura_id:05d} anulada.", "success")
        return redirect(url_for("ver_factura", factura_id=factura_id))



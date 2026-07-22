import re

from motores.constants import COMISION_DEFAULT_TIERS, VENDEDOR_LOCAL, METODO_EFECTIVO
from motores.fechas import now_local
from motores.validacion import parse_money

from motores.shared import (
    boletas, vendedores, facturas,
    request, flash, redirect, render_template, url_for,
    require_collections, role_required,
    next_factura_id, calc_comision_por_boleta, get_config,
    registrar_abono_lote,
)


def register_routes(app):

    @app.route("/facturas/nueva/vendedor", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def nueva_factura_vendedor():
        require_collections()
        if request.method == "POST":
            vendedor_id = request.form.get("vendedor_id", "").strip()
            fecha = request.form.get("fecha", "").strip()
            boletas_raw = request.form.getlist("boleta[]")
            montos_raw = request.form.getlist("monto[]")
            metodos = request.form.getlist("metodo[]")
            referencias = request.form.getlist("referencia[]")

            if not vendedor_id:
                flash("Debe seleccionar un vendedor.", "danger")
                return redirect(url_for("nueva_factura_vendedor"))
            if not fecha:
                flash("Debe indicar la fecha del abono.", "danger")
                return redirect(url_for("nueva_factura_vendedor"))

            rows = []
            for i in range(len(boletas_raw)):
                parts = [p.strip() for p in re.split(r"[\s,;]+", boletas_raw[i]) if p.strip().isdigit()]
                if not parts:
                    continue
                m = parse_money(montos_raw[i]) if i < len(montos_raw) else 0
                if m <= 0:
                    continue
                meta = metodos[i] if i < len(metodos) else METODO_EFECTIVO
                ref = referencias[i].strip() if i < len(referencias) else ""
                for p in parts:
                    rows.append({
                        "boleta": int(p),
                        "monto": m,
                        "metodo": meta,
                        "referencia": ref,
                    })

            if not rows:
                flash("Debe incluir al menos una boleta con un abono v\u00e1lido.", "danger")
                return redirect(url_for("nueva_factura_vendedor"))

            seen = set()
            deduped = []
            for r in rows:
                if r["boleta"] not in seen:
                    seen.add(r["boleta"])
                    deduped.append(r)
            rows = deduped

            boleta_ids = [r["boleta"] for r in rows]
            docs_map = {d["_id"]: d for d in boletas.find({"_id": {"$in": boleta_ids}})}
            if len(docs_map) != len(boleta_ids):
                missing = [b for b in boleta_ids if b not in docs_map]
                flash(f"Boletas no encontradas: {', '.join(f'{b:04d}' for b in missing)}", "danger")
                return redirect(url_for("nueva_factura_vendedor"))

            ajenas = [b for b in boleta_ids if docs_map[b].get("vendedor_id", "") != vendedor_id]
            if ajenas:
                detalles = []
                for b in ajenas:
                    d = docs_map[b]
                    actual = d.get("vendedor_id", "")
                    actual_nombre = actual
                    if actual and actual not in ("", VENDEDOR_LOCAL):
                        vd = vendedores.find_one({"_id": actual}, {"nombre": 1})
                        if vd:
                            actual_nombre = vd.get("nombre", actual)
                    detalles.append(f"#{b:04d} ({actual_nombre})")
                flash(f"Boletas que no pertenecen a este vendedor: {', '.join(detalles)}", "danger")
                return redirect(url_for("nueva_factura_vendedor"))

            pagadas = [b for b in boleta_ids if docs_map[b].get("estado") == "pagada"]
            if pagadas:
                flash(f"Boletas ya pagadas: {', '.join(f'{b:04d}' for b in pagadas)}", "danger")
                return redirect(url_for("nueva_factura_vendedor"))

            factura_id = next_factura_id()

            for r in rows:
                registrar_abono_lote(
                    [r["boleta"]],
                    {"fecha": fecha, "metodo": r["metodo"], "referencia": r["referencia"]},
                    r["monto"],
                    factura_id=factura_id,
                )

            docs = list(boletas.find({"_id": {"$in": boleta_ids}}, sort=[("_id", 1)]))
            detalle = []
            for doc in docs:
                for pago in doc.get("historial_pagos") or []:
                    if pago.get("factura_id") == factura_id:
                        detalle.append({
                            "boleta": doc["_id"],
                            "fecha": str(pago.get("fecha", "")),
                            "valor": int(pago.get("valor", 0) or 0),
                            "metodo": pago.get("metodo", ""),
                            "referencia": pago.get("referencia", ""),
                        })
            valor_total = sum(d["valor"] for d in detalle)

            v = vendedores.find_one({"_id": vendedor_id})
            v_nombre = v.get("nombre", vendedor_id) if v else vendedor_id
            v_telefono = v.get("telefono", "") if v else ""

            config = get_config()
            valor_boleta = int(config["valor_boleta"])
            tiers = config.get("comisiones_tiers", COMISION_DEFAULT_TIERS)
            existing_vendidas = boletas.count_documents({
                "vendedor_id": vendedor_id,
                "_id": {"$nin": boleta_ids},
                "total_abonado": {"$gte": valor_boleta},
            })
            pagadas_en_lote = boletas.count_documents({
                "_id": {"$in": boleta_ids},
                "total_abonado": {"$gte": valor_boleta},
            })
            total_vendidas = existing_vendidas + pagadas_en_lote
            comision_por_boleta = calc_comision_por_boleta(total_vendidas, tiers)
            total_comision = total_vendidas * comision_por_boleta

            factura = {
                "_id": factura_id,
                "tipo": "vendedor",
                "fecha": now_local(),
                "boletas": sorted(boleta_ids),
                "detalle": detalle,
                "valor_total": valor_total,
                "vendedor_id": vendedor_id,
                "vendedor_nombre": v_nombre,
                "vendedor_telefono": v_telefono,
                "comision_por_boleta": comision_por_boleta,
                "total_comision": total_comision,
                "total_vendidas": total_vendidas,
            }
            facturas.insert_one(factura)
            flash(f"Factura de vendedor N\u00b0 {factura['_id']:05d} generada.", "success")
            return redirect(url_for("ver_factura", factura_id=factura["_id"]))

        vendedores_list = list(vendedores.find().sort("_id", 1))
        today = now_local().strftime("%Y-%m-%d")
        return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today)

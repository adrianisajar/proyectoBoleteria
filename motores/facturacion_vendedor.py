import re

from motores.constants import COMISION_DEFAULT_TIERS, VENDEDOR_LOCAL, METODO_EFECTIVO, METODO_TRANSFERENCIA
from motores.fechas import now_local
from motores.validacion import parse_money

from motores.shared import (
    boletas, vendedores, facturas,
    request, flash, redirect, render_template, url_for,
    require_collections, role_required,
    next_factura_id, calc_comision_por_boleta, get_config,
    registrar_abono_lote,
    rollback_pagos_por_factura,
)


def _build_form_data(vendedor_id, fecha, boletas_raw, montos_raw, metodos, referencias, bancos):
    v_nombre = ""
    if vendedor_id:
        v = vendedores.find_one({"_id": vendedor_id}, {"nombre": 1})
        if v:
            v_nombre = v.get("nombre", vendedor_id)
    form_rows = []
    for i in range(len(boletas_raw)):
        raw = boletas_raw[i].strip()
        if not raw:
            continue
        form_rows.append({
            "boleta": raw,
            "monto": montos_raw[i] if i < len(montos_raw) else "",
            "metodo": metodos[i] if i < len(metodos) else "efectivo",
            "referencia": referencias[i] if i < len(referencias) else "",
            "banco": bancos[i] if i < len(bancos) else "",
        })
    return {
        "vendedor_id": vendedor_id,
        "vendedor_nombre": v_nombre,
        "fecha": fecha,
        "form_rows": form_rows,
    }


def _render_vendedor_form(form_data):
    vendedores_list = list(vendedores.find().sort("_id", 1))
    today = now_local().strftime("%Y-%m-%d")
    return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today, form_data=form_data)


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
            bancos = request.form.getlist("banco[]")

            form_data = _build_form_data(vendedor_id, fecha, boletas_raw, montos_raw, metodos, referencias, bancos)

            if not vendedor_id:
                flash("Debe seleccionar un vendedor.", "danger")
                return _render_vendedor_form(form_data)
            if not fecha:
                flash("Debe indicar la fecha del abono.", "danger")
                return _render_vendedor_form(form_data)

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
                banco_val = bancos[i].strip() if i < len(bancos) else ""
                for p in parts:
                    rows.append({
                        "boleta": int(p),
                        "monto": m,
                        "metodo": meta,
                        "referencia": ref,
                        "banco": banco_val,
                    })

            errors = []
            for r in rows:
                if r["metodo"] == METODO_TRANSFERENCIA:
                    if not r.get("referencia", "").strip():
                        errors.append(f"Referencia obligatoria para transferencia en boleta #{r['boleta']:04d}.")
                    if not r.get("banco", "").strip():
                        errors.append(f"Banco obligatorio para transferencia en boleta #{r['boleta']:04d}.")

            if not errors:
                seen_refs = set()
                for r in rows:
                    if r["metodo"] == METODO_TRANSFERENCIA:
                        ref_key = (r["referencia"].strip(), r["banco"].strip())
                        if ref_key in seen_refs:
                            continue
                        seen_refs.add(ref_key)
                        elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": r["referencia"].strip()}
                        if r["banco"].strip():
                            elem_match["banco"] = r["banco"].strip()
                        dup = boletas.find_one({"historial_pagos": {"$elemMatch": elem_match}}, {"_id": 1})
                        if dup:
                            errors.append(f"Ya existe un pago por transferencia con referencia {r['referencia'].strip()} y banco {r['banco'].strip()} (boleta #{dup['_id']:04d}).")

            if errors:
                for e in errors:
                    flash(e, "danger")
                return _render_vendedor_form(form_data)

            if not rows:
                flash("Debe incluir al menos una boleta con un abono v\u00e1lido.", "danger")
                return _render_vendedor_form(form_data)

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
                return _render_vendedor_form(form_data)

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
                return _render_vendedor_form(form_data)

            pagadas = [b for b in boleta_ids if docs_map[b].get("estado") == "pagada"]
            if pagadas:
                flash(f"Boletas ya pagadas: {', '.join(f'{b:04d}' for b in pagadas)}", "danger")
                return _render_vendedor_form(form_data)

            factura_id = None
            valor_boleta = None
            try:
                factura_id = next_factura_id()
                config = get_config()
                valor_boleta = int(config["valor_boleta"])

                for r in rows:
                    registrar_abono_lote(
                        [r["boleta"]],
                        {"fecha": fecha, "metodo": r["metodo"], "referencia": r["referencia"], "banco": r.get("banco", "")},
                        r["monto"],
                        factura_id=factura_id,
                    )

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
                valor_total = sum(d["valor"] for d in detalle)

                v = vendedores.find_one({"_id": vendedor_id})
                v_nombre = v.get("nombre", vendedor_id) if v else vendedor_id
                v_telefono = v.get("telefono", "") if v else ""

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

            except Exception as exc:
                if factura_id is not None:
                    rollback_pagos_por_factura(factura_id, valor_boleta)
                flash(f"Error al generar la factura: {exc}", "danger")
                return _render_vendedor_form(form_data)

        vendedores_list = list(vendedores.find().sort("_id", 1))
        today = now_local().strftime("%Y-%m-%d")
        return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today, form_data={})

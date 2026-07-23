from datetime import datetime

from motores.constants import VENDEDOR_LOCAL, METODO_EFECTIVO, METODO_TRANSFERENCIA
from motores.fechas import now_local
from motores.validacion import parse_money

from motores.shared import (
    boletas, facturas,
    request, flash, redirect, render_template, url_for,
    require_collections, role_required,
    registrar_abono_lote, next_factura_id, build_abono_preview,
    rollback_pagos_por_factura,
    get_config,
    estado_pipeline_expr,
)


def _build_cliente_form_rows(boletas_raw, montos_raw, metodos, referencias, bancos):
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
    return form_rows


def _render_cliente_form(form, form_rows, today):
    form_data = {
        "fecha": form.get("fecha", ""),
        "form_rows": form_rows,
    }
    return render_template("nueva_factura_cliente.html", form=form, today=today, form_data=form_data)


def register_routes(app):

    @app.route("/facturas/nueva/cliente", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def nueva_factura_cliente():
        require_collections()
        today = now_local().strftime("%Y-%m-%d")
        form = {"nombre": "", "telefono": "", "direccion": ""}

        if request.method == "POST":
            nombre = request.form.get("nombre", "").strip().upper()
            telefono = request.form.get("telefono", "").strip()
            direccion = request.form.get("direccion", "").strip().upper()
            fecha_str = request.form.get("fecha", "").strip()

            form = {"nombre": nombre, "telefono": telefono, "direccion": direccion, "fecha": fecha_str}

            boletas_raw = request.form.getlist("boleta[]")
            montos_raw = request.form.getlist("monto[]")
            metodos = request.form.getlist("metodo[]")
            referencias = request.form.getlist("referencia[]")
            bancos = request.form.getlist("banco[]")

            form_rows = _build_cliente_form_rows(boletas_raw, montos_raw, metodos, referencias, bancos)

            errors = []
            if not nombre:
                errors.append("El nombre del cliente es obligatorio.")
            if not fecha_str:
                errors.append("Debe indicar la fecha del abono.")

            rows = []
            for i in range(len(boletas_raw)):
                raw = boletas_raw[i].strip()
                if not raw:
                    continue
                try:
                    num = int(raw)
                except (ValueError, TypeError):
                    errors.append(f"'{raw}' no es un n\u00famero de boleta v\u00e1lido.")
                    continue
                if num < 0 or num > 9999:
                    errors.append(f"#{num:04d} est\u00e1 fuera del rango 0000-9999.")
                    continue
                m = parse_money(montos_raw[i]) if i < len(montos_raw) else 0
                meta = metodos[i].strip().lower() if i < len(metodos) else METODO_EFECTIVO
                ref = referencias[i].strip() if i < len(referencias) else ""
                banco_val = bancos[i].strip() if i < len(bancos) else ""
                rows.append({
                    "boleta": num,
                    "monto": m,
                    "metodo": meta,
                    "referencia": ref,
                    "banco": banco_val,
                })

            if not rows:
                errors.append("Ingrese al menos una boleta.")

            for r in rows:
                if r["metodo"] == METODO_TRANSFERENCIA:
                    if not r.get("referencia", "").strip():
                        errors.append(f"Referencia obligatoria para transferencia en boleta #{r['boleta']:04d}.")
                    if not r.get("banco", "").strip():
                        errors.append(f"Banco obligatorio para transferencia en boleta #{r['boleta']:04d}.")

            if not errors:
                for r in rows:
                    if r["metodo"] == METODO_TRANSFERENCIA:
                        ref = r["referencia"].strip()
                        banco_val = r["banco"].strip()
                        elem_match = {"metodo": METODO_TRANSFERENCIA, "referencia": ref}
                        if banco_val:
                            elem_match["banco"] = banco_val
                        dup = boletas.find_one({"historial_pagos": {"$elemMatch": elem_match}}, {"_id": 1})
                        if dup:
                            errors.append(f"Ya existe un pago por transferencia con referencia {ref} y banco {banco_val} (boleta #{dup['_id']:04d}).")

            if errors:
                for e in errors:
                    flash(e, "danger")
                return _render_cliente_form(form, form_rows, today)

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
                return _render_cliente_form(form, form_rows, today)

            pagadas = [b for b in boleta_ids if docs_map[b].get("estado") == "pagada"]
            if pagadas:
                flash(f"Boletas ya pagadas: {', '.join(f'{b:04d}' for b in pagadas)}", "danger")
                return _render_cliente_form(form, form_rows, today)

            factura_id = None
            valor_boleta_local = None
            try:
                factura_id = next_factura_id()
                config_local = get_config()
                valor_boleta_local = int(config_local["valor_boleta"])

                for r in rows:
                    if r["monto"] > 0:
                        pago_form = {
                            "boletas": f"{r['boleta']:04d}",
                            "valor": str(r["monto"]),
                            "fecha": fecha_str,
                            "metodo": r["metodo"],
                            "referencia": r["referencia"],
                            "banco": r.get("banco", ""),
                        }
                        _form_data, preview = build_abono_preview(pago_form, factura_id=factura_id)
                        if not preview.get("can_confirm"):
                            raise ValueError("; ".join(preview.get("errors", [])))
                        registrar_abono_lote([r["boleta"]], _form_data, preview["valor_abono"], factura_id=factura_id)

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

                factura = {
                    "_id": factura_id,
                    "tipo": "cliente",
                    "fecha": datetime.strptime(fecha_str, "%Y-%m-%d"),
                    "boletas": sorted(boleta_ids),
                    "detalle": detalle,
                    "valor_total": valor_total,
                    "cliente": {"nombre": nombre, "telefono": telefono, "direccion": direccion},
                    "vendedor_id": VENDEDOR_LOCAL,
                    "vendedor_nombre": VENDEDOR_LOCAL,
                }
                facturas.insert_one(factura)

                cliente_data = {"nombre": nombre, "telefono": telefono, "direccion": direccion}
                boletas.update_many(
                    {"_id": {"$in": boleta_ids}},
                    {"$set": {"cliente": cliente_data}},
                )
                boletas.update_many(
                    {"_id": {"$in": boleta_ids}, "vendedor_id": {"$in": ["", None, VENDEDOR_LOCAL]}},
                    {"$set": {"vendedor_id": VENDEDOR_LOCAL}},
                )
                boletas.update_many(
                    {"_id": {"$in": boleta_ids}},
                    [{"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}}],
                )

                flash(f"Factura de cliente generada con {len(boleta_ids)} boleta(s).", "success")
                return redirect(url_for("ver_factura", factura_id=factura["_id"], imprimir=1))

            except Exception as exc:
                if factura_id is not None:
                    rollback_pagos_por_factura(factura_id, valor_boleta_local)
                    try:
                        boletas.update_many(
                            {"_id": {"$in": boleta_ids}},
                            {"$set": {"cliente": {"nombre": "", "telefono": "", "direccion": ""}}},
                        )
                        boletas.update_many(
                            {"_id": {"$in": boleta_ids}, "vendedor_id": VENDEDOR_LOCAL, "total_abonado": 0},
                            {"$set": {"vendedor_id": ""}},
                        )
                        boletas.update_many(
                            {"_id": {"$in": boleta_ids}},
                            [{"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}}],
                        )
                    except Exception:
                        pass
                flash(f"Error al generar la factura: {exc}", "danger")
                return _render_cliente_form(form, form_rows, today)

        boleta_query = request.args.get("boletas", "").strip()
        form_rows = []
        if boleta_query:
            for part in boleta_query.split(","):
                part = part.strip()
                if part:
                    form_rows.append({
                        "boleta": part,
                        "monto": "",
                        "metodo": "efectivo",
                        "referencia": "",
                        "banco": "",
                    })
        return _render_cliente_form(form, form_rows, today)

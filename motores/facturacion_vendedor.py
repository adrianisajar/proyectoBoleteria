import re

from datetime import datetime

from motores.constants import COMISION_DEFAULT_TIERS, VENDEDOR_LOCAL, METODO_EFECTIVO, METODO_TRANSFERENCIA, USUARIO_SISTEMA
from motores.fechas import now_local
from motores.validacion import parse_money

from motores.shared import (
    boletas, vendedores, facturas,
    request, flash, redirect, render_template, url_for,
    require_collections, role_required,
    next_factura_id, calc_comision_por_boleta, get_config,
    current_user, estado_pipeline_expr, invalidate_dashboard_cache,
    rollback_pagos_por_factura,
    _buscar_transferencia_duplicada,
    _build_factura_detalle,
)
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError


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


def _render_vendedor_form(form_data, vendedores_list=None):
    if vendedores_list is None:
        vendedores_list = list(vendedores.find().sort("_id", 1))
    today = now_local().strftime("%Y-%m-%d")
    _cfg = get_config()
    _vb = int(_cfg.get("valor_boleta", 10000) or 10000)
    return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today, form_data=form_data, valor_boleta=_vb)


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
            _vendedores_list = list(vendedores.find().sort("_id", 1))

            if not vendedor_id:
                flash("Debe seleccionar un vendedor.", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)
            if not fecha:
                flash("Debe indicar la fecha del abono.", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)
            try:
                fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
                if fecha_dt.date() > now_local().date():
                    flash("La fecha no puede ser posterior a hoy.", "danger")
                    return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)
            except ValueError:
                flash("Formato de fecha inv\u00e1lido.", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            _cfg = get_config()
            _vb = int(_cfg.get("valor_boleta", 10000) or 10000)

            errors = []
            rows = []
            for i in range(len(boletas_raw)):
                parts = [p.strip() for p in re.split(r"[\s,;]+", boletas_raw[i]) if p.strip().isdigit()]
                if not parts:
                    continue
                m = parse_money(montos_raw[i]) if i < len(montos_raw) else 0
                if m <= 0:
                    continue
                if m > _vb * len(parts):
                    errors.append(f"El monto ${m:,} para {len(parts)} boleta(s) supera el m\u00e1ximo de ${_vb * len(parts):,}.")
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
                        dup = _buscar_transferencia_duplicada(r["referencia"].strip(), r["banco"].strip())
                        if dup:
                            errors.append(f"Ya existe un pago por transferencia con referencia {r['referencia'].strip()} y banco {r['banco'].strip()} (boleta #{dup['_id']:04d}).")

            if errors:
                for e in errors:
                    flash(e, "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            if not rows:
                flash("Debe incluir al menos una boleta con un abono v\u00e1lido.", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

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
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

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
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            pagadas = [b for b in boleta_ids if docs_map[b].get("estado") == "pagada"]
            if pagadas:
                flash(f"Boletas ya pagadas: {', '.join(f'{b:04d}' for b in pagadas)}", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            factura_id = None
            valor_boleta = None
            try:
                factura_id = next_factura_id()
                config = get_config()
                valor_boleta = int(config["valor_boleta"])

                v = vendedores.find_one({"_id": vendedor_id})
                v_nombre = v.get("nombre", vendedor_id) if v else vendedor_id
                v_telefono = v.get("telefono", "") if v else ""

                # ── 1. Crear factura "pendiente" antes de tocar las boletas ──
                facturas.insert_one({
                    "_id": factura_id,
                    "tipo": "vendedor",
                    "estado": "pendiente",
                    "fecha": now_local() if fecha_dt.date() == now_local().date() else fecha_dt,
                    "boletas": sorted(boleta_ids),
                    "detalle": [],
                    "valor_total": 0,
                    "vendedor_id": vendedor_id,
                    "vendedor_nombre": v_nombre,
                    "vendedor_telefono": v_telefono,
                })

                # ── 2. Validar montos (sin viajes extra a MongoDB) ──
                sobrepasan = []
                for r in rows:
                    doc = docs_map.get(r["boleta"])
                    if not doc:
                        continue
                    actual = doc.get("total_abonado", 0) or 0
                    if actual + r["monto"] > valor_boleta:
                        sobrepasan.append(r["boleta"])
                if sobrepasan:
                    raise ValueError(
                        f"El abono excede el saldo pendiente en {len(sobrepasan)} boleta(s): "
                        f"{', '.join(f'#{b:04d}' for b in sobrepasan)}."
                    )

                # ── 3. Un solo bulk_write con todos los pagos ──
                ops = []
                usuario = (current_user() or {}).get("username", USUARIO_SISTEMA)
                for r in rows:
                    pago = {
                        "fecha": fecha,
                        "valor": r["monto"],
                        "metodo": r["metodo"],
                        "registrado_en": now_local(),
                        "usuario": usuario,
                        "factura_id": factura_id,
                    }
                    if r["metodo"] == METODO_TRANSFERENCIA:
                        pago["referencia"] = r.get("referencia", "")
                        if r.get("banco"):
                            pago["banco"] = r["banco"]

                    ops.append(UpdateOne(
                        {"_id": r["boleta"], "estado": {"$ne": "pagada"}},
                        [
                            {"$set": {
                                "historial_pagos": {"$concatArrays": [
                                    {"$ifNull": ["$historial_pagos", []]},
                                    {"$literal": [pago]},
                                ]},
                                "total_abonado": {"$add": [
                                    {"$ifNull": ["$total_abonado", 0]},
                                    r["monto"],
                                ]},
                            }},
                            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
                        ],
                    ))

                try:
                    boletas.bulk_write(ops, ordered=False)
                except BulkWriteError:
                    rollback_pagos_por_factura(factura_id, valor_boleta)
                    facturas.update_one(
                        {"_id": factura_id},
                        {"$set": {"estado": "error", "error": "Error de escritura en MongoDB durante el bulk_write."}},
                    )
                    flash(
                        f"Error al registrar los pagos (factura #{factura_id:05d}). "
                        "Los pagos se revirtieron automáticamente. Intente de nuevo.",
                        "danger",
                    )
                    return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

                invalidate_dashboard_cache()

                # ── 4. Calcular comisiones (después de los pagos) ──
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

                # ── 5. Construir detalle y finalizar factura ──
                detalle = _build_factura_detalle(boleta_ids, factura_id)
                valor_total = sum(d["valor"] for d in detalle)

                facturas.update_one(
                    {"_id": factura_id},
                    {"$set": {
                        "detalle": detalle,
                        "valor_total": valor_total,
                        "estado": "completa",
                        "comision_por_boleta": comision_por_boleta,
                        "total_comision": total_comision,
                        "total_vendidas": total_vendidas,
                    }},
                )

                flash(f"Factura de vendedor N\u00b0 {factura_id:05d} generada.", "success")
                return redirect(url_for("ver_factura", factura_id=factura_id, imprimir=1))

            except Exception as exc:
                if factura_id is not None:
                    rollback_pagos_por_factura(factura_id, valor_boleta)
                    facturas.update_one(
                        {"_id": factura_id},
                        {"$set": {"estado": "error", "error": str(exc)[:200]}},
                    )
                flash(f"Error al generar la factura: {exc}", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

        vendedores_list = list(vendedores.find().sort("_id", 1))
        today = now_local().strftime("%Y-%m-%d")
        _cfg = get_config()
        _vb = int(_cfg.get("valor_boleta", 10000) or 10000)
        return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today, form_data={}, valor_boleta=_vb)

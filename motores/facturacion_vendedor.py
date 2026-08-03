import re
from collections import Counter
from datetime import datetime

from flask import Flask, Response
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from motores.constants import COMISION_DEFAULT_TIERS, METODO_EFECTIVO, METODO_TRANSFERENCIA, USUARIO_SISTEMA, VENDEDOR_LOCAL
from motores.facturacion_common import deduplicar_filas_boleta, validar_filas_transferencia, verificar_boletas_existen
from motores.fechas import now_local
from motores.shared import (
    boletas,
    build_factura_detalle,
    calc_comision_por_boleta,
    current_user,
    estado_pipeline_expr,
    facturas,
    flash,
    get_config,
    invalidate_dashboard_cache,
    next_factura_id,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    rollback_pagos_por_factura,
    url_for,
    vendedores,
)
from motores.validacion import parse_money


def _build_form_data(
    vendedor_id: str, fecha: str, boletas_raw: list[str], montos_raw: list[str], metodos: list[str], referencias: list[str], bancos: list[str]
) -> dict:
    """Build the vendor invoice form context from parallel POST lists."""
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
        form_rows.append(
            {
                "boleta": raw,
                "monto": montos_raw[i] if i < len(montos_raw) else "",
                "metodo": metodos[i] if i < len(metodos) else "efectivo",
                "referencia": referencias[i] if i < len(referencias) else "",
                "banco": bancos[i] if i < len(bancos) else "",
            }
        )
    return {
        "vendedor_id": vendedor_id,
        "vendedor_nombre": v_nombre,
        "fecha": fecha,
        "form_rows": form_rows,
    }


def _render_vendedor_form(form_data: dict, vendedores_list: list | None = None) -> str:
    """Render the vendor invoice form with the given values for a re-render."""
    if vendedores_list is None:
        vendedores_list = list(vendedores.find().sort("_id", 1))
    today = now_local().strftime("%Y-%m-%d")
    _cfg = get_config()
    _vb = int(_cfg.get("valor_boleta", 10000) or 10000)
    return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today, form_data=form_data, valor_boleta=_vb)


def register_routes(app: Flask) -> None:
    """Register the vendor invoice creation route."""

    @app.route("/facturas/nueva/vendedor", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def nueva_factura_vendedor() -> str | Response:
        """Create a vendor invoice from dynamic rows; register payments + commission."""
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
                    rows.append(
                        {
                            "boleta": int(p),
                            "monto": m,
                            "metodo": meta,
                            "referencia": ref,
                            "banco": banco_val,
                        }
                    )

            validar_filas_transferencia(rows, errors)

            if errors:
                for e in errors:
                    flash(e, "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            if not rows:
                flash("Debe incluir al menos una boleta con un abono v\u00e1lido.", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            contador = Counter(r["boleta"] for r in rows)
            duplicadas = [b for b, cnt in contador.items() if cnt > 1]
            if duplicadas:
                flash(f"Boletas duplicadas en el abono: {', '.join(f'{b:04d}' for b in sorted(duplicadas))}. Elimine las repeticiones.", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            rows, _ = deduplicar_filas_boleta(rows)

            boleta_ids = [r["boleta"] for r in rows]
            docs_map, missing = verificar_boletas_existen(boleta_ids)
            if missing:
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
                facturas.insert_one(
                    {
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
                    }
                )

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
                    raise ValueError(f"El abono excede el saldo pendiente en {len(sobrepasan)} boleta(s): {', '.join(f'#{b:04d}' for b in sobrepasan)}.")

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

                    ops.append(
                        UpdateOne(
                            {"_id": r["boleta"], "estado": {"$ne": "pagada"}},
                            [
                                {
                                    "$set": {
                                        "historial_pagos": {
                                            "$concatArrays": [
                                                {"$ifNull": ["$historial_pagos", []]},
                                                {"$literal": [pago]},
                                            ]
                                        },
                                        "total_abonado": {
                                            "$add": [
                                                {"$ifNull": ["$total_abonado", 0]},
                                                r["monto"],
                                            ]
                                        },
                                    }
                                },
                                {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
                            ],
                        )
                    )

                try:
                    boletas.bulk_write(ops, ordered=False)
                except BulkWriteError:
                    rollback_pagos_por_factura(factura_id, valor_boleta)
                    facturas.update_one(
                        {"_id": factura_id},
                        {"$set": {"estado": "error", "error": "Error de escritura en MongoDB durante el bulk_write."}},
                    )
                    flash(
                        f"Error al registrar los pagos (factura #{factura_id:05d}). Los pagos se revirtieron automáticamente. Intente de nuevo.",
                        "danger",
                    )
                    return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

                invalidate_dashboard_cache()

                # ── 4. Calcular comisiones (después de los pagos) ──
                tiers = config.get("comisiones_tiers", COMISION_DEFAULT_TIERS)
                existing_vendidas = boletas.count_documents(
                    {
                        "vendedor_id": vendedor_id,
                        "_id": {"$nin": boleta_ids},
                        "total_abonado": {"$gte": valor_boleta},
                    }
                )
                pagadas_en_lote = boletas.count_documents(
                    {
                        "_id": {"$in": boleta_ids},
                        "total_abonado": {"$gte": valor_boleta},
                    }
                )
                total_vendidas = existing_vendidas + pagadas_en_lote
                comision_por_boleta = calc_comision_por_boleta(total_vendidas, tiers)
                total_comision = total_vendidas * comision_por_boleta

                # ── 5. Construir detalle y finalizar factura ──
                detalle = build_factura_detalle(boleta_ids, factura_id)
                valor_total = sum(d["valor"] for d in detalle)

                facturas.update_one(
                    {"_id": factura_id},
                    {
                        "$set": {
                            "detalle": detalle,
                            "valor_total": valor_total,
                            "estado": "completa",
                            "comision_por_boleta": comision_por_boleta,
                            "total_comision": total_comision,
                            "total_vendidas": total_vendidas,
                        }
                    },
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

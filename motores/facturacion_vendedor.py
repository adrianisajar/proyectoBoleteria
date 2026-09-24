import re
from collections import Counter
from datetime import datetime

from flask import Flask, Response, current_app
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError

from motores.constants import (
    COMISION_DEFAULT_TIERS,
    METODO_EFECTIVO,
    METODO_PAGO_DELIO,
    METODO_TRANSFERENCIA,
    MOV_PAGO,
    MOVIMIENTOS_FIELD,
    USUARIO_SISTEMA,
    VENDEDOR_LOCAL,
    VENDEDOR_LOCAL_LABEL,
)
from motores.facturacion_common import deduplicar_filas_boleta, validar_filas_transferencia, verificar_boletas_existen
from motores.fechas import now_local
from motores.shared import (
    boletas,
    build_factura_detalle,
    buscar_transferencia_duplicada,
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
    vendedores_con_local,
)
from motores.validacion import es_boleta_completa, parse_money, safe_error_message


def _build_form_data(
    vendedor_id: str, fecha: str, boletas_raw: list[str], montos_raw: list[str], metodos: list[str], referencias: list[str], bancos: list[str]
) -> dict:
    """Build the vendor invoice form context from parallel POST lists."""
    v_nombre = ""
    if vendedor_id == VENDEDOR_LOCAL:
        v_nombre = VENDEDOR_LOCAL_LABEL
    elif vendedor_id:
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


def _render_vendedor_form(form_data: dict, vendedores_list: list | None = None, confirm_pagadas: list[dict] | None = None) -> str:
    """Render the vendor invoice form with the given values for a re-render."""
    if vendedores_list is None:
        vendedores_list = vendedores_con_local()
    today = now_local().strftime("%Y-%m-%d")
    _cfg = get_config()
    _vb = int(_cfg.get("valor_boleta", 10000) or 10000)
    return render_template(
        "nueva_factura_vendedor.html",
        vendedores=vendedores_list,
        today=today,
        form_data=form_data,
        valor_boleta=_vb,
        confirm_pagadas=confirm_pagadas or [],
    )


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
            confirmar_pagadas = request.form.get("confirmar_pagadas", "") == "1"

            form_data = _build_form_data(vendedor_id, fecha, boletas_raw, montos_raw, metodos, referencias, bancos)
            _vendedores_list = vendedores_con_local()

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
                tokens = [p.strip() for p in re.split(r"[\s,;]+", boletas_raw[i]) if p.strip()]
                incompletas = [t for t in tokens if not es_boleta_completa(t)]
                parts = [t for t in tokens if es_boleta_completa(t)]
                if incompletas:
                    errors.append(f"Boleta(s) con formato inv\u00e1lido (escriba los 4 d\u00edgitos): {', '.join(incompletas[:8])}.")
                    continue
                if not parts:
                    continue
                m = parse_money(montos_raw[i]) if i < len(montos_raw) else 0
                if m <= 0:
                    raw_boleta = boletas_raw[i].strip() if i < len(boletas_raw) else "-"
                    errors.append(f"El monto para la boleta {raw_boleta} debe ser mayor que cero.")
                    continue
                if m > _vb:
                    errors.append(f"El monto ${m:,} supera el valor de la boleta (${_vb:,}). El abono se aplica a cada boleta escrita.")
                    continue
                meta = metodos[i].strip().lower() if i < len(metodos) else METODO_EFECTIVO
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

            contador = Counter((r["boleta"], r["metodo"]) for r in rows)
            duplicadas = [b for (b, _), cnt in contador.items() if cnt > 1]
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
                unique_vids = {docs_map[b].get("vendedor_id", "") for b in ajenas if docs_map[b].get("vendedor_id", "") not in ("", VENDEDOR_LOCAL)}
                vid_names: dict[str, str] = {}
                if unique_vids:
                    for vd in vendedores.find({"_id": {"$in": list(unique_vids)}}, {"_id": 1, "nombre": 1}):
                        vid_names[vd["_id"]] = vd.get("nombre", vd["_id"])
                detalles = []
                for b in ajenas:
                    d = docs_map[b]
                    actual = d.get("vendedor_id", "")
                    actual_nombre = vid_names.get(actual, actual) if actual and actual not in ("", VENDEDOR_LOCAL) else actual
                    detalles.append(f"#{b:04d} ({actual_nombre})")
                flash(f"Boletas que no pertenecen a este vendedor: {', '.join(detalles)}", "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

            pagadas = [b for b in boleta_ids if docs_map[b].get("estado") == "pagada"]
            if pagadas and not confirmar_pagadas:
                detalle_pagadas = [{"boleta": b, "total": int(docs_map[b].get("total_abonado") or 0)} for b in sorted(pagadas)]
                flash(
                    "Hay boletas ya pagadas en el abono. Confirme para registrar los pagos como excedente (seguirán mostrando estado pagada).",
                    "warning",
                )
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list, confirm_pagadas=detalle_pagadas)

            excedentes = []
            for r in rows:
                doc = docs_map.get(r["boleta"])
                if doc is None:
                    continue
                total_actual = int(doc.get("total_abonado") or 0)
                if total_actual + r["monto"] > _vb:
                    excedentes.append((r["boleta"], total_actual, r["monto"]))
            if excedentes:
                det = ", ".join(f"#{b:04d} (${t:,} + ${m:,} = ${t + m:,})" for b, t, m in excedentes[:8])
                flash(
                    f"El acumulado de las siguientes boletas excedería el valor de la boleta (${_vb:,}): {det}.",
                    "warning",
                )

            factura_id = None
            valor_boleta = _vb
            try:
                config = get_config()
                valor_boleta = int(config["valor_boleta"])

                v = vendedores.find_one({"_id": vendedor_id})
                if vendedor_id == VENDEDOR_LOCAL:
                    v_nombre = VENDEDOR_LOCAL_LABEL
                    v_telefono = ""
                else:
                    v_nombre = v.get("nombre", vendedor_id) if v else vendedor_id
                    v_telefono = v.get("telefono", "") if v else ""

                # ── 1. Reservar id + crear factura "pendiente" antes de tocar
                # las boletas (fail-fast; reintenta si el contador está desfasado
                # y otro proceso tomó el mismo id).
                user = current_user() or {}
                for _intento in range(5):
                    factura_id = next_factura_id()
                    try:
                        facturas.insert_one(
                            {
                                "_id": factura_id,
                                "tipo": "vendedor",
                                "estado": "pendiente",
                                "creada_en": now_local(),
                                "fecha": now_local() if fecha_dt.date() == now_local().date() else fecha_dt,
                                "boletas": sorted(boleta_ids),
                                "detalle": [],
                                "valor_total": 0,
                                "vendedor_id": vendedor_id,
                                "vendedor_nombre": v_nombre,
                                "vendedor_telefono": v_telefono,
                                "usuario_id": user.get("usuario_id"),
                                "usuario_nombre": user.get("nombre") or user.get("username"),
                            }
                        )
                        break
                    except DuplicateKeyError:
                        factura_id = None
                        continue
                if factura_id is None:
                    flash("No se pudo reservar el número de factura. Intente de nuevo.", "danger")
                    return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

                # ── 2. Un solo bulk_write con todos los pagos ──
                ops = []
                usuario = (current_user() or {}).get("username", USUARIO_SISTEMA)
                for r in rows:
                    pago = {
                        "tipo": MOV_PAGO,
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
                    elif r["metodo"] == METODO_PAGO_DELIO:
                        pago["referencia"] = "SIN REGISTRO"

                    ops.append(
                        UpdateOne(
                            {
                                "_id": r["boleta"],
                                # Con confirmación expresa, las boletas ya pagadas también
                                # reciben el pago (excedente); si no, se excluyen aquí.
                                **({} if confirmar_pagadas else {"estado": {"$ne": "pagada"}}),
                                # El acumulado de abonos sí puede superar el valor de
                                # la boleta; el tope es por pago individual (validado
                                # arriba: ningún monto supera el valor de la boleta).
                            },
                            [
                                {
                                    "$set": {
                                        MOVIMIENTOS_FIELD: {
                                            "$concatArrays": [
                                                {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]},
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
                    bulk_result = boletas.bulk_write(ops, ordered=False)
                except BulkWriteError:
                    try:
                        rollback_pagos_por_factura(factura_id, valor_boleta)
                    except Exception as rollback_exc:
                        current_app.logger.warning("Rollback falló tras BulkWriteError en factura %s: %s", factura_id, rollback_exc)
                    facturas.delete_one({"_id": factura_id, "estado": {"$ne": "completa"}})
                    flash(
                        f"Error al registrar los pagos. Se eliminó la factura #{factura_id:05d} y se revirtieron los pagos. Intente de nuevo.",
                        "danger",
                    )
                    return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

                if bulk_result.matched_count < len(ops):
                    omitidas = len(ops) - bulk_result.matched_count
                    try:
                        rollback_pagos_por_factura(factura_id, valor_boleta)
                    except Exception as rollback_exc:
                        current_app.logger.warning("Rollback falló tras matched_count en factura %s: %s", factura_id, rollback_exc)
                    facturas.delete_one({"_id": factura_id, "estado": {"$ne": "completa"}})
                    flash(
                        f"{omitidas} boleta(s) cambiaron de estado mientras se registraba (ya pagadas). "
                        f"Se eliminó la factura #{factura_id:05d} y se revirtieron los pagos. Intente de nuevo.",
                        "danger",
                    )
                    return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

                # Verificación post-escritura de referencias: si otra factura usó la
                # misma referencia+banco entre la validación y el bulk_write,
                # se revierte todo (exactly-once).
                refs_vistas = set()
                for r in rows:
                    if r["metodo"] != METODO_TRANSFERENCIA:
                        continue
                    clave = (r.get("referencia", ""), r.get("banco", ""))
                    if clave in refs_vistas:
                        continue
                    refs_vistas.add(clave)
                    if buscar_transferencia_duplicada(clave[0], clave[1], exclude_factura_id=factura_id):
                        try:
                            rollback_pagos_por_factura(factura_id, valor_boleta)
                        except Exception as rollback_exc:
                            current_app.logger.warning("Rollback falló tras ref duplicada en factura %s: %s", factura_id, rollback_exc)
                        facturas.delete_one({"_id": factura_id, "estado": {"$ne": "completa"}})
                        flash(
                            f"La referencia {clave[0]} fue registrada por otra factura mientras se procesaba. "
                            f"Se eliminó la factura #{factura_id:05d} y se revirtieron los pagos. Intente de nuevo.",
                            "danger",
                        )
                        return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

                invalidate_dashboard_cache()

                # ── 3. Calcular comisiones (después de los pagos) ──
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
                if vendedor_id == VENDEDOR_LOCAL:
                    comision_por_boleta = 0
                    total_comision = 0
                else:
                    comision_por_boleta = calc_comision_por_boleta(total_vendidas, tiers)
                    total_comision = total_vendidas * comision_por_boleta

                # ── 4. Construir detalle y finalizar factura ──
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
                return redirect(url_for("ver_factura", factura_id=factura_id))

            except Exception as exc:
                if factura_id is not None:
                    try:
                        rollback_pagos_por_factura(factura_id, valor_boleta)
                    except Exception as rollback_exc:
                        current_app.logger.warning("Rollback de pagos falló para factura %s: %s", factura_id, rollback_exc)
                    try:
                        facturas.delete_one({"_id": factura_id, "estado": {"$ne": "completa"}})
                    except Exception as del_exc:
                        current_app.logger.warning("Eliminación de factura pendiente %s falló: %s", factura_id, del_exc)
                flash(safe_error_message(exc), "danger")
                return _render_vendedor_form(form_data, vendedores_list=_vendedores_list)

        vendedores_list = vendedores_con_local()
        today = now_local().strftime("%Y-%m-%d")
        _cfg = get_config()
        _vb = int(_cfg.get("valor_boleta", 10000) or 10000)
        return render_template("nueva_factura_vendedor.html", vendedores=vendedores_list, today=today, form_data={}, valor_boleta=_vb)

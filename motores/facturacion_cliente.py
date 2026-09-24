from collections import Counter, defaultdict
from datetime import datetime

from flask import Flask, Response, current_app
from pymongo.errors import DuplicateKeyError

from motores.constants import METODO_EFECTIVO, VENDEDOR_LOCAL
from motores.facturacion_common import validar_filas_transferencia, verificar_boletas_existen
from motores.fechas import now_local
from motores.shared import (
    boletas,
    build_abono_preview,
    build_boletas_info_snapshot,
    build_factura_detalle,
    current_user,
    estado_pipeline_expr,
    facturas,
    flash,
    get_config,
    next_factura_id,
    redirect,
    registrar_abono_lote,
    render_template,
    request,
    require_collections,
    role_required,
    rollback_pagos_por_factura,
    url_for,
)
from motores.validacion import es_boleta_completa, parse_money, safe_error_message


def _build_cliente_form_rows(boletas_raw: list[str], montos_raw: list[str], metodos: list[str], referencias: list[str], bancos: list[str]) -> list[dict]:
    """Build form row dicts from parallel POST lists, skipping empty ticket numbers."""
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
    return form_rows


def _render_cliente_form(form: dict, form_rows: list[dict], today: str, confirm_pagadas: list[dict] | None = None) -> str:
    """Render the customer invoice form with the given values for a re-render."""
    form_data = {
        "fecha": form.get("fecha", ""),
        "form_rows": form_rows,
    }
    _cfg_cliente = get_config()
    _vb_cliente = int(_cfg_cliente.get("valor_boleta", 10000) or 10000)
    return render_template(
        "nueva_factura_cliente.html",
        form=form,
        today=today,
        form_data=form_data,
        valor_boleta=_vb_cliente,
        confirm_pagadas=confirm_pagadas or [],
    )


def register_routes(app: Flask) -> None:
    """Register the customer invoice creation route."""

    @app.route("/facturas/nueva/cliente", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def nueva_factura_cliente() -> str | Response:
        """Create a customer invoice from dynamic rows (boleta + monto + metodo)."""
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
            confirmar_pagadas = request.form.get("confirmar_pagadas", "") == "1"

            form_rows = _build_cliente_form_rows(boletas_raw, montos_raw, metodos, referencias, bancos)

            errors = []
            if not nombre:
                errors.append("El nombre del cliente es obligatorio.")
            if telefono and not all(c in "0123456789 +-()" for c in telefono):
                errors.append("El tel\u00e9fono contiene caracteres inv\u00e1lidos. Use solo d\u00edgitos, espacios, guiones o par\u00e9ntesis.")
            if len(telefono) > 30:
                errors.append("El tel\u00e9fono no puede tener m\u00e1s de 30 caracteres.")
            if len(nombre) > 100:
                errors.append("El nombre no puede tener m\u00e1s de 100 caracteres.")
            if len(direccion) > 200:
                errors.append("La direcci\u00f3n no puede tener m\u00e1s de 200 caracteres.")
            if not fecha_str:
                errors.append("Debe indicar la fecha del abono.")
            else:
                try:
                    fecha_dt = datetime.strptime(fecha_str, "%Y-%m-%d")
                    if fecha_dt.date() > now_local().date():
                        errors.append("La fecha no puede ser posterior a hoy.")
                except ValueError:
                    errors.append("Formato de fecha inv\u00e1lido.")

            _cfg_cliente = get_config()
            _vb_cliente = int(_cfg_cliente.get("valor_boleta", 10000) or 10000)

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
                if not es_boleta_completa(raw):
                    errors.append(f"'{raw}' no es una boleta v\u00e1lida: escriba los 4 d\u00edgitos (0000-9999).")
                    continue
                m = parse_money(montos_raw[i]) if i < len(montos_raw) else 0
                if m <= 0:
                    errors.append(f"El monto para la boleta #{num:04d} debe ser mayor que cero.")
                    continue
                if m > _vb_cliente:
                    errors.append(f"El monto ${m:,} para la boleta #{num:04d} supera el valor de la boleta (${_vb_cliente:,}).")
                    continue
                meta = metodos[i].strip().lower() if i < len(metodos) else METODO_EFECTIVO
                ref = referencias[i].strip() if i < len(referencias) else ""
                banco_val = bancos[i].strip() if i < len(bancos) else ""
                rows.append(
                    {
                        "boleta": num,
                        "monto": m,
                        "metodo": meta,
                        "referencia": ref,
                        "banco": banco_val,
                    }
                )

            if not rows:
                errors.append("Ingrese al menos una boleta.")

            validar_filas_transferencia(rows, errors)

            if errors:
                for e in errors:
                    flash(e, "danger")
                return _render_cliente_form(form, form_rows, today)

            contador = Counter((r["boleta"], r["metodo"]) for r in rows)
            duplicadas = [b for (b, _), cnt in contador.items() if cnt > 1]
            if duplicadas:
                flash(f"Boletas duplicadas en la factura: {', '.join(f'{b:04d}' for b in sorted(duplicadas))}. Elimine las repeticiones.", "danger")
                return _render_cliente_form(form, form_rows, today)

            boleta_ids = [r["boleta"] for r in rows]
            docs_map, missing = verificar_boletas_existen(boleta_ids)
            if missing:
                flash(f"Boletas no encontradas: {', '.join(f'{b:04d}' for b in missing)}", "danger")
                return _render_cliente_form(form, form_rows, today)

            pagadas = [b for b in boleta_ids if docs_map[b].get("estado") == "pagada"]
            if pagadas and not confirmar_pagadas:
                detalle_pagadas = [{"boleta": b, "total": int(docs_map[b].get("total_abonado") or 0)} for b in sorted(pagadas)]
                flash(
                    "Hay boletas ya pagadas en la factura. Confirme para registrar los pagos como excedente (seguirán mostrando estado pagada).",
                    "warning",
                )
                return _render_cliente_form(form, form_rows, today, confirm_pagadas=detalle_pagadas)

            excedentes = []
            for r in rows:
                doc = docs_map.get(r["boleta"])
                if doc is None:
                    continue
                total_actual = int(doc.get("total_abonado") or 0)
                if total_actual + r["monto"] > _vb_cliente:
                    excedentes.append((r["boleta"], total_actual, r["monto"]))
            if excedentes:
                det = ", ".join(f"#{b:04d} (${t:,} + ${m:,} = ${t + m:,})" for b, t, m in excedentes[:8])
                flash(f"El acumulado de las siguientes boletas excedería el valor de la boleta (${_vb_cliente:,}): {det}.", "warning")

            factura_id = None
            valor_boleta_local = _vb_cliente
            try:
                config_local = get_config()
                valor_boleta_local = int(config_local["valor_boleta"])

                # Reservar id + crear factura "pendiente" antes de tocar las
                # boletas (fail-fast; reintenta si el contador está desfasado
                # y otro proceso tomó el mismo id).
                user = current_user() or {}
                vendedores_existentes = {
                    d["vendedor_id"]
                    for d in boletas.find(
                        {"_id": {"$in": boleta_ids}, "vendedor_id": {"$nin": ["", None, VENDEDOR_LOCAL]}},
                        {"vendedor_id": 1},
                    )
                }
                vendedor_factura = vendedores_existentes.pop() if len(vendedores_existentes) == 1 else VENDEDOR_LOCAL

                for _intento in range(5):
                    factura_id = next_factura_id()
                    try:
                        facturas.insert_one(
                            {
                                "_id": factura_id,
                                "tipo": "cliente",
                                "estado": "pendiente",
                                "creada_en": now_local(),
                                "fecha": now_local() if fecha_dt.date() == now_local().date() else fecha_dt,
                                "boletas": sorted(boleta_ids),
                                "detalle": [],
                                "valor_total": 0,
                                "cliente": {"nombre": nombre, "telefono": telefono, "direccion": direccion},
                                "vendedor_id": vendedor_factura,
                                "vendedor_nombre": vendedor_factura,
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
                    return _render_cliente_form(form, form_rows, today)

                groups = defaultdict(list)
                for r in rows:
                    if r["monto"] > 0:
                        key = (r["metodo"], r["referencia"], r.get("banco", ""), r["monto"])
                        groups[key].append(r["boleta"])

                for (metodo, referencia, banco, monto), boleta_ids_group in groups.items():
                    pago_form = {
                        "boletas": ",".join(f"{b:04d}" for b in boleta_ids_group),
                        "valor": str(monto),
                        "fecha": fecha_str,
                        "metodo": metodo,
                        "referencia": referencia,
                        "banco": banco,
                    }
                    _form_data, preview = build_abono_preview(pago_form, factura_id=factura_id, confirmar_pagadas=confirmar_pagadas)
                    if not preview.get("can_confirm"):
                        raise ValueError("; ".join(preview.get("errors", [])))
                    registrar_abono_lote(boleta_ids_group, _form_data, preview["valor_abono"], factura_id=factura_id, permitir_pagadas=confirmar_pagadas)

                detalle = build_factura_detalle(boleta_ids, factura_id)
                valor_total = sum(d["valor"] for d in detalle)

                cliente_data = {"nombre": nombre, "telefono": telefono, "direccion": direccion}
                # Solo se asocia el cliente a la primera boleta de la factura; las
                # demás pueden pertenecer a otros compradores y conservan sus datos.
                boletas.update_many(
                    {"_id": {"$in": boleta_ids[:1]}, "cliente.nombre": {"$in": ["", None]}},
                    {"$set": {"cliente": cliente_data}},
                )
                # Solo asignar LOCAL a boletas que NO tengan un vendedor real ya asignado.
                boletas.update_many(
                    {"_id": {"$in": boleta_ids}, "vendedor_id": {"$in": ["", None]}},
                    {"$set": {"vendedor_id": VENDEDOR_LOCAL}},
                )
                boletas.update_many(
                    {"_id": {"$in": boleta_ids}},
                    [{"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}}],
                )

                facturas.update_one(
                    {"_id": factura_id},
                    {
                        "$set": {
                            "tipo": "cliente",
                            "estado": "completa",
                            "fecha": now_local() if fecha_dt.date() == now_local().date() else fecha_dt,
                            "boletas": sorted(boleta_ids),
                            "detalle": detalle,
                            "valor_total": valor_total,
                            "cliente": cliente_data,
                            "vendedor_id": vendedor_factura,
                            "vendedor_nombre": vendedor_factura,
                            "usuario_id": user.get("usuario_id"),
                            "usuario_nombre": user.get("nombre") or user.get("username"),
                            "boletas_info": build_boletas_info_snapshot(boleta_ids, valor_boleta_local),
                        }
                    },
                )

                flash(f"Factura de cliente generada con {len(boleta_ids)} boleta(s).", "success")
                return redirect(url_for("ver_factura", factura_id=factura_id))

            except Exception as exc:
                if factura_id is not None:
                    try:
                        rollback_pagos_por_factura(factura_id, valor_boleta_local)
                    except Exception as rollback_exc:
                        current_app.logger.warning("Rollback de pagos falló para factura %s: %s", factura_id, rollback_exc)
                    try:
                        facturas.delete_one({"_id": factura_id, "estado": {"$ne": "completa"}})
                    except Exception as del_exc:
                        current_app.logger.warning("Eliminación de factura pendiente %s falló: %s", factura_id, del_exc)
                flash(safe_error_message(exc), "danger")
                return _render_cliente_form(form, form_rows, today)

        boleta_query = request.args.get("boletas", "").strip()
        form_rows = []
        if boleta_query:
            for raw_part in boleta_query.split(","):
                part = raw_part.strip()
                if part:
                    form_rows.append(
                        {
                            "boleta": part,
                            "monto": "",
                            "metodo": "efectivo",
                            "referencia": "",
                            "banco": "",
                        }
                    )
        return _render_cliente_form(form, form_rows, today)

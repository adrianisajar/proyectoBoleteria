from collections import defaultdict
from datetime import datetime

from flask import Flask, Response

from motores.constants import METODO_EFECTIVO, VENDEDOR_LOCAL
from motores.facturacion_common import deduplicar_filas_boleta, validar_filas_transferencia, verificar_boletas_existen
from motores.fechas import now_local
from motores.shared import (
    boletas,
    build_abono_preview,
    build_boletas_info_snapshot,
    build_factura_detalle,
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
from motores.validacion import es_boleta_completa, parse_money


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


def _render_cliente_form(form: dict, form_rows: list[dict], today: str) -> str:
    """Render the customer invoice form with the given values for a re-render."""
    form_data = {
        "fecha": form.get("fecha", ""),
        "form_rows": form_rows,
    }
    _cfg_cliente = get_config()
    _vb_cliente = int(_cfg_cliente.get("valor_boleta", 10000) or 10000)
    return render_template("nueva_factura_cliente.html", form=form, today=today, form_data=form_data, valor_boleta=_vb_cliente)


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

            rows, dup_count = deduplicar_filas_boleta(rows)
            if dup_count:
                flash(f"{dup_count} boleta(s) duplicada(s) ignorada(s).", "warning")

            boleta_ids = [r["boleta"] for r in rows]
            docs_map, missing = verificar_boletas_existen(boleta_ids)
            if missing:
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
                    _form_data, preview = build_abono_preview(pago_form, factura_id=factura_id)
                    if not preview.get("can_confirm"):
                        raise ValueError("; ".join(preview.get("errors", [])))
                    registrar_abono_lote(boleta_ids_group, _form_data, preview["valor_abono"], factura_id=factura_id)

                detalle = build_factura_detalle(boleta_ids, factura_id)
                valor_total = sum(d["valor"] for d in detalle)

                factura = {
                    "_id": factura_id,
                    "tipo": "cliente",
                    "fecha": now_local() if fecha_dt.date() == now_local().date() else fecha_dt,
                    "boletas": sorted(boleta_ids),
                    "detalle": detalle,
                    "valor_total": valor_total,
                    "cliente": {"nombre": nombre, "telefono": telefono, "direccion": direccion},
                    "vendedor_id": VENDEDOR_LOCAL,
                    "vendedor_nombre": VENDEDOR_LOCAL,
                }

                cliente_data = {"nombre": nombre, "telefono": telefono, "direccion": direccion}
                boletas.update_many(
                    {"_id": {"$in": boleta_ids}, "cliente.nombre": {"$in": ["", None]}},
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

                factura["boletas_info"] = build_boletas_info_snapshot(boleta_ids, valor_boleta_local)
                facturas.insert_one(factura)

                flash(f"Factura de cliente generada con {len(boleta_ids)} boleta(s).", "success")
                return redirect(url_for("ver_factura", factura_id=factura["_id"], imprimir=1))

            except Exception as exc:
                if factura_id is not None:
                    rollback_pagos_por_factura(factura_id, valor_boleta_local)
                    try:
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

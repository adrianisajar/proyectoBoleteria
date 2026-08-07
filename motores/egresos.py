"""Egreso invoice routes (comprobante interno, admin only)."""

import re
from datetime import datetime

from flask import Flask, Response

from motores.constants import (
    METODO_EFECTIVO,
    METODO_TRANSFERENCIA,
    METODOS_PAGO,
    MOV_EGRESO,
    MOVIMIENTOS_FIELD,
    TIPOS_EGRESO,
    USUARIO_SISTEMA,
    VENDEDOR_LOCAL,
    VENDEDOR_LOCAL_LABEL,
)
from motores.egreso_service import build_egreso_detalle, registrar_egresos, rollback_egresos_por_factura
from motores.facturacion_common import deduplicar_filas_boleta, verificar_boletas_existen
from motores.fechas import now_local
from motores.shared import (
    boletas,
    current_user,
    facturas,
    flash,
    get_config,
    jsonify,
    next_factura_id,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    url_for,
    vendedores,
)
from motores.validacion import es_boleta_completa, parse_money


def _vendedores_con_local() -> list[dict]:
    """Return the vendor list including the LOCAL system vendor."""
    lista = list(vendedores.find().sort("_id", 1))
    return [*[{"_id": VENDEDOR_LOCAL, "nombre": VENDEDOR_LOCAL_LABEL}], *lista]


def _build_form_data(vendedor_id: str, fecha: str, egreso_tipo: str, observaciones: str, rows_raw: list[dict]) -> dict:
    """Build the re-render context for the egreso form."""
    v_nombre = ""
    if vendedor_id == VENDEDOR_LOCAL:
        v_nombre = VENDEDOR_LOCAL_LABEL
    elif vendedor_id:
        v = vendedores.find_one({"_id": vendedor_id}, {"nombre": 1})
        if v:
            v_nombre = v.get("nombre", vendedor_id)
    return {
        "vendedor_id": vendedor_id,
        "vendedor_nombre": v_nombre,
        "fecha": fecha,
        "egreso_tipo": egreso_tipo,
        "observaciones": observaciones,
        "rows": rows_raw,
    }


def _render_form(form_data: dict, vendedores_list: list | None = None) -> str:
    if vendedores_list is None:
        vendedores_list = _vendedores_con_local()
    today = now_local().strftime("%Y-%m-%d")
    return render_template(
        "nueva_factura_egreso.html",
        vendedores=vendedores_list,
        today=today,
        form=form_data,
        tipos_egreso=TIPOS_EGRESO,
    )


def validar_egresos_transferencia(form_rows: list[dict], errors: list[str]) -> None:
    """Validate transfer egresos: require reference+banco on each transfer row."""
    for r in form_rows:
        if r["metodo"] != METODO_TRANSFERENCIA:
            continue
        if not r.get("referencia", "").strip():
            errors.append(f"Referencia obligatoria para transferencia en boleta #{r['boleta']:04d}.")
        if not r.get("banco", "").strip():
            errors.append(f"Banco obligatorio para transferencia en boleta #{r['boleta']:04d}.")


def register_routes(app: Flask) -> None:
    """Register the egreso invoice creation and listing routes."""

    @app.route("/facturas/egreso")
    @role_required("admin", "cajero")
    def egresos_list() -> str:
        """List egreso invoices (comprobantes internos)."""
        require_collections()
        lista = list(facturas.find({"tipo": "egreso"}).sort([("fecha", -1), ("_id", -1)]).limit(100))
        return render_template("egresos_list.html", facturas=lista, tipos_egreso=TIPOS_EGRESO)

    @app.route("/api/egresos/boletas/<vendedor_id>")
    @role_required("admin", "cajero")
    def api_boletas_vendedor_egreso(vendedor_id: str) -> Response:
        """Return the vendor's tickets with per-ticket income/egreso info for the egreso form."""
        require_collections()
        if vendedor_id != VENDEDOR_LOCAL and not vendedores.find_one({"_id": vendedor_id}, {"_id": 1}):
            return jsonify({"ok": False, "error": "Vendedor no encontrado."}), 404
        items = []
        for d in boletas.find(
            {"vendedor_id": vendedor_id},
            {"estado": 1, "total_abonado": 1, "cliente": 1, MOVIMIENTOS_FIELD: 1},
        ).sort("_id", 1):
            total_egresado = sum(int((m or {}).get("valor") or 0) for m in (d.get(MOVIMIENTOS_FIELD) or []) if (m or {}).get("tipo") == MOV_EGRESO)
            items.append(
                {
                    "numero": d["_id"],
                    "estado": d.get("estado", ""),
                    "total_ingresado": int(d.get("total_abonado") or 0),
                    "total_egresado": total_egresado,
                    "tiene_egresos_previos": total_egresado > 0,
                    "cliente": d.get("cliente", "") or "",
                }
            )
        return jsonify({"ok": True, "boletas": items})

    @app.route("/facturas/egreso/nueva", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def nueva_factura_egreso() -> str | Response:
        """Create an egreso invoice from dynamic rows (boleta + valor manual)."""
        require_collections()
        empty_form = {
            "vendedor_id": "",
            "vendedor_nombre": "",
            "fecha": "",
            "egreso_tipo": "",
            "observaciones": "",
            "rows": [],
        }

        if request.method == "POST":
            vendedor_id = request.form.get("vendedor_id", "").strip()
            fecha = request.form.get("fecha", "").strip()
            egreso_tipo = request.form.get("egreso_tipo", "").strip()
            observaciones = request.form.get("observaciones", "").strip()

            boletas_raw = request.form.getlist("boleta[]")
            valores_raw = request.form.getlist("valor[]")
            metodos = request.form.getlist("metodo[]")
            referencias = request.form.getlist("referencia[]")
            bancos = request.form.getlist("banco[]")

            rows_raw = []
            for i in range(len(boletas_raw)):
                raw = boletas_raw[i].strip()
                if not raw:
                    continue
                rows_raw.append(
                    {
                        "boleta": raw,
                        "valor": valores_raw[i] if i < len(valores_raw) else "",
                        "metodo": metodos[i] if i < len(metodos) else METODO_EFECTIVO,
                        "referencia": referencias[i] if i < len(referencias) else "",
                        "banco": bancos[i] if i < len(bancos) else "",
                    }
                )
            form_data = _build_form_data(vendedor_id, fecha, egreso_tipo, observaciones, rows_raw)
            vendedores_list = _vendedores_con_local()

            errors = []
            if not vendedor_id:
                errors.append("Debe seleccionar un vendedor.")
            if egreso_tipo not in TIPOS_EGRESO:
                errors.append("Debe indicar el tipo de egreso.")
            if not fecha:
                errors.append("Debe indicar la fecha del egreso.")
            else:
                try:
                    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
                    if fecha_dt.date() > now_local().date():
                        errors.append("La fecha no puede ser posterior a hoy.")
                except ValueError:
                    errors.append("Formato de fecha inv\u00e1lido.")

            _cfg = get_config()
            _vb = int(_cfg.get("valor_boleta", 10000) or 10000)

            rows = []
            for i in range(len(boletas_raw)):
                raw = boletas_raw[i].strip()
                if not raw:
                    continue
                tokens = [p.strip() for p in re.split(r"[\s,;]+", raw) if p.strip()]
                incompletas = [t for t in tokens if not es_boleta_completa(t)]
                parts = [t for t in tokens if es_boleta_completa(t)]
                if incompletas:
                    errors.append(f"Boleta(s) con formato inv\u00e1lido (escriba los 4 d\u00edgitos): {', '.join(incompletas[:8])}.")
                    continue
                if not parts:
                    continue
                valor = parse_money(valores_raw[i]) if i < len(valores_raw) else 0
                if valor <= 0:
                    errors.append("El valor del egreso debe ser mayor que cero.")
                    continue
                if valor > _vb * len(parts):
                    errors.append(f"El valor ${valor:,} para {len(parts)} boleta(s) supera el m\u00e1ximo de ${_vb * len(parts):,}.")
                    continue
                metodo = metodos[i].strip().lower() if i < len(metodos) else METODO_EFECTIVO
                if metodo not in METODOS_PAGO:
                    errors.append("M\u00e9todo de egreso inv\u00e1lido.")
                    continue
                ref = referencias[i].strip() if i < len(referencias) else ""
                banco_val = bancos[i].strip() if i < len(bancos) else ""
                if metodo == METODO_TRANSFERENCIA and not ref:
                    errors.append("La referencia bancaria es obligatoria para transferencias.")
                    continue
                for p in parts:
                    rows.append(
                        {
                            "boleta": int(p),
                            "valor": valor,
                            "metodo": metodo,
                            "referencia": ref,
                            "banco": banco_val,
                        }
                    )

            validar_egresos_transferencia(rows, errors)

            if errors:
                for e in errors:
                    flash(e, "danger")
                return _render_form(form_data, vendedores_list=vendedores_list)

            if not rows:
                flash("Debe incluir al menos una boleta con un egreso v\u00e1lido.", "danger")
                return _render_form(form_data, vendedores_list=vendedores_list)

            rows, dup_count = deduplicar_filas_boleta(rows)
            if dup_count:
                flash(f"{dup_count} boleta(s) duplicada(s) ignorada(s).", "warning")

            boleta_ids = [r["boleta"] for r in rows]
            docs_map, missing = verificar_boletas_existen(boleta_ids)
            if missing:
                flash(f"Boletas no encontradas: {', '.join(f'{b:04d}' for b in missing)}", "danger")
                return _render_form(form_data, vendedores_list=vendedores_list)

            # No se bloquean boletas con egresos previos: solo se informa.
            con_previos = [b for b in boleta_ids if any((m or {}).get("tipo") == MOV_EGRESO for m in (docs_map[b].get(MOVIMIENTOS_FIELD) or []))]
            if con_previos:
                flash(
                    f"Atenci\u00f3n: {len(con_previos)} boleta(s) ya registran egresos anteriores: {', '.join(f'#{b:04d}' for b in sorted(con_previos))}.",
                    "warning",
                )

            factura_id = None
            try:
                factura_id = next_factura_id()
                v = vendedores.find_one({"_id": vendedor_id})
                if vendedor_id == VENDEDOR_LOCAL:
                    v_nombre = VENDEDOR_LOCAL_LABEL
                    v_telefono = ""
                else:
                    v_nombre = v.get("nombre", vendedor_id) if v else vendedor_id
                    v_telefono = v.get("telefono", "") if v else ""

                user = current_user() or {}
                usuario = user.get("username") or USUARIO_SISTEMA

                registrar_egresos(factura_id, rows, fecha, usuario, egreso_tipo)

                detalle = build_egreso_detalle(boleta_ids, factura_id)
                valor_total = sum(d["valor"] for d in detalle)

                facturas.insert_one(
                    {
                        "_id": factura_id,
                        "tipo": "egreso",
                        "egreso_tipo": egreso_tipo,
                        "estado": "completa",
                        "fecha": now_local() if fecha_dt.date() == now_local().date() else fecha_dt,
                        "boletas": sorted(boleta_ids),
                        "detalle": detalle,
                        "valor_total": valor_total,
                        "vendedor_id": vendedor_id,
                        "vendedor_nombre": v_nombre,
                        "vendedor_telefono": v_telefono,
                        "usuario_id": user.get("usuario_id"),
                        "usuario_nombre": user.get("nombre") or user.get("username"),
                        "observaciones": observaciones,
                    }
                )

                flash(f"Factura de egreso N\u00b0 {factura_id:05d} generada.", "success")
                return redirect(url_for("ver_factura", factura_id=factura_id, imprimir=1))

            except Exception as exc:
                if factura_id is not None:
                    rollback_egresos_por_factura(factura_id)
                flash(f"Error al generar la factura de egreso: {exc}", "danger")
                return _render_form(form_data, vendedores_list=vendedores_list)

        return _render_form(empty_form)

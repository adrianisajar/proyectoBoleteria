import hashlib
import hmac
import re
import time as _time
from datetime import datetime, timedelta

from flask import Flask, Response, current_app

from motores.constants import METODO_PAGO_DELIO, METODO_TRANSFERENCIA, USUARIO_SISTEMA
from motores.egreso_service import rollback_egresos_por_factura
from motores.fechas import now_local
from motores.impresora import (
    build_cliente_receipt,
    build_egreso_receipt,
    build_traslado_receipt,
    build_vendedor_receipt,
    imprimir,
)
from motores.impresora import (
    is_configured as printer_configured,
)
from motores.shared import (
    abort,
    boletas,
    build_boletas_info_snapshot,
    current_user,
    facturas,
    flash,
    get_config,
    invalidate_dashboard_cache,
    jsonify,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    rollback_pagos_por_factura,
    traslados,
    url_for,
)
from motores.validacion import safe_error_message
from motores.validacion_factura import validar_factura


def _anulacion_hash(factura_id: int, anulada: bool, secret: str) -> str:
    """Build the HMAC-SHA256 hash that guards an invoice annulment against tampering.

    Uses the Flask secret_key as the HMAC key (not as message content) to
    prevent length-extension attacks.  The truncated 16-char hex output is
    sufficient for CSRF-level protection (a single session generates one
    hash at a time and the hash changes on state change).
    """
    msg = f"{factura_id}:{anulada}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:16]


_ULTIMA_LIMPIEZA_PENDIENTES: list[float] = [0.0]


def _limpiar_facturas_pendientes() -> None:
    """Remove stale pending invoices only when they have no ledger movements.

    A pending invoice can already have payments while its creation flow is
    finishing.  Those documents are evidence needed for recovery, so they are
    deliberately retained instead of deleting their associated payments.
    Runs at most once per 60 seconds.
    """
    now = _time.monotonic()
    if now - _ULTIMA_LIMPIEZA_PENDIENTES[0] < 60:
        return
    _ULTIMA_LIMPIEZA_PENDIENTES[0] = now
    if facturas is None:
        return
    try:
        cutoff = now_local() - timedelta(minutes=5)
        pendientes = facturas.find(
            {"estado": "pendiente", "creada_en": {"$lt": cutoff}},
            {"_id": 1},
        ).limit(50)
        for factura in pendientes:
            factura_id = factura["_id"]
            tiene_movimientos = boletas.count_documents({"historial_movimientos.factura_id": factura_id}, limit=1)
            if not tiene_movimientos:
                facturas.delete_one({"_id": factura_id, "estado": "pendiente", "creada_en": {"$lt": cutoff}})
    except Exception:
        current_app.logger.exception("No se pudieron limpiar facturas pendientes")


def register_routes(app: Flask) -> None:
    """Register the invoice list, detail and annulment routes."""

    @app.before_request
    def _cleanup_stale_facturas() -> None:
        _limpiar_facturas_pendientes()

    @app.route("/facturas")
    @role_required("admin", "cajero")
    def facturas_list() -> str:
        """List all invoices, searchable by id or client/vendor name."""
        require_collections()
        q = request.args.get("q", "").strip()
        query = {}
        if q:
            try:
                query["_id"] = int(q)
            except ValueError:
                query["$or"] = [
                    {"cliente.nombre": {"$regex": f"^{re.escape(q)}", "$options": "i"}},
                    {"vendedor_nombre": {"$regex": f"^{re.escape(q)}", "$options": "i"}},
                    {"vendedor_id": q},
                ]
        query["estado"] = {"$in": ["completa", None]}
        projection = {"detalle": 0}
        sort_by = request.args.get("sort_by", "_id").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        if sort_by not in {"_id", "fecha", "tipo", "vendedor_id", "valor_total"}:
            sort_by = "_id"
        sort_direction = 1 if sort_dir == "asc" else -1
        lista = list(facturas.find(query, projection).sort(sort_by, sort_direction).limit(100))
        return render_template("facturas_list.html", facturas=lista, q=q, sort_by=sort_by, sort_dir=sort_dir)

    @app.route("/facturas/cliente")
    @role_required("admin", "cajero")
    def facturas_cliente() -> str:
        """List customer (cliente) invoices."""
        require_collections()
        sort_by = request.args.get("sort_by", "_id").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        if sort_by not in {"_id", "fecha", "tipo", "cliente.nombre", "valor_total"}:
            sort_by = "_id"
        sort_direction = 1 if sort_dir == "asc" else -1
        lista = list(facturas.find({"tipo": "cliente", "estado": {"$in": ["completa", None]}}, {"detalle": 0}).sort(sort_by, sort_direction).limit(100))
        return render_template("facturas_cliente.html", facturas=lista, sort_by=sort_by, sort_dir=sort_dir)

    @app.route("/facturas/vendedor")
    @role_required("admin", "cajero")
    def facturas_vendedor() -> str:
        """List vendor (vendedor) invoices."""
        require_collections()
        sort_by = request.args.get("sort_by", "_id").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        if sort_by not in {"_id", "fecha", "tipo", "vendedor_id", "valor_total"}:
            sort_by = "_id"
        sort_direction = 1 if sort_dir == "asc" else -1
        lista = list(facturas.find({"tipo": "vendedor", "estado": {"$in": ["completa", None]}}, {"detalle": 0}).sort(sort_by, sort_direction).limit(100))
        return render_template("facturas_vendedor.html", facturas=lista, sort_by=sort_by, sort_dir=sort_dir)

    @app.route("/facturas/<int:factura_id>")
    @role_required("admin", "cajero")
    def ver_factura(factura_id: int) -> str:
        """Render the printable invoice detail (cliente or vendedor layout)."""
        require_collections()
        factura = facturas.find_one({"_id": factura_id})
        if not factura:
            abort(404)

        ctx = {"factura": factura, "config": get_config()}

        fecha_f = factura.get("fecha")
        if fecha_f is not None and isinstance(fecha_f, datetime):
            if fecha_f.hour == 0 and fecha_f.minute == 0 and fecha_f.second == 0:
                factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y")
            else:
                factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y %I:%M %p")
        else:
            factura["fecha_display"] = "—"

        if factura.get("tipo") == "cliente":
            boletas_info = factura.get("boletas_info")
            if not boletas_info:
                try:
                    config_local = ctx["config"]
                    valor_boleta = int(config_local.get("valor_boleta", 10000) or 10000)
                    boletas_info = build_boletas_info_snapshot(factura.get("boletas", []), valor_boleta)
                except Exception:
                    current_app.logger.warning("No se pudo construir boletas_info para la factura %s", factura_id)
                    boletas_info = {}
            ctx["boletas_info"] = {int(k): v for k, v in boletas_info.items()}

        if factura.get("tipo") == "cliente":
            for d in factura.get("detalle") or []:
                d["grupo_pago"] = str(d.get("valor", 0))
                if d.get("metodo") == "transferencia":
                    d["grupo_transferencia"] = f"{d.get('banco', '')}|{d.get('referencia', '')}"

        if factura.get("tipo") == "vendedor":
            total_efectivo = 0
            total_transferencia = 0
            total_delio = 0
            for d in factura.get("detalle") or []:
                valor = int(d.get("valor", 0) or 0)
                d["grupo_pago"] = str(valor)
                if d.get("metodo") == METODO_TRANSFERENCIA:
                    total_transferencia += valor
                    d["grupo_transferencia"] = f"{d.get('banco', '')}|{d.get('referencia', '')}"
                elif d.get("metodo") == METODO_PAGO_DELIO:
                    total_delio += valor
                else:
                    total_efectivo += valor
            ctx["total_efectivo"] = total_efectivo
            ctx["total_transferencia"] = total_transferencia
            ctx["total_delio"] = total_delio

        ctx["anulacion_hash"] = _anulacion_hash(factura_id, bool(factura.get("anulada")), current_app.secret_key)
        template_map = {
            "cliente": "factura_cliente.html",
            "vendedor": "factura_vendedor.html",
            "egreso": "factura_egreso.html",
        }
        template = template_map.get(factura.get("tipo", ""), "factura_cliente.html")
        return render_template(template, **ctx)

    @app.route("/api/validar-factura", methods=["POST"])
    @role_required("admin", "cajero")
    def api_validar_factura() -> Response:
        """Real-time validation for invoice forms (no writes)."""
        payload = request.get_json(silent=True) or {}
        try:
            resultado = validar_factura(payload)
        except Exception as exc:
            return jsonify({"ok": False, "total_errores": 1, "campo_errores": {"form": [safe_error_message(exc)]}, "filas": []}), 500
        return jsonify(resultado)

    @app.route("/facturas/<int:factura_id>/anular", methods=["POST"])
    @role_required("admin")
    def anular_factura(factura_id: int) -> Response:
        """Annul an invoice (hash-guarded): roll back payments and recalc ticket states."""
        require_collections()
        factura = facturas.find_one({"_id": factura_id})
        if not factura:
            abort(404)
        if factura.get("anulada"):
            flash("La factura ya fue anulada.", "warning")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        submitted_hash = request.form.get("anulacion_hash", "")
        expected_hash = _anulacion_hash(factura_id, False, current_app.secret_key)
        if not submitted_hash or not hmac.compare_digest(submitted_hash, expected_hash):
            flash("La factura fue modificada por otro usuario. Recargue la p\u00e1gina.", "danger")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        motivo = request.form.get("motivo", "").strip()
        if not motivo:
            flash("Debe indicar el motivo de la anulaci\u00f3n.", "danger")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        user = (current_user() or {}).get("username", USUARIO_SISTEMA)

        config_local = get_config()
        valor_boleta_local = int(config_local["valor_boleta"])

        try:
            # Marcar como anulada ANTES del rollback para que si algo falla,
            # la factura quede consistente (anulada sin pagos) en vez de
            # inconsistente (pagos revertidos pero sin flag).
            facturas.update_one(
                {"_id": factura_id},
                {
                    "$set": {
                        "anulada": True,
                        "anulada_en": now_local(),
                        "anulada_por": user,
                        "motivo_anulacion": motivo,
                    }
                },
            )
            if factura.get("tipo") == "egreso":
                rollback_egresos_por_factura(factura_id)
            else:
                rollback_pagos_por_factura(factura_id, valor_boleta_local)
        except Exception as exc:
            # Revertir el flag de anulación si el rollback falló.
            try:
                facturas.update_one(
                    {"_id": factura_id},
                    {"$set": {"anulada": False}, "$unset": {"anulada_en": "", "anulada_por": "", "motivo_anulacion": ""}},
                )
            except Exception as exc2:
                current_app.logger.warning("No se pudo revertir el flag anulada de la factura %s: %s", factura_id, exc2)
            flash(safe_error_message(exc), "danger")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        invalidate_dashboard_cache()
        flash(f"Factura N\u00b0 {factura_id:05d} anulada.", "success")
        return redirect(url_for("ver_factura", factura_id=factura_id))

    @app.route("/facturas/<int:factura_id>/imprimir", methods=["POST"])
    @role_required("admin", "cajero")
    def imprimir_factura(factura_id: int) -> Response:
        """Send an invoice directly to the thermal printer (ESC/POS via TCP)."""
        require_collections()
        if not printer_configured():
            flash("Impresora no configurada. Define PRINTER_HOST en .env.", "warning")
            return redirect(url_for("ver_factura", factura_id=factura_id))
        factura = facturas.find_one({"_id": factura_id})
        if not factura:
            abort(404)
        config_local = get_config()
        fecha_f = factura.get("fecha")
        if fecha_f is not None and isinstance(fecha_f, datetime):
            factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y %I:%M %p")
        else:
            factura["fecha_display"] = "—"
        tipo = factura.get("tipo", "cliente")
        if tipo == "cliente":
            boletas_info = factura.get("boletas_info") or {}
            boletas_info = {int(k): v for k, v in boletas_info.items()}
            data = build_cliente_receipt(factura, config_local, boletas_info)
        elif tipo == "vendedor":
            data = build_vendedor_receipt(factura, config_local)
        elif tipo == "egreso":
            data = build_egreso_receipt(factura, config_local)
        else:
            flash(f"Tipo de factura '{tipo}' no soportado para impresion directa.", "warning")
            return redirect(url_for("ver_factura", factura_id=factura_id))
        ok, msg = imprimir(data)
        if ok:
            flash(f"Factura enviada a la impresora. {msg}", "success")
        else:
            flash(f"Error al imprimir: {msg}", "danger")
        return redirect(url_for("ver_factura", factura_id=factura_id))

    @app.route("/traslados/<int:traslado_id>/imprimir", methods=["POST"])
    @role_required("admin", "cajero")
    def imprimir_traslado(traslado_id: int) -> Response:
        """Send a traslado comprobante directly to the thermal printer."""
        require_collections()
        if not printer_configured():
            flash("Impresora no configurada. Define PRINTER_HOST en .env.", "warning")
            return redirect(url_for("ver_traslado", traslado_id=traslado_id))
        traslado_doc = traslados.find_one({"_id": traslado_id})
        if not traslado_doc:
            flash(f"Traslado N\u00b0 {traslado_id:05d} no encontrado.", "danger")
            return redirect(url_for("traslados_list"))
        config_local = get_config()
        origen_doc = boletas.find_one({"_id": traslado_doc["boleta_origen"]}, {"_id": 1, "cliente": 1})
        destino_doc = boletas.find_one({"_id": traslado_doc["boleta_destino"]}, {"_id": 1, "cliente": 1})
        data = build_traslado_receipt(traslado_doc, config_local, origen_doc or {}, destino_doc or {})
        ok, msg = imprimir(data)
        if ok:
            flash(f"Traslado enviado a la impresora. {msg}", "success")
        else:
            flash(f"Error al imprimir: {msg}", "danger")
        return redirect(url_for("ver_traslado", traslado_id=traslado_id))

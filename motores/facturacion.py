import contextlib
import hashlib
import re
from datetime import datetime

from flask import Flask, Response, current_app

from motores.constants import METODO_TRANSFERENCIA, USUARIO_SISTEMA, VENDEDOR_LOCAL
from motores.fechas import now_local
from motores.shared import (
    abort,
    boletas,
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
    url_for,
    vendedores,
)
from motores.validacion_factura import validar_factura


def _anulacion_hash(factura_id: int, anulada: bool, secret: str) -> str:
    """Build the short hash that guards an invoice annulment against tampering."""
    raw = f"{factura_id}:{anulada}:{secret}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def register_routes(app: Flask) -> None:
    """Register the invoice list, detail and annulment routes."""

    @app.route("/facturas")
    @role_required("admin", "cajero", "consulta")
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
                    {"cliente.nombre": {"$regex": re.escape(q), "$options": "i"}},
                    {"vendedor_nombre": {"$regex": re.escape(q), "$options": "i"}},
                    {"vendedor_id": {"$regex": re.escape(q), "$options": "i"}},
                ]
        lista = list(facturas.find(query).sort([("fecha", -1), ("_id", -1)]).limit(100))
        return render_template("facturas_list.html", facturas=lista, q=q)

    @app.route("/facturas/cliente")
    @role_required("admin", "cajero", "consulta")
    def facturas_cliente() -> str:
        """List customer (cliente) invoices."""
        require_collections()
        lista = list(facturas.find({"tipo": "cliente"}).sort([("fecha", -1), ("_id", -1)]).limit(100))
        return render_template("facturas_cliente.html", facturas=lista)

    @app.route("/facturas/vendedor")
    @role_required("admin", "cajero", "consulta")
    def facturas_vendedor() -> str:
        """List vendor (vendedor) invoices."""
        require_collections()
        lista = list(facturas.find({"tipo": "vendedor"}).sort([("fecha", -1), ("_id", -1)]).limit(100))
        return render_template("facturas_vendedor.html", facturas=lista)

    @app.route("/facturas/<int:factura_id>")
    @role_required("admin", "cajero", "consulta")
    def ver_factura(factura_id: int) -> str:
        """Render the printable invoice detail (cliente or vendedor layout)."""
        require_collections()
        factura = facturas.find_one({"_id": factura_id})
        if not factura:
            abort(404)

        ctx = {"factura": factura, "config": get_config(), "imprimir": request.args.get("imprimir")}

        fecha_f = factura["fecha"]
        if isinstance(fecha_f, datetime):
            if fecha_f.hour == 0 and fecha_f.minute == 0 and fecha_f.second == 0:
                factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y")
            else:
                factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y %I:%M %p")

        if factura.get("tipo") == "cliente":
            boletas_ids = factura.get("boletas", [])
            docs = list(boletas.find({"_id": {"$in": boletas_ids}}))
            config = ctx["config"]
            valor_boleta = int(config.get("valor_boleta", 10000))
            boletas_info = {}
            vendedores_vistos = {doc.get("vendedor_id") for doc in docs if doc.get("vendedor_id") and doc.get("vendedor_id") != VENDEDOR_LOCAL}
            vid_cache = {}
            if vendedores_vistos:
                for v in vendedores.find({"_id": {"$in": list(vendedores_vistos)}}, {"nombre": 1}):
                    vid_cache[v["_id"]] = v.get("nombre", v["_id"])
            for doc in docs:
                bid = doc["_id"]
                historial_completo = doc.get("historial_pagos") or []
                # Pagos hasta la fecha de la factura: filtramos por fecha del pago <= fecha de la factura
                # Y adem�s por factura_id para resolver el orden dentro del mismo d�a
                fecha_factura_str = factura["fecha"].strftime("%Y-%m-%d") if isinstance(factura["fecha"], datetime) else str(factura["fecha"])[:10]
                historial_hasta_factura = [
                    p
                    for p in historial_completo
                    if (p.get("factura_id") is None or p.get("factura_id", 0) <= factura_id) and str(p.get("fecha", ""))[:10] <= fecha_factura_str
                ]
                # Pagos de esta factura (para tabla "PAGOS DE ESTA FACTURA")
                historial_esta_factura = [p for p in historial_hasta_factura if p.get("factura_id") == factura_id]
                total_hasta_factura = sum(int(p.get("valor", 0) or 0) for p in historial_hasta_factura)
                saldo_hasta_factura = max(valor_boleta - total_hasta_factura, 0)
                if total_hasta_factura >= valor_boleta:
                    estado_historico = "pagada"
                elif total_hasta_factura > 0:
                    estado_historico = "abonando"
                elif doc.get("vendedor_id") == VENDEDOR_LOCAL:
                    estado_historico = "separada"
                elif doc.get("vendedor_id"):
                    estado_historico = "asignada"
                else:
                    estado_historico = "disponible"

                boletas_info[bid] = {
                    "total_abonado": total_hasta_factura,
                    "saldo_pendiente": saldo_hasta_factura,
                    "estado": estado_historico,
                    "valor_boleta": valor_boleta,
                    "vendedor_id": doc.get("vendedor_id", "LOCAL"),
                    "vendedor_nombre": vid_cache.get(doc.get("vendedor_id", "LOCAL"), "LOCAL"),
                    "historial_pagos": historial_hasta_factura,
                    "pagos_factura": historial_esta_factura,
                }
            ctx["boletas_info"] = boletas_info

        if factura.get("tipo") == "cliente":
            for d in factura.get("detalle") or []:
                d["grupo_pago"] = str(d.get("valor", 0))
                if d.get("metodo") == "transferencia":
                    d["grupo_transferencia"] = f"{d.get('banco', '')}|{d.get('referencia', '')}"

        if factura.get("tipo") == "vendedor":
            total_efectivo = 0
            total_transferencia = 0
            for d in factura.get("detalle") or []:
                valor = int(d.get("valor", 0) or 0)
                d["grupo_pago"] = str(valor)
                if d.get("metodo") == METODO_TRANSFERENCIA:
                    total_transferencia += valor
                    d["grupo_transferencia"] = f"{d.get('banco', '')}|{d.get('referencia', '')}"
                else:
                    total_efectivo += valor
            ctx["total_efectivo"] = total_efectivo
            ctx["total_transferencia"] = total_transferencia

        ctx["anulacion_hash"] = _anulacion_hash(factura_id, bool(factura.get("anulada")), current_app.secret_key)
        template_map = {"cliente": "factura_cliente.html", "vendedor": "factura_vendedor.html"}
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
            return jsonify({"ok": False, "total_errores": 1, "campo_errores": {"form": [f"Error al validar: {exc}"]}, "filas": []}), 500
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
        if not submitted_hash or submitted_hash != expected_hash:
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
            rollback_pagos_por_factura(factura_id, valor_boleta_local)

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
        except Exception as exc:
            with contextlib.suppress(Exception):
                facturas.update_one(
                    {"_id": factura_id},
                    {"$set": {"anulada": False}},
                )
            flash(f"Error al anular la factura N\u00b0 {factura_id:05d}: {exc}", "danger")
            return redirect(url_for("ver_factura", factura_id=factura_id))

        invalidate_dashboard_cache()
        flash(f"Factura N\u00b0 {factura_id:05d} anulada.", "success")
        return redirect(url_for("ver_factura", factura_id=factura_id))

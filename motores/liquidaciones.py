from flask import Flask, Response

from motores.constants import METODO_EFECTIVO, METODOS_PAGO
from motores.errores import safe_error_message
from motores.liquidacion_service import (
    generar_liquidacion,
    get_liquidacion,
    get_liquidacion_detalle,
    get_liquidaciones_resumen,
    registrar_abono_liquidacion,
)
from motores.shared import (
    abort,
    flash,
    get_config,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    url_for,
)
from motores.validacion import parse_money


def register_routes(app: Flask) -> None:
    """Register the liquidaciones module routes (resumen, detalle, comprobante, abonos)."""

    @app.route("/liquidaciones")
    @role_required("admin", "cajero", "consulta")
    def liquidaciones_panel() -> str:
        """Resumen de liquidaciones de todos los vendedores de la rifa activa."""
        require_collections()
        rows, resumen = get_liquidaciones_resumen()
        return render_template("liquidaciones.html", filas=rows, resumen=resumen)

    @app.route("/liquidaciones/vendedor/<vendedor_id>")
    @role_required("admin", "cajero", "consulta")
    def liquidacion_vendedor(vendedor_id: str) -> str | Response:
        """Detalle de la liquidación de un vendedor (cómo se calculó la comisión)."""
        require_collections()
        try:
            detalle = get_liquidacion_detalle(vendedor_id)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("liquidaciones_panel"))
        return render_template("liquidacion_vendedor.html", d=detalle)

    @app.route("/liquidaciones/vendedor/<vendedor_id>/generar", methods=["POST"])
    @role_required("admin")
    def generar_liquidacion_vendedor(vendedor_id: str) -> Response:
        """Persistir la liquidación del vendedor y redirigir al comprobante."""
        require_collections()
        observaciones = request.form.get("observaciones", "").strip()
        try:
            doc = generar_liquidacion(vendedor_id, observaciones)
        except Exception as exc:
            flash(f"No se pudo generar la liquidación: {safe_error_message(exc)}", "danger")
            return redirect(url_for("liquidacion_vendedor", vendedor_id=vendedor_id))
        flash(f"Liquidación N° {doc['_id']:05d} generada.", "success")
        return redirect(url_for("comprobante_liquidacion", liquidacion_id=doc["_id"]))

    @app.route("/liquidaciones/<int:liquidacion_id>")
    @role_required("admin", "cajero", "consulta")
    def comprobante_liquidacion(liquidacion_id: int) -> str | Response:
        """Comprobante de liquidación reimprimible."""
        require_collections()
        liqui = get_liquidacion(liquidacion_id)
        if not liqui:
            abort(404)
        return render_template("comprobante_liquidacion.html", liqui=liqui, config=get_config())

    @app.route("/liquidaciones/<int:liquidacion_id>/abono", methods=["POST"])
    @role_required("admin")
    def abono_liquidacion(liquidacion_id: int) -> Response:
        """Registrar un abono (pago parcial) a una liquidación."""
        require_collections()
        monto = parse_money(request.form.get("monto", ""))
        metodo = request.form.get("metodo", METODO_EFECTIVO).strip().lower()
        fecha = request.form.get("fecha", "").strip()
        observaciones = request.form.get("observaciones", "").strip()
        if metodo not in METODOS_PAGO:
            metodo = METODO_EFECTIVO
        try:
            liqui = registrar_abono_liquidacion(liquidacion_id, monto, metodo=metodo, fecha=fecha, observaciones=observaciones)
        except Exception as exc:
            flash(f"No se pudo registrar el abono: {safe_error_message(exc)}", "danger")
            return redirect(url_for("comprobante_liquidacion", liquidacion_id=liquidacion_id))
        flash(f"Abono de ${monto:,} registrado en la liquidación N° {liquidacion_id:05d}.", "success")
        return redirect(url_for("liquidacion_vendedor", vendedor_id=liqui["vendedor_id"]))

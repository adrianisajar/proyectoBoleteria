from flask import Flask, Response, current_app

from database import liquidaciones
from motores.errores import safe_error_message
from motores.liquidacion_service import (
    generar_liquidacion,
    get_liquidacion,
    get_liquidacion_detalle,
    get_liquidaciones_resumen,
)
from motores.pdf_service import generar_pdf_liquidacion
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
        if liquidaciones.find_one({"vendedor_id": vendedor_id}) and request.form.get("confirmar_regen") != "1":
            flash("Ya existe un comprobante para este vendedor. Marque la confirmación para generar uno nuevo.", "warning")
            return redirect(url_for("liquidacion_vendedor", vendedor_id=vendedor_id))
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

    @app.route("/liquidaciones/<int:liquidacion_id>/pdf")
    @role_required("admin", "cajero", "consulta")
    def descargar_liquidacion_pdf(liquidacion_id: int) -> Response:
        """Generate and download liquidación comprobante as PDF (server-side via WeasyPrint)."""
        require_collections()
        liqui = get_liquidacion(liquidacion_id)
        if not liqui:
            abort(404)
        config = get_config()
        try:
            pdf_bytes = generar_pdf_liquidacion(liqui, config)
        except Exception as exc:
            current_app.logger.exception("Error generando PDF liquidación %s", liquidacion_id)
            abort(500, description=f"Error generando PDF: {exc}")
        filename = f"LIQ-{liquidacion_id:05d}.pdf"
        return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename={filename}"})

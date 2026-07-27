from datetime import datetime

from motores.constants import CONFIG_ID, COMISION_DEFAULT_TIERS, DEFAULT_CONFIG
from motores.fechas import now_local
from motores.validacion import parse_money

from motores.shared import (
    configuracion, rifas,
    request, flash, redirect, render_template, url_for,
    get_config, get_rifa_activa, invalidate_config_cache, invalidate_dashboard_cache,
    require_collections, role_required, sync_ticket_statuses,
    importar_modelo_rifa, crear_nueva_rifa,
)


def register_routes(app):

    @app.route("/configuracion", methods=["GET", "POST"])
    @role_required("admin")
    def configuracion_panel():
        require_collections()
        config = get_config()
        rifa = get_rifa_activa()
        if request.method == "POST":
            action = request.form.get("action", "")

            if action == "guardar_empresa":
                update = {
                    "nombre_empresa": request.form.get("nombre_empresa", "").strip(),
                    "direccion": request.form.get("direccion", "").strip().upper(),
                    "telefono": request.form.get("telefono", "").strip(),
                    "ciudad": request.form.get("ciudad", "").strip().upper(),
                    "footer_texto": request.form.get("footer_texto", "").strip(),
                    "observaciones_recaudo": request.form.get("observaciones_recaudo", "").strip(),
                }
                configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
                invalidate_config_cache()
                invalidate_dashboard_cache()
                flash("Datos de la empresa guardados.", "success")
                return redirect(url_for("configuracion_panel"))

            elif action == "guardar_config":
                valor_boleta = parse_money(request.form.get("valor_boleta", ""))
                nombre = request.form.get("nombre_rifa", "").strip() or DEFAULT_CONFIG["nombre_rifa"]
                cantidad_boletas = parse_money(request.form.get("cantidad_boletas", "")) or 0

                errors = []
                if valor_boleta <= 0:
                    errors.append("El valor de la boleta debe ser mayor que cero.")
                if cantidad_boletas < 1:
                    errors.append("La cantidad de boletas debe ser al menos 1.")

                if errors:
                    for error in errors:
                        flash(error, "danger")
                else:
                    update = {
                        "nombre_rifa": nombre,
                        "valor_boleta": valor_boleta,
                        "cantidad_boletas": cantidad_boletas,
                    }
                    configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
                    try:
                        rifas.update_one({"estado": "activa"}, {"$set": {"nombre": nombre, "valor_boleta": valor_boleta, "cantidad_boletas": cantidad_boletas}})
                    except Exception:
                        flash("Advertencia: no se pudo actualizar el documento de la rifa.", "warning")
                    sync_ticket_statuses(valor_boleta)
                    invalidate_dashboard_cache()
                    invalidate_config_cache()
                    flash("Parámetros de la rifa guardados.", "success")
                    return redirect(url_for("configuracion_panel"))

            elif action == "guardar_comisiones":
                try:
                    indices = request.form.getlist("tier_idx")
                    nuevos_tiers = []
                    for idx in indices:
                        min_val = parse_money(request.form.get(f"tier_min_{idx}", ""))
                        valor = parse_money(request.form.get(f"tier_valor_{idx}", ""))
                        if min_val is not None and valor is not None and min_val >= 0 and valor >= 0:
                            nuevos_tiers.append({"min": min_val, "valor": valor})
                    if not nuevos_tiers:
                        flash("Debe haber al menos un tier de comisión.", "danger")
                    else:
                        nuevos_tiers.sort(key=lambda t: t["min"])
                        update = {"comisiones_tiers": nuevos_tiers}
                        configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
                        try:
                            rifas.update_one({"estado": "activa"}, {"$set": update})
                        except Exception:
                            flash("Advertencia: no se pudo actualizar comisiones en el documento de rifa.", "warning")
                        invalidate_config_cache()
                        flash("Comisiones guardadas correctamente.", "success")
                except Exception as exc:
                    flash(f"Error al guardar comisiones: {exc}", "danger")
                return redirect(url_for("configuracion_panel"))

        return render_template("configuracion.html", config=config, rifa=rifa)

    @app.route("/rifas/nueva", methods=["POST"])
    @role_required("admin")
    def nueva_rifa():
        nombre = request.form.get("nombre_rifa_nueva", "").strip() or f"Rifa {now_local().date().isoformat()}"
        valor_boleta = parse_money(request.form.get("valor_boleta_nueva", ""))
        conservar_vendedores = request.form.get("conservar_vendedores") == "on"
        confirmacion = request.form.get("confirmacion", "").strip().upper()
        cantidad_boletas = parse_money(request.form.get("cantidad_boletas", "10000")) or 10000
        premio_mayor = request.form.get("premio_mayor", "").strip()
        estado = request.form.get("estado", "activa").strip()

        errors = []
        if valor_boleta <= 0:
            errors.append("El valor de la nueva rifa debe ser mayor que cero.")
        if cantidad_boletas < 1:
            errors.append("La cantidad de boletas debe ser al menos 1.")
        if confirmacion != "NUEVA RIFA":
            errors.append("Escribe NUEVA RIFA para confirmar la reinicializaci\u00f3n.")

        if errors:
            for error in errors:
                flash(error, "danger")
            return redirect(url_for("configuracion_panel"))

        try:
            crear_nueva_rifa(
                nombre,
                valor_boleta,
                conservar_vendedores,
                cantidad_boletas=cantidad_boletas,
                premio_mayor=premio_mayor,
                estado=estado,
            )
        except Exception as exc:
            flash(f"No se pudo crear la nueva rifa: {exc}", "danger")
            return redirect(url_for("configuracion_panel"))

        flash("Nueva rifa creada correctamente.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/rifas/importar", methods=["POST"])
    @role_required("admin")
    def importar_rifa_excel():
        archivo = request.files.get("archivo_rifa")
        confirmacion = request.form.get("confirmacion_importacion", "").strip().upper()

        if confirmacion != "IMPORTAR":
            flash("Escribe IMPORTAR para confirmar la actualizaci├│n desde Excel.", "danger")
            return redirect(url_for("configuracion_panel"))

        if not archivo or not archivo.filename:
            flash("Selecciona un archivo .xlsx para importar.", "danger")
            return redirect(url_for("configuracion_panel"))

        if not archivo.filename.lower().endswith(".xlsx"):
            flash("El archivo debe tener formato .xlsx.", "danger")
            return redirect(url_for("configuracion_panel"))

        try:
            summary = importar_modelo_rifa(archivo.stream)
        except Exception as exc:
            flash(f"No se pudo importar el modelo de rifa: {exc}", "danger")
            return redirect(url_for("configuracion_panel"))

        omitidas = []
        if summary.get("local_ignoradas"):
            omitidas.append(f"{summary['local_ignoradas']} LOCAL")
        if summary.get("camion_ignoradas"):
            omitidas.append(f"{summary['camion_ignoradas']} CAMI\u00d3N")
        if summary.get("paquete_ignoradas"):
            omitidas.append(f"{summary['paquete_ignoradas']} PAQUETE")
        omit_msg = f" ({', '.join(omitidas)} omitida(s))" if omitidas else ""

        message = (
            f"Asignaciones actualizadas: {summary['boletas_asignadas']} boleta(s) procesada(s), "
            f"{summary['vendedores']} vendedor(es), {summary['boletas_actualizadas']} actualizada(s)"
            f"{omit_msg}."
        )
        if summary["invalid_rows"]:
            message += " Filas omitidas: " + ", ".join(str(row) for row in summary["invalid_rows"])
        flash(message, "success")
        return redirect(url_for("dashboard"))

    @app.route("/rifas/sincronizar-estados", methods=["POST"])
    @role_required("admin")
    def sincronizar_estados():
        try:
            require_collections()
            config = get_config()
            valor_boleta = int(config.get("valor_boleta", 10000) or 10000)
            sync_ticket_statuses(valor_boleta)
            flash("Estados sincronizados correctamente.", "success")
        except Exception as exc:
            flash(f"Error al sincronizar estados: {exc}", "danger")
        return redirect(url_for("configuracion_panel"))

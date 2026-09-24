from flask import Flask, Response, current_app

from motores.config_service import get_rifa_activa
from motores.constants import CONFIG_ID, DEFAULT_CONFIG
from motores.fechas import now_local
from motores.shared import (
    configuracion,
    crear_nueva_rifa,
    flash,
    get_config,
    invalidate_config_cache,
    invalidate_dashboard_cache,
    redirect,
    render_template,
    request,
    require_collections,
    rifas,
    role_required,
    sync_ticket_statuses,
    url_for,
)
from motores.usuarios import list_usuarios, requiere_clave_admin
from motores.validacion import parse_money, safe_error_message, sanitizar_texto


def register_routes(app: Flask) -> None:
    """Register the config panel and rifa lifecycle routes."""

    @app.route("/configuracion", methods=["GET", "POST"])
    @role_required("admin")
    def configuracion_panel() -> str | Response:
        """Config page: company data, rifa params, commission tiers."""
        require_collections()
        config = get_config()
        rifa = get_rifa_activa()
        if request.method == "POST":
            action = request.form.get("action", "")
            if action in ("guardar_config", "guardar_empresa", "guardar_comisiones") and not requiere_clave_admin():
                return redirect(url_for("configuracion_panel"))

            if action == "guardar_empresa":
                update = {
                    "nombre_empresa": sanitizar_texto(request.form.get("nombre_empresa", ""), "titulo"),
                    "direccion": sanitizar_texto(request.form.get("direccion", ""), "address").upper(),
                    "telefono": sanitizar_texto(request.form.get("telefono", ""), "numbers"),
                    "ciudad": sanitizar_texto(request.form.get("ciudad", ""), "titulo").upper(),
                }
                try:
                    configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
                    invalidate_config_cache()
                    invalidate_dashboard_cache()
                    flash("Datos de la empresa guardados.", "success")
                except Exception as exc:
                    flash(safe_error_message(exc), "danger")
                return redirect(url_for("configuracion_panel"))

            elif action == "guardar_config":
                valor_boleta = parse_money(request.form.get("valor_boleta", ""))
                nombre = sanitizar_texto(request.form.get("nombre_rifa", ""), "titulo") or DEFAULT_CONFIG["nombre_rifa"]
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
                    try:
                        rifas.update_one({"estado": "activa"}, {"$set": {"nombre": nombre, "valor_boleta": valor_boleta, "cantidad_boletas": cantidad_boletas}})
                        # Limpiar overrides legacy en configuracion para que
                        # get_config() lea de rifas sin stale overrides.
                        configuracion.update_one({"_id": CONFIG_ID}, {"$unset": {"nombre_rifa": "", "valor_boleta": "", "cantidad_boletas": ""}})
                    except Exception as exc:
                        flash(safe_error_message(exc), "danger")
                        return redirect(url_for("configuracion_panel"))
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
                        try:
                            rifas.update_one({"estado": "activa"}, {"$set": update})
                        except Exception as exc:
                            current_app.logger.warning("No se pudo actualizar comisiones en el documento de rifa: %s", exc)
                            flash("Advertencia: no se pudo actualizar comisiones en el documento de rifa.", "warning")
                        invalidate_config_cache()
                        flash("Comisiones guardadas correctamente.", "success")
                except Exception as exc:
                    flash(safe_error_message(exc), "danger")
                return redirect(url_for("configuracion_panel"))

        sort_by = request.args.get("sort_by", "rol").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        if sort_by not in {"_id", "usuario", "nombre", "rol", "activo", "ultimo_acceso"}:
            sort_by = "rol"
        return render_template("configuracion.html", config=config, rifa=rifa, usuarios=list_usuarios(sort_by, sort_dir), sort_by=sort_by, sort_dir=sort_dir)

    @app.route("/rifas/nueva", methods=["POST"])
    @role_required("admin")
    def nueva_rifa() -> Response:
        """Reset the system for a new rifa (admin-password-gated, optional vendor keep)."""
        if not requiere_clave_admin():
            return redirect(url_for("configuracion_panel"))
        nombre = sanitizar_texto(request.form.get("nombre_rifa_nueva", ""), "titulo") or f"Rifa {now_local().date().isoformat()}"
        valor_boleta = parse_money(request.form.get("valor_boleta_nueva", ""))
        conservar_vendedores = request.form.get("conservar_vendedores") == "on"
        conservar_reservas = request.form.get("conservar_reservas", "on") == "on"
        cantidad_boletas = parse_money(request.form.get("cantidad_boletas", "10000")) or 10000

        errors = []
        if valor_boleta <= 0:
            errors.append("El valor de la nueva rifa debe ser mayor que cero.")
        if cantidad_boletas < 1:
            errors.append("La cantidad de boletas debe ser al menos 1.")

        if errors:
            for error in errors:
                flash(error, "danger")
            return redirect(url_for("configuracion_panel"))

        try:
            resumen = crear_nueva_rifa(
                nombre,
                valor_boleta,
                conservar_vendedores,
                cantidad_boletas=cantidad_boletas,
                conservar_reservas=conservar_reservas,
            )
        except Exception as exc:
            flash(safe_error_message(exc), "danger")
            return redirect(url_for("configuracion_panel"))

        flash("Nueva rifa creada correctamente.", "success")
        if conservar_reservas:
            aplicadas = int((resumen or {}).get("reservas_aplicadas", 0) or 0)
            omitidas = (resumen or {}).get("reservas_omitidas", []) or []
            if aplicadas:
                flash(f"{aplicadas} reserva(s) fija(s) conservadas como separadas.", "success")
            if omitidas:
                flash(f"{len(omitidas)} reserva(s) omitida(s) (fuera de rango o sin comprador).", "warning")
        return redirect(url_for("dashboard"))

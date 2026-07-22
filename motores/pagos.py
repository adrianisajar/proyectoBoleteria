from motores.validacion import parse_boletas
from motores.constants import OPERACIONES_VENDEDOR, BOLETA_MIN, BOLETA_MAX

from motores.shared import (
    boletas, vendedores,
    request, flash, redirect, render_template, url_for, jsonify,
    get_config, require_collections, role_required,
    safe_vendedores_snapshot,
    normalize_vendedor_id, existing_boleta_ids,
    estado_pipeline_expr,
)


def _actualizar_estado_boletas(filtro, nuevo_vendedor_id, valor_boleta):
    boletas.update_many(
        filtro,
        [
            {"$set": {"vendedor_id": nuevo_vendedor_id}},
            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
        ],
    )


def _render_vendedores(form_data):
    vendedores_lista, resumen = safe_vendedores_snapshot()
    return render_template("vendedores.html", form=form_data, vendedores_lista=vendedores_lista, resumen=resumen)


def _validar_form_vendedor(form_data):
    errors = []
    vendedor_id = ""
    try:
        require_collections()
        raw_id = form_data.get("vendedor_id", "").strip()
        if not raw_id:
            raw_id = form_data["nombre"]
        vendedor_id = normalize_vendedor_id(raw_id)
        form_data["vendedor_id"] = vendedor_id
    except (RuntimeError, ValueError) as exc:
        errors.append(str(exc))

    if form_data["operacion"] not in OPERACIONES_VENDEDOR:
        errors.append("Selecciona una operaci\u00f3n v\u00e1lida para el vendedor.")

    boleta_ids, invalid, out_of_range = parse_boletas(form_data["boletas"])
    if invalid:
        errors.append("Hay entradas no num\u00e9ricas: " + ", ".join(invalid[:8]))
    if out_of_range:
        errors.append("Hay boletas fuera del rango 0000-9999: " + ", ".join(out_of_range[:8]))
    if form_data["operacion"] in {"asignar", "quitar", "reemplazar"} and not boleta_ids:
        errors.append("Ingresa al menos una boleta para esta operaci\u00f3n.")

    return vendedor_id, boleta_ids, errors


def _procesar_guardar(vendedor_id, perfil_update):
    vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)
    flash(f"Vendedor {vendedor_id} guardado.", "success")


def _procesar_asignar(vendedor_id, boleta_ids, perfil_update, valor_boleta):
    existentes = existing_boleta_ids(boleta_ids)
    faltantes = len(boleta_ids) - len(existentes)
    vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)

    if existentes:
        vendedores.update_many(
            {"_id": {"$ne": vendedor_id}},
            {"$pull": {"boletas_asignadas": {"$in": existentes}}},
        )
        vendedores.update_one(
            {"_id": vendedor_id},
            {"$addToSet": {"boletas_asignadas": {"$each": existentes}}},
        )
        _actualizar_estado_boletas({"_id": {"$in": existentes}}, vendedor_id, valor_boleta)

    mensaje = f"{len(existentes)} boleta(s) asignada(s) a {vendedor_id}."
    if faltantes:
        mensaje += f" {faltantes} no exist\u00edan en la colecci\u00f3n boletas."
    flash(mensaje, "success" if existentes else "warning")


def _procesar_quitar(vendedor_id, boleta_ids, perfil_update, valor_boleta):
    existentes = existing_boleta_ids(boleta_ids)
    faltantes = len(boleta_ids) - len(existentes)
    vendedores.update_one({"_id": vendedor_id}, perfil_update, upsert=True)
    vendedores.update_one({"_id": vendedor_id}, {"$pull": {"boletas_asignadas": {"$in": existentes}}})
    if existentes:
        _actualizar_estado_boletas({"_id": {"$in": existentes}, "vendedor_id": vendedor_id}, "", valor_boleta)
    mensaje = f"{len(existentes)} boleta(s) quitada(s) de {vendedor_id}."
    if faltantes:
        mensaje += f" {faltantes} no exist\u00edan en la colecci\u00f3n boletas."
    flash(mensaje, "success" if existentes else "warning")


def _procesar_reemplazar(vendedor_id, boleta_ids, perfil_set, valor_boleta):
    existentes = existing_boleta_ids(boleta_ids)
    faltantes = len(boleta_ids) - len(existentes)
    vendedores.update_many(
        {"_id": {"$ne": vendedor_id}},
        {"$pull": {"boletas_asignadas": {"$in": existentes}}},
    )
    vendedores.update_one(
        {"_id": vendedor_id},
        {"$set": {**perfil_set, "boletas_asignadas": existentes}},
        upsert=True,
    )
    _actualizar_estado_boletas({"vendedor_id": vendedor_id, "_id": {"$nin": existentes}}, "", valor_boleta)
    if existentes:
        _actualizar_estado_boletas({"_id": {"$in": existentes}}, vendedor_id, valor_boleta)
    mensaje = f"Lista de {vendedor_id} reemplazada con {len(existentes)} boleta(s)."
    if faltantes:
        mensaje += f" {faltantes} no exist\u00edan en la colecci\u00f3n boletas."
    flash(mensaje, "success")


def _procesar_eliminar(vendedor_id, valor_boleta):
    vendedor_doc = vendedores.find_one({"_id": vendedor_id})
    if not vendedor_doc:
        flash(f"El vendedor {vendedor_id} no existe.", "danger")
        return
    boletas_ids_vendor = [
        num for num in vendedor_doc.get("boletas_asignadas", [])
        if isinstance(num, int) and BOLETA_MIN <= num <= BOLETA_MAX
    ]
    if boletas_ids_vendor:
        _actualizar_estado_boletas({"_id": {"$in": boletas_ids_vendor}}, "", valor_boleta)
    vendedores.delete_one({"_id": vendedor_id})
    flash(f"Vendedor {vendedor_id} ({vendedor_doc.get('nombre', '')}) eliminado con {len(boletas_ids_vendor)} boleta(s) liberada(s).", "success")


def register_routes(app):
    @app.route("/vendedores", methods=["GET", "POST"])
    @role_required("admin")
    def vendedores_panel():
        config = get_config()
        valor_boleta = int(config["valor_boleta"])
        form_data = {
            "vendedor_id": "",
            "nombre": "",
            "telefono": "",
            "operacion": "guardar",
            "boletas": "",
        }

        if request.method == "POST":
            form_data.update(
                {
                    "vendedor_id": request.form.get("vendedor_id", ""),
                    "nombre": request.form.get("nombre", "").strip(),
                    "telefono": request.form.get("telefono", "").strip(),
                    "operacion": request.form.get("operacion", "guardar").strip().lower(),
                    "boletas": request.form.get("boletas", "").strip(),
                }
            )

            vendedor_id, boleta_ids, errors = _validar_form_vendedor(form_data)

            if errors:
                for error in errors:
                    flash(error, "danger")
                return _render_vendedores(form_data)

            perfil_set = {
                "nombre": form_data["nombre"],
                "telefono": form_data["telefono"],
            }
            perfil_update = {"$set": perfil_set, "$setOnInsert": {"boletas_asignadas": []}}

            try:
                operacion = form_data["operacion"]
                if operacion == "guardar":
                    _procesar_guardar(vendedor_id, perfil_update)
                elif operacion == "asignar":
                    _procesar_asignar(vendedor_id, boleta_ids, perfil_update, valor_boleta)
                elif operacion == "quitar":
                    _procesar_quitar(vendedor_id, boleta_ids, perfil_update, valor_boleta)
                elif operacion == "reemplazar":
                    _procesar_reemplazar(vendedor_id, boleta_ids, perfil_set, valor_boleta)
                elif operacion == "eliminar":
                    _procesar_eliminar(vendedor_id, valor_boleta)
            except Exception as exc:
                flash(f"No se pudo aplicar la operaci\u00f3n del vendedor: {exc}", "danger")
                return _render_vendedores(form_data)

            return redirect(url_for("vendedores_panel"))

        return _render_vendedores(form_data)

    @app.route("/api/vendedores/<vendedor_id>/boletas")
    @role_required("admin", "cajero", "consulta")
    def api_vendedor_boletas(vendedor_id):
        require_collections()
        try:
            docs = list(boletas.find(
                {"vendedor_id": vendedor_id},
                {"_id": 1, "estado": 1, "total_abonado": 1, "cliente": 1},
            ).sort("_id", 1))
            boletas_list = []
            for d in docs:
                cliente = d.get("cliente") or {}
                boletas_list.append({
                    "numero": f"{d['_id']:04d}",
                    "estado": d.get("estado", "disponible"),
                    "abonado": int(d.get("total_abonado", 0) or 0),
                    "cliente": cliente.get("nombre", ""),
                })
            return jsonify({"ok": True, "total": len(boletas_list), "boletas": boletas_list})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500



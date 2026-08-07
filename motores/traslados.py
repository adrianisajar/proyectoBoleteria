"""Traslado de saldo routes (cambio de n\u00famero, admin only)."""

from datetime import datetime

from flask import Flask, Response

from database import boletas, traslados, vendedores
from motores.constants import USUARIO_SISTEMA, VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL
from motores.fechas import now_local
from motores.shared import (
    current_user,
    flash,
    get_config,
    redirect,
    render_template,
    request,
    require_collections,
    role_required,
    url_for,
)
from motores.traslado_service import next_traslado_id, registrar_traslado, revertir_traslado
from motores.validacion import es_boleta_completa, parse_money


def _vendedores_con_local() -> list[dict]:
    lista = list(vendedores.find().sort("_id", 1))
    return [*[{"_id": VENDEDOR_LOCAL, "nombre": VENDEDOR_LOCAL_LABEL}], *lista]


def _validar_traslado(origen: int, destino: int, valor: int, docs: dict, valor_boleta: int, errors: list[str]) -> None:
    """Cross-check balance limits for a traslado between two existing tickets."""
    doc_origen = docs[origen]
    doc_destino = docs[destino]
    if doc_origen.get("rifa_id") != doc_destino.get("rifa_id"):
        errors.append("Las dos boletas deben pertenecer a la misma rifa.")
        return
    saldo_origen = int(doc_origen.get("total_abonado") or 0)
    saldo_destino = int(doc_destino.get("total_abonado") or 0)
    if saldo_origen <= 0:
        errors.append(f"La boleta #{origen:04d} no tiene saldo que trasladar.")
    elif valor > saldo_origen:
        errors.append(f"El valor ${valor:,} supera el saldo disponible ${saldo_origen:,} de la boleta #{origen:04d}.")
    if saldo_destino + valor > valor_boleta:
        errors.append(f"La boleta #{destino:04d} quedar\u00eda con ${saldo_destino + valor:,}, superando el valor de la boleta (${valor_boleta:,}).")


def register_routes(app: Flask) -> None:
    """Register traslado creation, listing and comprobante routes."""

    @app.route("/traslados")
    @role_required("admin", "cajero")
    def traslados_list() -> str:
        """List traslados (comprobantes de cambio de n\u00famero)."""
        require_collections()
        lista = list(traslados.find().sort([("fecha", -1), ("_id", -1)]).limit(100))
        return render_template("traslados_list.html", traslados=lista)

    @app.route("/traslados/nuevo", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def nuevo_traslado() -> str | Response:
        """Create a traslado moving saldo between two tickets."""
        require_collections()
        today = now_local().strftime("%Y-%m-%d")
        empty_form = {
            "origen": "",
            "destino": "",
            "valor": "",
            "fecha": "",
            "vendedor_id": "",
            "vendedor_nombre": "",
        }

        if request.method == "POST":
            origen_raw = request.form.get("origen", "").strip()
            destino_raw = request.form.get("destino", "").strip()
            valor_raw = request.form.get("valor", "").strip()
            fecha = request.form.get("fecha", "").strip()
            vendedor_id = request.form.get("vendedor_id", "").strip()
            v_nombre = VENDEDOR_LOCAL_LABEL if vendedor_id == VENDEDOR_LOCAL else ""
            if vendedor_id and vendedor_id != VENDEDOR_LOCAL:
                _v = vendedores.find_one({"_id": vendedor_id}, {"nombre": 1})
                v_nombre = (_v or {}).get("nombre", vendedor_id) if _v else vendedor_id
            form_data = {
                "origen": origen_raw,
                "destino": destino_raw,
                "valor": valor_raw,
                "fecha": fecha,
                "vendedor_id": vendedor_id,
                "vendedor_nombre": v_nombre,
            }
            vendedores_list = _vendedores_con_local()

            errors = []
            if not es_boleta_completa(origen_raw):
                errors.append("Debe indicar la boleta de origen (4 d\u00edgitos).")
            if not es_boleta_completa(destino_raw):
                errors.append("Debe indicar la boleta de destino (4 d\u00edgitos).")
            if not fecha:
                errors.append("Debe indicar la fecha del traslado.")
            else:
                try:
                    fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
                    if fecha_dt.date() > now_local().date():
                        errors.append("La fecha no puede ser posterior a hoy.")
                except ValueError:
                    errors.append("Formato de fecha inv\u00e1lido.")
            if not vendedor_id:
                errors.append("Debe seleccionar el vendedor que realiza el traslado.")
            valor = parse_money(valor_raw)
            if valor <= 0:
                errors.append("El valor a trasladar debe ser mayor que cero.")

            _cfg = get_config()
            _vb = int(_cfg.get("valor_boleta", 10000) or 10000)

            if not errors:
                origen = int(origen_raw)
                destino = int(destino_raw)
                if origen == destino:
                    errors.append("La boleta de origen y destino deben ser distintas.")
                else:
                    docs_map, missing = _verificar_existen([origen, destino])
                    if missing:
                        errors.append(f"Boletas no encontradas: {', '.join(f'#{b:04d}' for b in sorted(missing))}")
                    else:
                        _validar_traslado(origen, destino, valor, docs_map, _vb, errors)

            if errors:
                for e in errors:
                    flash(e, "danger")
                return render_template(
                    "nuevo_traslado.html",
                    vendedores=vendedores_list,
                    today=today,
                    form=form_data,
                )

            traslado_id = None
            try:
                traslado_id = next_traslado_id()
                v = vendedores.find_one({"_id": vendedor_id})
                v_nombre = VENDEDOR_LOCAL_LABEL if vendedor_id == VENDEDOR_LOCAL else (v.get("nombre", vendedor_id) if v else vendedor_id)

                user = current_user() or {}
                usuario = user.get("username") or USUARIO_SISTEMA

                registrar_traslado(
                    traslado_id,
                    origen,
                    destino,
                    valor,
                    fecha,
                    vendedor_id,
                    v_nombre,
                    usuario,
                    user.get("nombre") or user.get("username") or usuario,
                )

                flash(f"Traslado N\u00b0 {traslado_id:05d} registrado.", "success")
                return redirect(url_for("ver_traslado", traslado_id=traslado_id, imprimir=1))

            except Exception as exc:
                if traslado_id is not None:
                    revertir_traslado(traslado_id, _vb)
                flash(f"Error al registrar el traslado: {exc}", "danger")
                return render_template(
                    "nuevo_traslado.html",
                    vendedores=vendedores_list,
                    today=today,
                    form=form_data,
                )

        return render_template(
            "nuevo_traslado.html",
            vendedores=_vendedores_con_local(),
            today=today,
            form=empty_form,
        )

    @app.route("/traslados/<int:traslado_id>")
    @role_required("admin", "cajero")
    def ver_traslado(traslado_id: int) -> str:
        """Show a traslado comprobante (printable)."""
        require_collections()
        traslado = traslados.find_one({"_id": traslado_id})
        if not traslado:
            flash(f"Traslado N\u00b0 {traslado_id:05d} no encontrado.", "danger")
            return redirect(url_for("traslados_list"))
        origen = boletas.find_one({"_id": traslado["boleta_origen"]})
        destino = boletas.find_one({"_id": traslado["boleta_destino"]})
        return render_template(
            "traslado.html",
            traslado=traslado,
            origen=origen or {},
            destino=destino or {},
        )


def _verificar_existen(boleta_ids: list[int]) -> tuple[dict, list[int]]:
    """Return (docs_map, missing_ids) for the given ticket ids."""
    docs = {d["_id"]: d for d in boletas.find({"_id": {"$in": boleta_ids}})}
    missing = [b for b in boleta_ids if b not in docs]
    return docs, missing

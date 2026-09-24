import contextlib
import uuid
from datetime import datetime

from flask import Flask, Response, current_app
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError
from werkzeug.exceptions import BadRequest

from motores.auth import current_user
from motores.constants import (
    BOLETA_MAX,
    BOLETA_MIN,
    METODO_EFECTIVO,
    METODO_TRANSFERENCIA,
    MOV_PAGO,
    MOVIMIENTOS_FIELD,
    USUARIO_SISTEMA,
    VENDEDOR_LOCAL,
)
from motores.errores import safe_error_message
from motores.fechas import now_local
from motores.shared import (
    boletas,
    estado_pipeline_expr,
    flash,
    get_config,
    invalidate_dashboard_cache,
    jsonify,
    redirect,
    render_template,
    request,
    require_collections,
    reservas,
    role_required,
    url_for,
)
from motores.validacion import parse_money, sanitizar_texto


def _procesar_rows_rapido(rows: list, confirmar_pagadas: bool = False) -> dict:
    """Bulk-apply client data from quick-entry rows and return a summary dict.

    Each row may also carry `fecha_adquisicion` (optional, never overwrites
    when absent) and a one-shot `pago` (+ `pago_metodo`) recorded straight
    into the ledger without factura, referencia or banco.

    Payments to tickets already `pagada` are only registered when
    `confirmar_pagadas` is set (excedente); otherwise they are reported in
    `requiere_confirmacion_pagadas` so the UI can ask for confirmation.
    """
    require_collections()
    config = get_config()
    valor_boleta_local = int(config.get("valor_boleta", 10000) or 10000)
    today = now_local().date().isoformat()
    usuario = (current_user() or {}).get("username", USUARIO_SISTEMA)
    operacion_id = uuid.uuid4().hex

    boleta_ids = [r["boleta"] for r in rows if isinstance(r.get("boleta"), int)]
    docs_map = {d["_id"]: d for d in boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1, "total_abonado": 1, "estado": 1})}
    totales = {bid: int(d.get("total_abonado") or 0) for bid, d in docs_map.items()}
    estados = {bid: d.get("estado", "") for bid, d in docs_map.items()}

    ops = []
    cliente_bids = []
    not_found = []
    ids_con_nombre = []
    ids_sin_datos = []
    errores = []
    pagos_rechazados = []
    pagos_plan = []
    pagadas_sin_confirmar = []

    for row in rows:
        bid = row.get("boleta")
        if not isinstance(bid, int) or bid not in totales:
            if isinstance(bid, int):
                not_found.append(bid)
            continue

        fecha_adq = (row.get("fecha_adquisicion") or "").strip()
        if fecha_adq:
            try:
                datetime.strptime(fecha_adq, "%Y-%m-%d")
            except ValueError:
                errores.append(f"#{bid:04d}: fecha de adquisición inválida.")
                continue
            if fecha_adq > today:
                errores.append(f"#{bid:04d}: la fecha no puede ser posterior a hoy.")
                continue

        nombre = sanitizar_texto(row.get("nombre"), "name").upper()
        telefono = sanitizar_texto(row.get("telefono"), "numbers")
        direccion = sanitizar_texto(row.get("direccion"), "address").upper()
        if not nombre and not telefono and not direccion:
            ids_sin_datos.append(bid)
            continue

        set_fields: dict = {
            "cliente.nombre": nombre,
            "cliente.telefono": telefono,
            "cliente.direccion": direccion,
        }
        if nombre and totales.get(bid, 0) == 0 and estados.get(bid) not in ("pagada", "abonando"):
            vendedor_actual = (docs_map.get(bid) or {}).get("vendedor_id", "")
            if not vendedor_actual or vendedor_actual == VENDEDOR_LOCAL:
                set_fields["vendedor_id"] = VENDEDOR_LOCAL
        if fecha_adq:
            set_fields["fecha_adquisicion"] = fecha_adq
        ops.append(UpdateOne({"_id": bid}, [{"$set": set_fields}]))
        cliente_bids.append(bid)
        if nombre:
            ids_con_nombre.append(bid)

        pago_raw = row.get("pago", "")
        if pago_raw is None or (isinstance(pago_raw, str) and not pago_raw.strip()):
            continue
        if isinstance(pago_raw, str) and "-" in pago_raw:
            pagos_rechazados.append(f"#{bid:04d}: pago negativo no permitido.")
            continue
        pago = parse_money(pago_raw) if isinstance(pago_raw, str) else int(pago_raw or 0)
        if pago <= 0:
            continue
        metodo = (row.get("pago_metodo") or METODO_EFECTIVO).strip().lower()
        if metodo not in (METODO_EFECTIVO, METODO_TRANSFERENCIA):
            pagos_rechazados.append(f"#{bid:04d}: método de pago inválido.")
            continue
        if pago > valor_boleta_local:
            pagos_rechazados.append(f"#{bid:04d}: el pago ${pago:,} supera el valor de la boleta (${valor_boleta_local:,}).")
            continue
        if estados.get(bid) == "pagada" and not confirmar_pagadas:
            pagadas_sin_confirmar.append(f"#{bid:04d} (${totales.get(bid, 0):,} abonado)")
            continue
        pagos_plan.append((bid, pago, metodo, fecha_adq or today))

    for bid, pago, metodo, fecha_pago in pagos_plan:
        mov = {
            "tipo": MOV_PAGO,
            "fecha": fecha_pago,
            "valor": pago,
            "metodo": metodo,
            "registrado_en": now_local(),
            "usuario": usuario,
            "operacion_id": operacion_id,
        }
        ops.append(
            UpdateOne(
                {
                    "_id": bid,
                    # Sin confirmación expresa se excluyen las ya pagadas;
                    # con confirmación reciben el pago como excedente.
                    **({} if confirmar_pagadas else {"estado": {"$ne": "pagada"}}),
                },
                [
                    {
                        "$set": {
                            MOVIMIENTOS_FIELD: {"$concatArrays": [{"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}, {"$literal": [mov]}]},
                            "total_abonado": {"$add": [{"$ifNull": ["$total_abonado", 0]}, pago]},
                        }
                    },
                    {"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}},
                ],
            )
        )

    updated = 0
    pagos_registrados = 0
    pagos_total = 0
    pagos_omitidos = 0
    if ops:
        boletas.bulk_write(ops, ordered=False)
        # Los ops de cliente siempre coinciden (boleta verificada); el
        # desfase solo puede venir de pagos a boletas que quedaron pagadas
        # por otra operación concurrente (filtro estado != pagada).
        updated = len(cliente_bids)
        movimientos_confirmados = boletas.find(
            {MOVIMIENTOS_FIELD: {"$elemMatch": {"operacion_id": operacion_id}}},
            {MOVIMIENTOS_FIELD: 1},
        )
        for doc in movimientos_confirmados:
            for movimiento in doc.get(MOVIMIENTOS_FIELD) or []:
                if movimiento.get("operacion_id") == operacion_id:
                    pagos_registrados += 1
                    pagos_total += int(movimiento.get("valor", 0) or 0)
        pagos_omitidos = len(pagos_plan) - pagos_registrados

        if ids_con_nombre:
            boletas.update_many(
                {
                    "_id": {"$in": ids_con_nombre},
                    "$or": [
                        {"vendedor_id": {"$in": ["", VENDEDOR_LOCAL]}},
                        {"vendedor_id": None},
                    ],
                    "total_abonado": 0,
                },
                {"$set": {"vendedor_id": VENDEDOR_LOCAL}},
            )

        boletas.update_many(
            {"_id": {"$in": ids_con_nombre}},
            [{"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}}],
        )

        invalidate_dashboard_cache()

    return {
        "ok": True,
        "updated": updated,
        "total": len(rows),
        "not_found": not_found,
        "sin_datos": ids_sin_datos,
        "errores": errores,
        "pagos_registrados": pagos_registrados,
        "pagos_total": pagos_total,
        "pagos_rechazados": pagos_rechazados,
        "pagos_omitidos": pagos_omitidos,
        "requiere_confirmacion_pagadas": pagadas_sin_confirmar,
    }


def _procesar_reservas_bulk(rows: list) -> dict:
    """Bulk-insert fixed reservations from quick-entry rows; report per-row outcome."""
    require_collections()
    if reservas is None:
        raise RuntimeError("Colección de reservas no disponible.")
    usuario = (current_user() or {}).get("username", USUARIO_SISTEMA)

    vistos: set[int] = set()
    candidatas: list[tuple[int, str, str, str]] = []
    duplicadas: list[int] = []
    invalidas: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            bid = int(str(row.get("boleta", "")).strip())
        except (ValueError, TypeError):
            invalidas.append(str(row.get("boleta", "")))
            continue
        if not (BOLETA_MIN <= bid <= BOLETA_MAX):
            invalidas.append(str(bid))
            continue
        nombre = sanitizar_texto(row.get("nombre"), "name").upper()
        if not nombre:
            invalidas.append(f"#{bid:04d}: sin comprador")
            continue
        if bid in vistos:
            duplicadas.append(bid)
            continue
        vistos.add(bid)
        candidatas.append(
            (
                bid,
                nombre,
                sanitizar_texto(row.get("telefono"), "numbers"),
                sanitizar_texto(row.get("direccion"), "address").upper(),
            )
        )

    ids = [b for b, _, _, _ in candidatas]
    existentes = {d["_id"] for d in boletas.find({"_id": {"$in": ids}}, {"_id": 1})} if ids else set()
    ya = {d["_id"] for d in reservas.find({"_id": {"$in": ids}}, {"_id": 1})} if ids else set()

    docs = []
    no_existe: list[int] = []
    ya_reservadas: list[int] = []
    for bid, nombre, telefono, direccion in candidatas:
        if bid not in existentes:
            no_existe.append(bid)
        elif bid in ya:
            ya_reservadas.append(bid)
        else:
            docs.append(
                {
                    "_id": bid,
                    "cliente": {"nombre": nombre, "telefono": telefono, "direccion": direccion},
                    "creado_en": now_local(),
                    "creado_por": usuario,
                }
            )

    guardadas = 0
    if docs:
        try:
            resultado = reservas.insert_many(docs, ordered=False)
            guardadas = len(resultado.inserted_ids)
        except BulkWriteError as exc:
            # Solo posible por inserción concurrente de otro admin.
            guardadas = int((exc.details or {}).get("nInserted", 0) or 0)

    return {
        "ok": True,
        "guardadas": guardadas,
        "total": len(rows),
        "duplicadas": sorted(set(duplicadas)),
        "ya_reservadas": sorted(set(ya_reservadas)),
        "invalidas": invalidas,
        "no_existe": sorted(no_existe),
    }


def register_routes(app: Flask) -> None:
    """Register the quick buyer entry routes."""

    @app.route("/compradores/rapido", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def compradores_rapido() -> str | Response:
        """Quick-entry page for buyers: bulk update client data on tickets (JSON POST)."""
        require_collections()

        if request.method == "POST":
            try:
                data = request.get_json(force=True) or {}
            except BadRequest:
                return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
            rows = data.get("rows", [])
            if not isinstance(rows, list):
                return jsonify({"ok": False, "error": "Parámetros inválidos."}), 400
            if len(rows) > 500:
                return jsonify({"ok": False, "error": "Máximo 500 filas por operación."}), 400
            confirmar_pagadas = bool(data.get("confirmar_pagadas", False))

            try:
                return _procesar_rows_rapido(rows, confirmar_pagadas=confirmar_pagadas)
            except Exception as exc:
                return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

        config = get_config()
        return render_template("compradores_rapido.html", valor_boleta=config.get("valor_boleta", 0))

    @app.route("/api/compradores/validar", methods=["POST"])
    @role_required("admin", "cajero")
    def api_compradores_validar() -> Response:
        """Validate a list of tickets and report which already have client data."""
        try:
            data = request.get_json(force=True) or {}
        except BadRequest:
            return jsonify({"ok": False, "error": "JSON inv\u00e1lido."}), 400
        boletas_list = data.get("boletas", [])
        if not boletas_list:
            return jsonify({"ok": True, "resultados": {}})
        try:
            require_collections()
            int_ids = []
            for b in boletas_list:
                with contextlib.suppress(ValueError, TypeError):
                    int_ids.append(int(b))

            docs = {
                d["_id"]: d
                for d in boletas.find(
                    {"_id": {"$in": int_ids}},
                    {"_id": 1, "cliente": 1},
                )
            }
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

        resultados = {}
        for b in boletas_list:
            try:
                bid = int(b)
            except (ValueError, TypeError):
                resultados[str(b)] = {"existe": False}
                continue
            doc = docs.get(bid)
            if not doc:
                resultados[str(b)] = {"existe": False}
            else:
                cliente = doc.get("cliente") or {}
                tiene_cliente = bool(cliente.get("nombre", "").strip())
                resultados[str(b)] = {
                    "existe": True,
                    "tiene_cliente": tiene_cliente,
                    "cliente": {
                        "nombre": cliente.get("nombre", ""),
                        "telefono": cliente.get("telefono", ""),
                        "direccion": cliente.get("direccion", ""),
                    },
                }
        return jsonify({"ok": True, "resultados": resultados})

    @app.route("/compradores/reservas", methods=["GET", "POST"])
    @role_required("admin")
    def reservas_fijas() -> str | Response:
        """Manage fixed reservations (números fijos): survive new-rifa rollover as separadas."""
        require_collections()
        if reservas is None:
            flash("Colección de reservas no disponible.", "danger")
            return redirect(url_for("compradores_rapido"))

        if request.method == "POST":
            action = request.form.get("action", "").strip()
            if action == "agregar":
                boleta_raw = request.form.get("boleta", "").strip()
                nombre = sanitizar_texto(request.form.get("nombre"), "name").upper()
                telefono = sanitizar_texto(request.form.get("telefono"), "numbers")
                direccion = sanitizar_texto(request.form.get("direccion"), "address").upper()
                errors = []
                try:
                    bid = int(boleta_raw)
                except (ValueError, TypeError):
                    bid = None
                    errors.append("Número de boleta inválido.")
                if bid is not None and not (BOLETA_MIN <= bid <= BOLETA_MAX):
                    errors.append("El número debe estar entre 0000 y 9999.")
                if not nombre:
                    errors.append("El nombre del comprador es obligatorio.")
                if not errors and boletas.find_one({"_id": bid}, {"_id": 1}) is None:
                    errors.append(f"La boleta #{bid:04d} no existe.")
                if not errors and reservas.find_one({"_id": bid}, {"_id": 1}) is not None:
                    errors.append(f"La boleta #{bid:04d} ya está reservada.")
                if errors:
                    for error in errors:
                        flash(error, "danger")
                else:
                    user = current_user() or {}
                    try:
                        reservas.insert_one(
                            {
                                "_id": bid,
                                "cliente": {"nombre": nombre, "telefono": telefono, "direccion": direccion},
                                "creado_en": now_local(),
                                "creado_por": user.get("username", USUARIO_SISTEMA),
                            }
                        )
                        flash(f"Boleta #{bid:04d} reservada para {nombre}.", "success")
                    except Exception as exc:
                        current_app.logger.error("Error al crear reserva: %s", exc)
                        flash("Error al guardar la reserva. Intente de nuevo.", "danger")
            elif action == "eliminar":
                try:
                    bid = int(request.form.get("boleta", ""))
                except (ValueError, TypeError):
                    bid = None
                if bid is None or bid < BOLETA_MIN or bid > BOLETA_MAX:
                    flash("Número de boleta inválido.", "danger")
                else:
                    try:
                        if reservas.delete_one({"_id": bid}).deleted_count:
                            flash(f"Reserva de la boleta #{bid:04d} eliminada.", "success")
                        else:
                            flash("La reserva indicada no existe.", "warning")
                    except Exception as exc:
                        current_app.logger.error("Error al eliminar reserva: %s", exc)
                        flash("Error al eliminar la reserva. Intente de nuevo.", "danger")
            return redirect(url_for("reservas_fijas"))

        sort_by = request.args.get("sort_by", "_id").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        if sort_by not in {"_id", "comprador", "telefono", "direccion", "reservada"}:
            sort_by = "_id"
        sort_direction = 1 if sort_dir == "asc" else -1
        lista = list(reservas.find({}).sort(sort_by, sort_direction).limit(1000))
        return render_template("compradores_reservas.html", reservas=lista, sort_by=sort_by, sort_dir=sort_dir)

    @app.route("/api/compradores/reservas", methods=["POST"])
    @role_required("admin")
    def api_reservas_guardar() -> Response | tuple[Response, int]:
        """Bulk-save fixed reservations from quick-entry rows (JSON)."""
        try:
            data = request.get_json(force=True) or {}
        except BadRequest:
            return jsonify({"ok": False, "error": "JSON inválido."}), 400
        rows = data.get("rows", [])
        if not isinstance(rows, list):
            return jsonify({"ok": False, "error": "Parámetros inválidos."}), 400
        if len(rows) > 500:
            return jsonify({"ok": False, "error": "Máximo 500 filas por operación."}), 400
        try:
            require_collections()
            return jsonify(_procesar_reservas_bulk(rows))
        except Exception as exc:
            return jsonify({"ok": False, "error": safe_error_message(exc)}), 500

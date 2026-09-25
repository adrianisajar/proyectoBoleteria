import contextlib
import os
import re
import tempfile
import zipfile
from datetime import date
from typing import Any

from bson import ObjectId, json_util
from flask import Flask, Response

import database
from motores.config_service import get_rifa_activa, require_collections
from motores.constants import (
    BOLETA_MAX,
    BOLETA_MIN,
    ESTADOS_BOLETA,
    MOV_PAGO,
    MOV_TRASLADO_ENTRADA,
    MOV_TRASLADO_SALIDA,
    VENDEDOR_LOCAL,
)
from motores.excel_export import make_xlsx_response
from motores.shared import (
    boletas,
    configuracion,
    facturas,
    flash,
    get_dashboard_stats,
    home_endpoint,
    invalidate_config_cache,
    invalidate_dashboard_cache,
    invalidate_rifa_cache,
    modelo_rifa_report_rows,
    redirect,
    render_template,
    request,
    reservas,
    rifas,
    role_required,
    traslados,
    url_for,
    usuarios,
    vendedores,
)
from motores.usuarios import requiere_clave_admin
from motores.validacion import safe_error_message

TIPOS_FACTURA = {"cliente", "vendedor", "egreso"}
MAX_BACKUP_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _stream_zip_file(path: str):  # type: ignore[no-untyped-def]
    """Yield the ZIP in 64 KB chunks and delete the temp file afterwards."""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _es_numero(value: Any) -> bool:
    """Return True for real numbers (ints/floats, excluding bools)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validar_respaldo(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate a backup before any destructive restore.

    Returns (errores, advertencias). Errores abort the restore (nothing is
    written); advertencias are surfaced but do not block.
    """
    errores: list[str] = []
    advertencias: list[str] = []

    for nombre in ("boletas", "vendedores", "facturas", "configuracion"):
        if not isinstance(data.get(nombre), list):
            errores.append(f"{nombre}: el dato no es una lista")

    boletas_backup = [d for d in (data.get("boletas") or []) if isinstance(d, dict)]
    vendedores_backup = [d for d in (data.get("vendedores") or []) if isinstance(d, dict)]
    facturas_backup = [d for d in (data.get("facturas") or []) if isinstance(d, dict)]
    configuracion_backup = [d for d in (data.get("configuracion") or []) if isinstance(d, dict)]

    ids_boletas: set[int] = set()
    boletas_por_id: dict[int, dict[str, Any]] = {}
    for doc in boletas_backup:
        bid = doc.get("_id")
        if not isinstance(bid, int) or isinstance(bid, bool) or not (BOLETA_MIN <= bid <= BOLETA_MAX):
            errores.append(f"boletas: _id inválido {bid!r}")
            continue
        if bid in ids_boletas:
            errores.append(f"boletas: _id duplicado #{bid:04d}")
        ids_boletas.add(bid)
        boletas_por_id[bid] = doc

        total = doc.get("total_abonado", 0)
        if not _es_numero(total) or total < 0:
            errores.append(f"boletas #{bid:04d}: total_abonado inválido {total!r}")
        if "historial_movimientos" in doc and not isinstance(doc["historial_movimientos"], list):
            errores.append(f"boletas #{bid:04d}: historial_movimientos no es una lista")
        estado = doc.get("estado")
        if estado is not None and estado not in ESTADOS_BOLETA:
            errores.append(f"boletas #{bid:04d}: estado inválido {estado!r}")

    ids_vendedores: set[str] = set()
    for doc in vendedores_backup:
        vid = doc.get("_id")
        if not isinstance(vid, str) or not vid:
            errores.append(f"vendedores: _id inválido {vid!r}")
            continue
        if vid in ids_vendedores:
            errores.append(f"vendedores: _id duplicado {vid!r}")
        ids_vendedores.add(vid)

        asignadas = doc.get("boletas_asignadas", [])
        if not isinstance(asignadas, list):
            errores.append(f"vendedores {vid}: boletas_asignadas no es una lista")
            continue
        for numero in asignadas:
            if not isinstance(numero, int) or numero not in ids_boletas:
                errores.append(f"vendedores {vid}: boleta asignada #{numero:04d} no existe en el respaldo")

    for doc in vendedores_backup:
        vid = doc.get("_id")
        for numero in doc.get("boletas_asignadas") or []:
            if not isinstance(numero, int) or numero not in boletas_por_id:
                continue
            vendedor_boleta = boletas_por_id[numero].get("vendedor_id") or ""
            if vendedor_boleta and vendedor_boleta != VENDEDOR_LOCAL and vendedor_boleta != vid:
                errores.append(f"boletas #{numero:04d}: vendedor_id {vendedor_boleta!r} no coincide con la asignación de {vid}")

    for numero, doc in boletas_por_id.items():
        vendedor_boleta = doc.get("vendedor_id") or ""
        if vendedor_boleta not in ("", VENDEDOR_LOCAL) and vendedor_boleta not in ids_vendedores:
            errores.append(f"boletas #{numero:04d}: vendedor_id {vendedor_boleta!r} no existe en el respaldo")

    max_factura_id = -1
    for doc in facturas_backup:
        fid = doc.get("_id")
        if not isinstance(fid, int) or isinstance(fid, bool) or fid <= 0:
            errores.append(f"facturas: _id inválido {fid!r}")
            continue
        max_factura_id = max(max_factura_id, fid)
        tipo = doc.get("tipo")
        if tipo is not None and tipo not in TIPOS_FACTURA:
            errores.append(f"facturas #{fid}: tipo inválido {tipo!r}")
        if not _es_numero(doc.get("valor_total", 0)):
            errores.append(f"facturas #{fid}: valor_total inválido")

    config_rifa = next((d for d in configuracion_backup if d.get("_id") == "rifa"), None)
    if config_rifa is None:
        errores.append("configuracion: falta el documento 'rifa'")
    else:
        counter = config_rifa.get("factura_counter")
        if isinstance(counter, int) and max_factura_id > counter:
            errores.append(f"configuracion: factura_counter ({counter}) es menor que el id máximo de factura ({max_factura_id})")

    for numero, doc in boletas_por_id.items():
        movimientos = doc.get("historial_movimientos") or []
        if not isinstance(movimientos, list):
            continue
        neto = 0
        for mov in movimientos:
            if not isinstance(mov, dict):
                continue
            valor = mov.get("valor") or 0
            if not _es_numero(valor):
                continue
            tipo = mov.get("tipo") or MOV_PAGO
            if tipo in (MOV_PAGO, MOV_TRASLADO_ENTRADA):
                neto += int(valor)
            elif tipo == MOV_TRASLADO_SALIDA:
                neto -= int(valor)
        neto = max(0, neto)
        total = doc.get("total_abonado", 0)
        if _es_numero(total) and int(total) != neto:
            advertencias.append(f"boletas #{numero:04d}: total_abonado ({int(total)}) no coincide con el histórico neto ({neto})")

    return errores, advertencias


def _restore_objectids_from_backup(data: dict[str, list[dict[str, Any]]]) -> None:
    """Convert string _id / rifa_id to ObjectId for backward compat with old backups."""
    for name, docs in data.items():
        for doc in docs:
            if isinstance(doc.get("_id"), str) and len(doc["_id"]) == 24:
                with contextlib.suppress(Exception):
                    doc["_id"] = ObjectId(doc["_id"])
        if name == "boletas":
            for doc in docs:
                rifa_id = doc.get("rifa_id")
                if isinstance(rifa_id, str) and len(rifa_id) == 24:
                    with contextlib.suppress(Exception):
                        doc["rifa_id"] = ObjectId(rifa_id)


def register_routes(app: Flask) -> None:
    """Register the dashboard, search, export and backup routes."""

    @app.route("/")
    def home() -> Response:
        """Redirect root to the role-appropriate landing page."""
        return redirect(url_for(home_endpoint()))

    @app.route("/dashboard")
    @role_required("admin")
    def dashboard() -> str:
        """Render the dashboard with stats and active rifa info."""
        try:
            stats = get_dashboard_stats()
            rifa = get_rifa_activa()
        except Exception as exc:
            stats = {
                "recaudo_total": 0,
                "recaudo_hoy": 0,
                "pagos_hoy": 0,
                "pagos_efectivo": 0,
                "pagos_transferencia": 0,
                "total_pagos_delio": 0,
                "saldo_pendiente": 0,
                "vendidas": 0,
                "pagadas": 0,
                "disponibles": 0,
                "abonando": 0,
                "separadas": 0,
                "asignadas": 0,
                "progreso_ventas_pct": 0,
                "progreso_recaudo_pct": 0,
                "recaudo_potencial": 0,
                "recaudo_neto": 0,
                "total_egresos": 0,
                "ranking": [],
            }
            rifa = {}
            flash(safe_error_message(exc), "danger")
        return render_template("dashboard.html", stats=stats, rifa=rifa)

    @app.route("/buscar")
    @role_required("admin", "cajero")
    def buscar() -> str | Response:
        """Global search across invoices and vendors."""
        require_collections()
        q = request.args.get("q", "").strip()
        if not q:
            flash("Ingrese al menos 1 caracter para buscar.", "warning")
            return redirect(url_for(home_endpoint()))

        sort_by = request.args.get("sort_by", "_id").strip()
        sort_dir = request.args.get("sort_dir", "asc").strip()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        if sort_by not in {"_id", "fecha", "tipo", "cliente.nombre", "valor_total"}:
            sort_by = "_id"
        sort_direction = 1 if sort_dir == "asc" else -1

        results = {"facturas": [], "vendedores": []}

        regex = re.escape(q)
        try:
            num = int(q)
            factura = facturas.find_one({"_id": num})
            if factura:
                results["facturas"].append(factura)
        except ValueError:
            pass
        cursor = (
            facturas.find(
                {
                    "$or": [
                        {"vendedor_nombre": {"$regex": f"^{regex}", "$options": "i"}},
                        {"cliente.nombre": {"$regex": f"^{regex}", "$options": "i"}},
                    ]
                }
            )
            .sort(sort_by, sort_direction)
            .limit(20)
        )
        for f in cursor:
            results["facturas"].append(f)
        cursor = vendedores.find(
            {
                "$or": [
                    {"_id": {"$regex": f"^{regex}", "$options": "i"}},
                    {"nombre": {"$regex": f"^{regex}", "$options": "i"}},
                ]
            }
        ).limit(10)
        for v in cursor:
            results["vendedores"].append(v)

        return render_template("buscar.html", q=q, results=results, sort_by=sort_by, sort_dir=sort_dir)

    @app.route("/reportes/modelo-rifa.xlsx")
    @role_required("admin")
    def exportar_modelo_rifa() -> Response:
        """Download the modelo-rifa Excel report (global rifa data, admin only)."""
        try:
            headers, rows = modelo_rifa_report_rows()
        except Exception as exc:
            flash(safe_error_message(exc), "danger")
            return redirect(url_for(home_endpoint()))

        filename = f"modelo_rifa_{date.today().isoformat()}"
        return make_xlsx_response(filename, headers, rows)

    @app.route("/backup", methods=["GET", "POST"])
    @role_required("admin")
    def backup() -> str | Response:
        """Export/import a ZIP backup of all collections."""
        require_collections()
        COLECCIONES = [
            ("boletas", boletas),
            ("vendedores", vendedores),
            ("facturas", facturas),
            ("rifas", rifas),
            ("configuracion", configuracion),
            ("usuarios", usuarios),
            ("traslados", traslados),
            ("reservas", reservas),
        ]
        if request.method == "POST":
            accion = request.form.get("accion", "")
            if accion == "exportar":
                # ZIP en disco temporal: la respuesta se transmite en chunks de 64 KB
                # y cada documento se serializa por separado (pico de RAM ~1 doc).
                fd, tmp_path = tempfile.mkstemp(suffix=".zip")
                os.close(fd)
                try:
                    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for nombre, col in COLECCIONES:
                            if col is None:
                                continue
                            with zf.open(f"{nombre}.json", "w", force_zip64=True) as entry:
                                entry.write(b"[")
                                primero = True
                                for doc in col.find({}):
                                    if not primero:
                                        entry.write(b",")
                                    primero = False
                                    entry.write(json_util.dumps(doc, ensure_ascii=False).encode("utf-8"))
                                entry.write(b"]")
                except Exception:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)
                    raise
                size = os.path.getsize(tmp_path)
                return Response(
                    _stream_zip_file(tmp_path),
                    mimetype="application/zip",
                    headers={
                        "Content-Disposition": f"attachment; filename=backup_{date.today().isoformat()}.zip",
                        "Content-Length": str(size),
                    },
                )
            elif accion == "importar":
                if not requiere_clave_admin():
                    return redirect(url_for("backup"))
                archivo = request.files.get("archivo")
                if not archivo or not archivo.filename:
                    flash("Seleccione un archivo ZIP.", "danger")
                    return redirect(url_for("backup"))
                try:
                    data = {}
                    with zipfile.ZipFile(archivo.stream) as zf:
                        nombres = zf.namelist()
                        if "backup.json" in nombres:
                            # Formato antiguo: un solo archivo JSON
                            info = zf.getinfo("backup.json")
                            if info.file_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                                raise ValueError("El respaldo descomprimido supera el límite permitido de 64 MB.")
                            if info.compress_size and info.file_size / info.compress_size > 100:
                                raise ValueError("El respaldo tiene una relación de compresión no permitida.")
                            with zf.open(info) as f:
                                raw = f.read(MAX_BACKUP_UNCOMPRESSED_BYTES + 1)
                            if len(raw) > MAX_BACKUP_UNCOMPRESSED_BYTES:
                                raise ValueError("El respaldo descomprimido supera el límite permitido de 64 MB.")
                            data = json_util.loads(raw.decode("utf-8"))
                        else:
                            # Formato nuevo: un JSON por colección
                            for nombre in nombres:
                                if not nombre.endswith(".json"):
                                    continue
                                info = zf.getinfo(nombre)
                                if info.file_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                                    raise ValueError(f"{nombre}: supera el límite de 64 MB.")
                                with zf.open(info) as f:
                                    raw = f.read(MAX_BACKUP_UNCOMPRESSED_BYTES + 1)
                                if len(raw) > MAX_BACKUP_UNCOMPRESSED_BYTES:
                                    raise ValueError(f"{nombre}: supera el límite de 64 MB.")
                                data[nombre[:-5]] = json_util.loads(raw.decode("utf-8"))
                    # Backward compat: convert string ObjectId for old backups
                    _restore_objectids_from_backup(data)
                except Exception as exc:
                    flash(safe_error_message(exc), "danger")
                    return redirect(url_for("backup"))
                if not isinstance(data, dict):
                    flash("El archivo de respaldo no tiene el formato esperado.", "danger")
                    return redirect(url_for("backup"))
                required = {"boletas", "vendedores", "facturas", "configuracion"}
                missing = required - set(data.keys())
                if missing:
                    flash(f"El respaldo está incompleto, faltan colecciones: {', '.join(sorted(missing))}.", "danger")
                    return redirect(url_for("backup"))
                errores_validacion, advertencias_validacion = _validar_respaldo(data)
                if errores_validacion:
                    for error in errores_validacion:
                        flash(f"Respaldo inválido: {error}", "danger")
                    flash("La restauración fue cancelada: no se modificó la base de datos.", "danger")
                    return redirect(url_for("backup"))
                for advertencia in advertencias_validacion[:50]:
                    flash(f"Advertencia: {advertencia}", "warning")
                restaurados = {}
                errores = []
                colecciones_a_restaurar = []
                for nombre, col in COLECCIONES:
                    if col is None or nombre not in data:
                        continue
                    docs = data[nombre]
                    if not isinstance(docs, list):
                        errores.append(f"{nombre}: el dato no es una lista")
                        continue
                    colecciones_a_restaurar.append((nombre, col, docs))
                if errores:
                    for error in errores:
                        flash(f"Error al restaurar {error}.", "danger")
                    return redirect(url_for("backup"))
                client = getattr(database, "client", None)
                if client is None:
                    flash("No hay conexión a MongoDB para restaurar el respaldo.", "danger")
                    return redirect(url_for("backup"))
                try:

                    def restaurar_en_transaccion(session):
                        for _nombre, col, docs in colecciones_a_restaurar:
                            col.delete_many({}, session=session)
                            if docs:
                                col.insert_many(docs, ordered=True, session=session)

                    with client.start_session() as session:
                        session.with_transaction(restaurar_en_transaccion)
                    restaurados = {nombre: len(docs) for nombre, _col, docs in colecciones_a_restaurar}
                except Exception as exc:
                    flash(safe_error_message(exc), "danger")
                    return redirect(url_for("backup"))
                if restaurados:
                    invalidate_config_cache()
                    invalidate_rifa_cache()
                    invalidate_dashboard_cache()
                    total = sum(restaurados.values())
                    flash(f"Respaldo restaurado: {total} documentos en {len(restaurados)} colecciones.", "success")
                return redirect(url_for("backup"))

        return render_template("backup.html")

import contextlib
import io
import re
import zipfile
from datetime import date
from typing import Any

from bson import ObjectId, json_util
from flask import Flask, Response

from motores.config_service import get_rifa_activa, require_collections
from motores.excel_export import make_xlsx_response
from motores.shared import (
    boletas,
    configuracion,
    facturas,
    flash,
    get_dashboard_stats,
    invalidate_config_cache,
    invalidate_dashboard_cache,
    invalidate_rifa_cache,
    modelo_rifa_report_rows,
    redirect,
    render_template,
    request,
    rifas,
    role_required,
    url_for,
    usuarios,
    vendedores,
)


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
        """Redirect root to the dashboard."""
        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    @role_required("admin", "cajero", "consulta")
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
                "ranking": [],
            }
            rifa = {}
            flash(f"No se pudo cargar el dashboard: {exc}", "danger")
        return render_template("dashboard.html", stats=stats, rifa=rifa)

    @app.route("/buscar")
    @role_required("admin", "cajero", "consulta")
    def buscar() -> str | Response:
        """Global search across invoices and vendors."""
        require_collections()
        q = request.args.get("q", "").strip()
        if not q or len(q) < 1:
            flash("Ingrese al menos 1 caracter para buscar.", "warning")
            return redirect(url_for("dashboard"))

        results = {"facturas": [], "vendedores": []}

        regex = re.escape(q)
        try:
            num = int(q)
            factura = facturas.find_one({"_id": num})
            if factura:
                results["facturas"].append(factura)
        except ValueError:
            pass
        cursor = facturas.find(
            {
                "$or": [
                    {"vendedor_nombre": {"$regex": regex, "$options": "i"}},
                    {"cliente.nombre": {"$regex": regex, "$options": "i"}},
                ]
            }
        ).limit(20)
        for f in cursor:
            results["facturas"].append(f)
        cursor = vendedores.find(
            {
                "$or": [
                    {"_id": {"$regex": regex, "$options": "i"}},
                    {"nombre": {"$regex": regex, "$options": "i"}},
                ]
            }
        ).limit(10)
        for v in cursor:
            results["vendedores"].append(v)

        return render_template("buscar.html", q=q, results=results)

    @app.route("/reportes/modelo-rifa.xlsx")
    @role_required("admin", "cajero", "consulta")
    def exportar_modelo_rifa() -> Response:
        """Download the modelo-rifa Excel report."""
        try:
            headers, rows = modelo_rifa_report_rows()
        except Exception as exc:
            flash(f"No se pudo generar el modelo de rifa: {exc}", "danger")
            return redirect(url_for("dashboard"))

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
        ]
        if request.method == "POST":
            accion = request.form.get("accion", "")
            if accion == "exportar":
                data = {}
                for nombre, col in COLECCIONES:
                    if col is not None:
                        data[nombre] = list(col.find({}))
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("backup.json", json_util.dumps(data, ensure_ascii=False, indent=2))
                buf.seek(0)
                return Response(
                    buf.getvalue(),
                    mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=backup_{date.today().isoformat()}.zip"},
                )
            elif accion == "importar":
                archivo = request.files.get("archivo")
                if not archivo or not archivo.filename:
                    flash("Seleccione un archivo ZIP.", "danger")
                    return redirect(url_for("backup"))
                try:
                    with zipfile.ZipFile(archivo.stream) as zf, zf.open("backup.json") as f:
                        raw = f.read().decode("utf-8")
                    data = json_util.loads(raw)
                    # Backward compat: convert string ObjectId for old backups
                    _restore_objectids_from_backup(data)
                except Exception as exc:
                    flash(f"Error al leer el archivo de respaldo: {exc}", "danger")
                    return redirect(url_for("backup"))
                if not isinstance(data, dict):
                    flash("El archivo de respaldo no tiene el formato esperado.", "danger")
                    return redirect(url_for("backup"))
                required = {"boletas", "vendedores", "facturas", "configuracion"}
                missing = required - set(data.keys())
                if missing:
                    flash(f"El respaldo está incompleto, faltan colecciones: {', '.join(sorted(missing))}.", "danger")
                    return redirect(url_for("backup"))
                restaurados = {}
                errores = []
                for nombre, col in COLECCIONES:
                    if col is None or nombre not in data:
                        continue
                    docs = data[nombre]
                    if not isinstance(docs, list):
                        errores.append(f"{nombre}: el dato no es una lista")
                        continue
                    try:
                        col.delete_many({})
                        if docs:
                            col.insert_many(docs, ordered=False)
                        restaurados[nombre] = len(docs)
                    except Exception as exc:
                        errores.append(f"{nombre}: {exc}")
                        restaurados[nombre] = 0
                if restaurados:
                    invalidate_config_cache()
                    invalidate_rifa_cache()
                    invalidate_dashboard_cache()
                    total = sum(restaurados.values())
                    flash(f"Respaldo restaurado: {total} documentos en {len(restaurados)} colecciones.", "success")
                for error in errores:
                    flash(f"Error al restaurar {error}.", "danger")
                return redirect(url_for("backup"))

        return render_template("backup.html")

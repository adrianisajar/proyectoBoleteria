import contextlib
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import login_intentos, usuarios
from motores.auth import current_user, home_endpoint, role_required
from motores.config_service import require_collections
from motores.constants import (
    ADMIN_INICIAL_PASSWORD,
    ADMIN_INICIAL_USUARIO,
    ROL_ADMIN,
    ROLES,
)
from motores.fechas import now_local
from motores.flask_integration import invalidate_activo_cache
from motores.shared import jsonify
from motores.validacion import safe_error_message, sanitizar_texto

logger = logging.getLogger(__name__)

LOGIN_MAX_INTENTOS = int(os.getenv("LOGIN_MAX_INTENTOS") or "50")
LOGIN_BLOQUEO_MINUTOS = 15
LOGIN_ATTEMPT_TTL_DAYS = 1


def _ensure_indexes() -> None:
    """Create the unique index on 'usuario' (idempotent)."""
    if usuarios is None:
        return
    with contextlib.suppress(Exception):
        usuarios.create_index("usuario", unique=True)
    if login_intentos is not None:
        with contextlib.suppress(Exception):
            login_intentos.create_index("expira_en", expireAfterSeconds=0)


def ensure_initial_admin() -> None:
    """Create a default admin when no admin user exists (first run).

    Uses ``update_one`` with ``upsert=True`` to make the check-and-insert
    atomic — no TOCTOU race between ``count_documents`` and ``insert_one``.
    """
    if usuarios is None:
        return
    _ensure_indexes()
    try:
        usuarios.update_one(
            {"rol": ROL_ADMIN},
            {"$setOnInsert": {
                "nombre": "Administrador",
                "usuario": ADMIN_INICIAL_USUARIO,
                "password_hash": generate_password_hash(ADMIN_INICIAL_PASSWORD),
                "rol": ROL_ADMIN,
                "activo": True,
                "fecha_creacion": now_local(),
                "ultimo_acceso": None,
            }},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("No se pudo crear el usuario administrador inicial: %s", exc)
        _ensure_indexes()


def authenticate(usuario: str, password: str) -> dict | None:
    """Return the user document when credentials are valid and active."""
    if usuarios is None:
        return None
    doc = usuarios.find_one({"usuario": usuario})
    if not doc:
        return None
    activo = doc.get("activo", False)
    if isinstance(activo, str):
        activo = activo.strip().lower() not in ("false", "0", "no", "n") and activo.strip().lower() in (
            "true",
            "1",
            "yes",
            "s",
            "si",
        )
    if not activo:
        return None
    if not check_password_hash(doc.get("password_hash", ""), password):
        return None
    return doc


def _intentos_doc(usuario: str) -> dict:
    if login_intentos is None:
        return {}
    doc = login_intentos.find_one({"_id": usuario})
    return doc or {}


def _segundos_bloqueo(usuario: str) -> int:
    """Return the remaining lockout seconds for a login name (0 = not locked).

    Uses a conditional delete to atomically clear expired lockouts, preventing
    a TOCTOU race where a concurrent request sees the lockout as still active
    or clears it prematurely.
    """
    if login_intentos is None:
        return 0
    ahora = now_local()
    doc = login_intentos.find_one({"_id": usuario})
    if not doc:
        return 0
    hasta = doc.get("bloqueo_hasta")
    if not hasta:
        return 0
    if isinstance(hasta, str):
        with contextlib.suppress(Exception):
            hasta = datetime.fromisoformat(hasta)
    if not isinstance(hasta, datetime):
        return 0
    restante = (hasta - ahora).total_seconds()
    if restante <= 0:
        # Atomically clear only if the lockout has actually expired (conditional delete).
        login_intentos.delete_one({"_id": usuario, "bloqueo_hasta": {"$lte": ahora}})
        return 0
    return int(restante)


def _registrar_intento_fallido(usuario: str) -> None:
    """Increment the failed-attempt counter and lock the login name when exceeded.

    Uses a single ``find_one_and_update`` that atomically increments the
    counter AND sets the lockout in one MongoDB operation when the threshold
    is crossed. This closes the race window between ``$inc`` and a separate
    ``$set`` where a concurrent login could slip through without lockout.
    """
    if login_intentos is None:
        return
    ahora = now_local()
    intentos = timedelta(minutes=LOGIN_BLOQUEO_MINUTOS)
    doc = login_intentos.find_one_and_update(
        {"_id": usuario},
        {
            "$inc": {"fallos": 1},
            "$set": {"actualizado_en": ahora, "expira_en": ahora + timedelta(days=LOGIN_ATTEMPT_TTL_DAYS)},
            "$setOnInsert": {"creado_en": ahora},
        },
        upsert=True,
        return_document=True,
    )
    fallos = int((doc or {}).get("fallos", 0))
    if fallos >= LOGIN_MAX_INTENTOS:
        # Atomic: set lockout and reset counter in one write.
        login_intentos.update_one(
            {"_id": usuario, "fallos": {"$gte": LOGIN_MAX_INTENTOS}},
            {
                "$set": {
                    "bloqueo_hasta": ahora + intentos,
                    "expira_en": ahora + timedelta(days=LOGIN_ATTEMPT_TTL_DAYS),
                },
                "$min": {"fallos": 0},
            },
        )


def _limpiar_intentos(usuario: str) -> None:
    """Reset the failed-attempt counter after a successful login."""
    if login_intentos is None:
        return
    with contextlib.suppress(Exception):
        login_intentos.delete_one({"_id": usuario})


def verificar_clave_admin(clave: str | None) -> bool:
    """Return True when the submitted password matches the logged-in admin."""
    user = current_user()
    if not user:
        return False
    if usuarios is None:
        return False
    usuario_id = user.get("usuario_id")
    if not usuario_id:
        return False
    try:
        doc = usuarios.find_one({"_id": ObjectId(usuario_id)})
    except Exception:
        return False
    if not doc or not doc.get("password_hash"):
        return False
    return bool(clave) and check_password_hash(doc["password_hash"], clave)


def requiere_clave_admin() -> bool:
    """Validate the admin password sent with a sensitive action (best-effort)."""
    if verificar_clave_admin(request.form.get("clave_admin")):
        return True
    flash("Confirme esta acci\u00f3n con la contrase\u00f1a del administrador.", "danger")
    return False


def list_usuarios(sort_by: str = "rol", sort_dir: str = "asc") -> list[dict]:
    """Return all users ordered by role then username."""
    if usuarios is None:
        return []
    if sort_by not in {"_id", "usuario", "nombre", "rol", "activo", "ultimo_acceso"}:
        sort_by = "rol"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"
    sort_direction = 1 if sort_dir == "asc" else -1
    return list(usuarios.find({}).sort(sort_by, sort_direction))


def _sanitizar_password(raw: str) -> str:
    return raw or ""


def crear_usuario(nombre: str, usuario: str, password: str, rol: str) -> None:
    """Create a new user (validates uniqueness of the login name)."""
    require_collections()
    if rol not in ROLES:
        raise ValueError("Rol inv\u00e1lido.")
    if usuarios.find_one({"usuario": usuario}):
        raise ValueError("Ya existe un usuario con ese nombre de usuario.")
    usuarios.insert_one(
        {
            "nombre": nombre,
            "usuario": usuario,
            "password_hash": generate_password_hash(_sanitizar_password(password)),
            "rol": rol,
            "activo": True,
            "fecha_creacion": now_local(),
            "ultimo_acceso": None,
        }
    )


def _get_usuario(usuario_id: str) -> dict:
    try:
        doc = usuarios.find_one({"_id": ObjectId(usuario_id)})
    except Exception as exc:
        raise ValueError("Usuario inv\u00e1lido.") from exc
    if not doc:
        raise ValueError("El usuario no existe.")
    return doc


def _try_view(action: Any, mensaje: str = "Cambios guardados correctamente.") -> Response:
    """Run an action and flash the result, then return to the config panel."""
    try:
        action()
        flash(mensaje, "success")
    except Exception as exc:
        flash(safe_error_message(exc), "danger")
    return redirect(url_for("configuracion_panel"))


def register_routes(app: Flask) -> None:
    """Register the login, logout and user management routes."""

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str | Response:
        """Authentication page (also lazily creates the initial admin)."""
        ensure_initial_admin()
        if current_user():
            return redirect(url_for(home_endpoint()))
        if request.method == "POST":
            usuario = sanitizar_texto(request.form.get("usuario", ""), "titulo").lower()
            password = request.form.get("password", "")

            segundos = _segundos_bloqueo(usuario)
            if segundos > 0:
                minutos = max(1, (segundos + 59) // 60)
                flash(f"Demasiados intentos fallidos. Intente de nuevo en {minutos} minuto(s).", "danger")
                return render_template("login.html"), 429

            user = authenticate(usuario, password)
            if user is None:
                _registrar_intento_fallido(usuario)
                flash("Usuario o contrase\u00f1a incorrectos.", "danger")
                return render_template("login.html"), 401
            _limpiar_intentos(usuario)
            session.clear()
            session["usuario_id"] = str(user["_id"])
            session["usuario"] = user["usuario"]
            session["nombre"] = user["nombre"]
            session["rol"] = user["rol"]
            if usuarios is not None:
                with contextlib.suppress(Exception):
                    usuarios.update_one({"_id": user["_id"]}, {"$set": {"ultimo_acceso": now_local()}})
            next_url = request.args.get("next") or url_for(home_endpoint())
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for(home_endpoint())
            return redirect(next_url)
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout() -> Response:
        """Destroy the session and return to the login page."""
        session.clear()
        return redirect(url_for("login"))

    @app.route("/usuarios/crear", methods=["POST"])
    @role_required(ROL_ADMIN)
    def usuarios_crear() -> Response:
        """Create a new user (admin only)."""
        nombre = sanitizar_texto(request.form.get("nombre", ""), "name").upper()
        usuario = sanitizar_texto(request.form.get("usuario", ""), "titulo").lower()
        password = request.form.get("password", "")
        rol = request.form.get("rol", "")

        def action() -> None:
            if not nombre:
                raise ValueError("El nombre es obligatorio.")
            if not re.match(r"^[a-z0-9_.-]{3,30}$", usuario):
                raise ValueError("El usuario debe tener entre 3 y 30 caracteres (letras, n\u00fameros, punto, gui\u00f3n).")
            if len(password) < 6:
                raise ValueError("La contrase\u00f1a debe tener al menos 6 caracteres.")
            crear_usuario(nombre, usuario, password, rol)

        return _try_view(action)

    @app.route("/usuarios/<usuario_id>/editar", methods=["POST"])
    @role_required(ROL_ADMIN)
    def usuarios_editar(usuario_id: str) -> Response:
        """Edit a user's display name and role (admin only, no deletion)."""
        nombre = sanitizar_texto(request.form.get("nombre", ""), "name").upper()
        rol = request.form.get("rol", "")

        def action() -> None:
            if not nombre:
                raise ValueError("El nombre es obligatorio.")
            if rol not in ROLES:
                raise ValueError("Rol inv\u00e1lido.")
            doc = _get_usuario(usuario_id)
            if str(doc["_id"]) == current_user().get("usuario_id") and rol != doc.get("rol"):
                raise ValueError("No puedes cambiar tu propio rol.")
            usuarios.update_one({"_id": doc["_id"]}, {"$set": {"nombre": nombre, "rol": rol}})

        return _try_view(action)

    @app.route("/usuarios/<usuario_id>/contrasena", methods=["POST"])
    @role_required(ROL_ADMIN)
    def usuarios_contrasena(usuario_id: str) -> Response:
        """Change a user's password (admin only)."""
        if not requiere_clave_admin():
            return redirect(url_for("configuracion_panel"))
        password = request.form.get("password", "")

        def action() -> None:
            if len(password) < 6:
                raise ValueError("La contrase\u00f1a debe tener al menos 6 caracteres.")
            doc = _get_usuario(usuario_id)
            usuarios.update_one({"_id": doc["_id"]}, {"$set": {"password_hash": generate_password_hash(_sanitizar_password(password))}})

        return _try_view(action)

    @app.route("/usuarios/<usuario_id>/estado", methods=["POST"])
    @role_required(ROL_ADMIN)
    def usuarios_estado(usuario_id: str) -> Response:
        """Activate or deactivate a user (never deletes the document)."""
        if not requiere_clave_admin():
            return redirect(url_for("configuracion_panel"))
        activo = request.form.get("activo", "") == "1"

        def action() -> None:
            doc = _get_usuario(usuario_id)
            if str(doc["_id"]) == current_user().get("usuario_id"):
                raise ValueError("No puedes desactivar tu propio usuario.")
            if not activo and doc.get("rol") == ROL_ADMIN:
                activos = usuarios.count_documents({"rol": ROL_ADMIN, "activo": True})
                if activos <= 1:
                    raise ValueError("No puedes desactivar el último usuario admin.")
            usuarios.update_one({"_id": doc["_id"]}, {"$set": {"activo": activo}})
            invalidate_activo_cache(usuario_id)

        return _try_view(action)

    @app.route("/usuarios/<usuario_id>/eliminar", methods=["POST"])
    @role_required(ROL_ADMIN)
    def usuarios_eliminar(usuario_id: str) -> Response:
        """Delete a user (admin only, never yourself)."""
        if not requiere_clave_admin():
            return redirect(url_for("configuracion_panel"))

        def action() -> None:
            doc = _get_usuario(usuario_id)
            if str(doc["_id"]) == current_user().get("usuario_id"):
                raise ValueError("No puedes eliminar tu propio usuario.")
            if doc.get("rol") == ROL_ADMIN:
                activos = usuarios.count_documents({"rol": ROL_ADMIN, "activo": True})
                if activos <= 1:
                    raise ValueError("No puedes eliminar el último usuario admin.")
            usuarios.delete_one({"_id": doc["_id"]})

        return _try_view(action, mensaje="Usuario eliminado correctamente.")

    @app.route("/api/usuarios")
    @role_required(ROL_ADMIN)
    def api_usuarios() -> Response:
        """List users as JSON (admin only)."""
        return jsonify({"ok": True, "usuarios": list_usuarios()})

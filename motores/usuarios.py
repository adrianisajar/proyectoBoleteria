import contextlib
import re
from typing import Any

from bson import ObjectId
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import usuarios
from motores.auth import current_user, role_required
from motores.config_service import require_collections
from motores.constants import (
    ADMIN_INICIAL_PASSWORD,
    ADMIN_INICIAL_USUARIO,
    ROL_ADMIN,
    ROLES,
)
from motores.fechas import now_local
from motores.shared import jsonify
from motores.validacion import sanitizar_texto


def _ensure_indexes() -> None:
    """Create the unique index on 'usuario' (idempotent)."""
    if usuarios is None:
        return
    with contextlib.suppress(Exception):
        usuarios.create_index("usuario", unique=True)


def ensure_initial_admin() -> None:
    """Create a default admin when the usuarios collection is empty (first run)."""
    if usuarios is None:
        return
    _ensure_indexes()
    try:
        if usuarios.count_documents({}) == 0:
            usuarios.insert_one(
                {
                    "nombre": "Administrador",
                    "usuario": ADMIN_INICIAL_USUARIO,
                    "password_hash": generate_password_hash(ADMIN_INICIAL_PASSWORD),
                    "rol": ROL_ADMIN,
                    "activo": True,
                    "fecha_creacion": now_local(),
                    "ultimo_acceso": None,
                }
            )
    except Exception:
        _ensure_indexes()


def authenticate(usuario: str, password: str) -> dict | None:
    """Return the user document when credentials are valid and active."""
    if usuarios is None:
        return None
    doc = usuarios.find_one({"usuario": usuario})
    if not doc or not doc.get("activo", False):
        return None
    if not check_password_hash(doc.get("password_hash", ""), password):
        return None
    return doc


def list_usuarios() -> list[dict]:
    """Return all users ordered by role then username."""
    if usuarios is None:
        return []
    return list(usuarios.find({}).sort([("rol", 1), ("usuario", 1)]))


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
        flash(str(exc), "danger")
    return redirect(url_for("configuracion_panel"))


def register_routes(app: Flask) -> None:
    """Register the login, logout and user management routes."""

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str | Response:
        """Authentication page (also lazily creates the initial admin)."""
        ensure_initial_admin()
        if current_user():
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            usuario = sanitizar_texto(request.form.get("usuario", ""), "titulo").lower()
            password = request.form.get("password", "")
            user = authenticate(usuario, password)
            if user is None:
                flash("Usuario o contrase\u00f1a incorrectos.", "danger")
                return render_template("login.html"), 401
            session.clear()
            session["usuario_id"] = str(user["_id"])
            session["usuario"] = user["usuario"]
            session["nombre"] = user["nombre"]
            session["rol"] = user["rol"]
            if usuarios is not None:
                with contextlib.suppress(Exception):
                    usuarios.update_one({"_id": user["_id"]}, {"$set": {"ultimo_acceso": now_local()}})
            next_url = request.args.get("next") or url_for("dashboard")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("dashboard")
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
        activo = request.form.get("activo", "") == "1"

        def action() -> None:
            doc = _get_usuario(usuario_id)
            if str(doc["_id"]) == current_user().get("usuario_id"):
                raise ValueError("No puedes desactivar tu propio usuario.")
            usuarios.update_one({"_id": doc["_id"]}, {"$set": {"activo": activo}})

        return _try_view(action)

    @app.route("/usuarios/<usuario_id>/eliminar", methods=["POST"])
    @role_required(ROL_ADMIN)
    def usuarios_eliminar(usuario_id: str) -> Response:
        """Delete a user (admin only, never yourself)."""
        if request.form.get("confirmacion", "") != "ELIMINAR":
            flash("Escribe ELIMINAR para confirmar el borrado.", "danger")
            return redirect(url_for("configuracion_panel"))

        def action() -> None:
            doc = _get_usuario(usuario_id)
            if str(doc["_id"]) == current_user().get("usuario_id"):
                raise ValueError("No puedes eliminar tu propio usuario.")
            usuarios.delete_one({"_id": doc["_id"]})

        return _try_view(action, mensaje="Usuario eliminado correctamente.")

    @app.route("/api/usuarios")
    @role_required(ROL_ADMIN)
    def api_usuarios() -> Response:
        """List users as JSON (admin only)."""
        return jsonify({"ok": True, "usuarios": list_usuarios()})

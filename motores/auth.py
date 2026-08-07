from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import abort, current_app, flash, jsonify, redirect, request, session, url_for

from motores.constants import ROL_ADMIN, ROL_CAJA


def _es_solicitud_api() -> bool:
    """Return True when the request expects a JSON error (API path or JSON Accept)."""
    path = (request.path or "").lower()
    if path.startswith("/api/") or path == "/health":
        return True
    accept = (request.headers.get("Accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


def current_user() -> dict | None:
    """Return the authenticated user (from session) or None when not logged in."""
    try:
        usuario_id = session.get("usuario_id")
    except RuntimeError:
        return None
    if not usuario_id:
        return None
    usuario = session.get("usuario", usuario_id)
    return {
        "usuario_id": usuario_id,
        "usuario": usuario,
        "username": usuario,
        "nombre": session.get("nombre", usuario),
        "rol": session.get("rol", ""),
        "role": session.get("rol", ""),
    }


def has_role(*roles: str) -> bool:
    """Return True when the current user belongs to any of the given roles."""
    user = current_user()
    if not user:
        return False
    return (not roles) or user.get("rol") in roles


def home_endpoint() -> str:
    """Return the landing endpoint for the current role (admin → dashboard, caja → consultas)."""
    return "dashboard" if has_role(ROL_ADMIN) else "consultas"


def login_required(view_func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: require an active session, otherwise redirect to login."""

    @wraps(view_func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_user() is None:
            current_app.logger.warning(
                "Redirigiendo a login (login_required): ruta=%s cookie=%s session_keys=%s",
                request.path,
                bool(request.cookies.get(current_app.config.get("SESSION_COOKIE_NAME", "session"))),
                sorted(session.keys()),
            )
            if _es_solicitud_api():
                return jsonify({"ok": False, "error": "Sesi\u00f3n no activa."}), 401
            flash("Debes iniciar sesi\u00f3n para acceder.", "warning")
            return redirect(url_for("login", next=request.full_path))
        return view_func(*args, **kwargs)

    return wrapped


def role_required(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: require login and (when roles given) an allowed role."""

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            user = current_user()
            if user is None:
                current_app.logger.warning(
                    "Redirigiendo a login (role_required): ruta=%s cookie=%s session_keys=%s",
                    request.path,
                    bool(request.cookies.get(current_app.config.get("SESSION_COOKIE_NAME", "session"))),
                    sorted(session.keys()),
                )
                if _es_solicitud_api():
                    return jsonify({"ok": False, "error": "Sesi\u00f3n no activa."}), 401
                flash("Debes iniciar sesi\u00f3n para acceder.", "warning")
                return redirect(url_for("login", next=request.full_path))
            if roles and user.get("rol") not in roles:
                if _es_solicitud_api():
                    return jsonify({"ok": False, "error": "No tienes permisos para esta acci\u00f3n."}), 403
                abort(403)
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def roles_admin_caja() -> tuple[str, str]:
    """Return the two supported roles (convenience for route decorators)."""
    return ROL_ADMIN, ROL_CAJA

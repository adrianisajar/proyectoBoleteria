import logging
import time
from datetime import datetime
from typing import Any

from bson import ObjectId
from flask import Flask, current_app, flash, g, jsonify, redirect, request, session, url_for

from database import usuarios
from motores.auth import current_user, has_role
from motores.config_service import get_config
from motores.constants import SESSION_IDLE_TIMEOUT_SECONDS, VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL
from motores.dashboard_service import get_alertas
from motores.impresora import is_configured as printer_configured

logger = logging.getLogger(__name__)

# Cache corto para verificar si el usuario sigue activo (evita query en cada request)
_ACTIVO_CACHE: dict[str, tuple[float, bool]] = {}
_ACTIVO_CACHE_TTL = 10  # segundos


def register_template_filters(app: Flask) -> None:
    """Register the 'cop' and 'pct' Jinja2 filters."""

    @app.template_filter("cop")
    def format_cop(value: Any) -> str:
        """Jinja filter: format an int amount as COP (e.g. $70.000)."""
        try:
            amount = int(value or 0)
        except (TypeError, ValueError):
            amount = 0
        return f"${amount:,}".replace(",", ".")

    @app.template_filter("pct")
    def format_pct(value: Any) -> str:
        """Jinja filter: format a number as a trimmed percentage."""
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0
        return f"{number:.2f}".rstrip("0").rstrip(".")

    @app.template_filter("fecha")
    def format_fecha(value: Any, with_time: bool = False) -> str:
        """Jinja filter: format a date as dd/mm/aaaa (or dd/mm/aaaa HH:MM).

        Accepts datetime objects, date objects, or ISO strings
        (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS``).
        Returns ``"—"`` for empty/invalid values.
        """
        if not value:
            return "—"
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return value if value else "—"
        try:
            if with_time:
                return value.strftime("%d/%m/%Y %H:%M")
            return value.strftime("%d/%m/%Y")
        except (AttributeError, ValueError):
            return str(value)


def register_before_request(app: Flask) -> None:
    """Register the before_request hook that loads config, user and enforces idle timeout."""

    @app.before_request
    def load_user_context() -> Any:
        """Populate g.config and g.current_user, and close idle sessions."""
        # Saltar todo el trabajo de sesión/config para assets estáticos
        if (request.path or "").startswith("/static/"):
            return None
        g.config = get_config()
        g.current_user = current_user()

        if not g.current_user:
            return None

        session.permanent = True

        # Verificar que el usuario siga activo (inhabilitación inmediata, máx 10s de delay)
        usuario_id = session.get("usuario_id")
        if usuario_id and not _usuario_activo(usuario_id):
            current_app.logger.warning(
                "Sesión cerrada: usuario inhabilitado=%s ruta=%s",
                session.get("usuario"),
                request.path,
            )
            session.clear()
            path = (request.path or "").lower()
            if path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Usuario inhabilitado."}), 401
            flash("Tu cuenta ha sido inhabilitada.", "warning")
            return redirect(url_for("login"))

        now = time.time()
        last = session.get("_ultima_actividad")
        if last is not None and (now - last) > SESSION_IDLE_TIMEOUT_SECONDS:
            current_app.logger.warning(
                "Sesión cerrada por inactividad: usuario=%s ruta=%s inactivo=%.0fs",
                session.get("usuario"),
                request.path,
                now - last,
            )
            session.clear()
            path = (request.path or "").lower()
            if path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Sesi\u00f3n cerrada por inactividad."}), 401
            return redirect(url_for("login"))
        session["_ultima_actividad"] = now
        return None


def _usuario_activo(usuario_id: str) -> bool:
    """Return True if the user is active in the DB (cached for 10s)."""
    now = time.time()
    cached = _ACTIVO_CACHE.get(usuario_id)
    if cached and (now - cached[0]) < _ACTIVO_CACHE_TTL:
        return cached[1]
    try:
        if usuarios is None:
            return True
        doc = usuarios.find_one({"_id": ObjectId(usuario_id)}, {"activo": 1})
        activo = doc.get("activo", True) if doc else False
    except Exception:
        activo = True
    _ACTIVO_CACHE[usuario_id] = (now, activo)
    return activo


def invalidate_activo_cache(usuario_id: str | None = None) -> None:
    """Clear the active-user cache (all users or a specific one)."""
    if usuario_id is None:
        _ACTIVO_CACHE.clear()
    else:
        _ACTIVO_CACHE.pop(usuario_id, None)


def register_request_logging(app: Flask) -> None:
    """Register before/after request hooks for structured request logging."""

    @app.before_request
    def _start_timer() -> None:
        g._request_start = time.monotonic()

    @app.after_request
    def _log_request(response):  # type: ignore[no-untyped-def]
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://cdn.jsdelivr.net; img-src 'self' data:",
        )
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        start = getattr(g, "_request_start", None)
        if start is None:
            return response
        duration_ms = (time.monotonic() - start) * 1000
        status = response.status_code
        method = request.method
        path = request.path
        remote = request.headers.get("X-Real-IP", request.remote_addr or "-")
        user = session.get("usuario", "-")
        level = logging.WARNING if status >= 400 else logging.INFO
        logger.log(
            level,
            "%s %s %d %.1fms user=%s ip=%s",
            method,
            path,
            status,
            duration_ms,
            user,
            remote,
        )
        return response


def register_context_processor(app: Flask) -> None:
    """Register the context processor that exposes globals to all templates."""

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        """Expose app config, current user and helpers to all templates."""
        return {
            "app_config": getattr(g, "config", get_config()),
            "current_user": getattr(g, "current_user", current_user()),
            "can": has_role,
            "alertas": get_alertas,
            "VENDEDOR_LOCAL": VENDEDOR_LOCAL,
            "VENDEDOR_LOCAL_LABEL": VENDEDOR_LOCAL_LABEL,
            "printer_configured": printer_configured(),
        }

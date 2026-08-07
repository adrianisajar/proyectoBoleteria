import time
from typing import Any

from flask import Flask, current_app, g, jsonify, redirect, request, session, url_for

from motores.auth import current_user, has_role
from motores.config_service import get_config
from motores.constants import SESSION_IDLE_TIMEOUT_SECONDS, VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL
from motores.dashboard_service import get_alertas


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


def register_before_request(app: Flask) -> None:
    """Register the before_request hook that loads config, user and enforces idle timeout."""

    @app.before_request
    def load_user_context() -> Any:
        """Populate g.config and g.current_user, and close idle sessions."""
        g.config = get_config()
        g.current_user = current_user()

        if not g.current_user:
            return None

        session.permanent = True

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
        }

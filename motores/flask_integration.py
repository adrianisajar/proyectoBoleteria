from typing import Any

from flask import Flask, g

from motores.auth import current_user, has_role
from motores.config_service import get_config
from motores.constants import VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL
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
    """Register the before_request hook that loads config and current user."""

    @app.before_request
    def load_user_context() -> None:
        """Populate g.config and g.current_user on every request."""
        g.config = get_config()
        g.current_user = current_user()


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

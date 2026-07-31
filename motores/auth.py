from collections.abc import Callable
from functools import wraps
from typing import Any

from motores.constants import USUARIO_SISTEMA


def current_user() -> dict:
    """Return dummy admin user (no real auth system)."""
    return {"username": USUARIO_SISTEMA, "role": "admin", "nombre": "Sistema"}


def has_role(*roles: str) -> bool:
    """Always returns True (no auth enforcement)."""
    return True


def role_required(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that allows all routes (no auth)."""

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a view function so it can be registered with roles."""

        @wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            """Call the wrapped view function unchanged (no auth enforcement)."""
            return view_func(*args, **kwargs)

        return wrapped

    return decorator

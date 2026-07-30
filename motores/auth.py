from functools import wraps

from motores.constants import USUARIO_SISTEMA


def current_user() -> dict:
    return {"username": USUARIO_SISTEMA, "role": "admin", "nombre": "Sistema"}


def has_role(*roles: str) -> bool:
    return True


def role_required(*roles: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            return view_func(*args, **kwargs)
        return wrapped
    return decorator

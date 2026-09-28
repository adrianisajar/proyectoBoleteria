"""Transacciones de MongoDB con fallback para servidores standalone.

En Atlas/replica set las operaciones múltiples corren en una transacción real
(si algo falla a mitad, nada queda escrito). En servidores standalone (sin
soporte de transacciones) la función se ejecuta sin sesión, que es el
comportamiento histórico.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from database import client

T = TypeVar("T")

_TRANSACCIONES_CACHE: dict[str, bool] = {}


def soporta_transacciones() -> bool:
    """True si el servidor acepta transacciones (replica set/mongos, p. ej. Atlas).

    Se cachea por proceso: la detección es una consulta rara.
    """
    if "ok" not in _TRANSACCIONES_CACHE:
        ok = False
        if client is not None:
            try:
                hello = client.admin.command("hello")
                ok = bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"
            except Exception:
                ok = False
        _TRANSACCIONES_CACHE["ok"] = ok
    return _TRANSACCIONES_CACHE["ok"]


def con_transaccion(func: Callable[[Any], T]) -> T:
    """Run ``func(session)`` in a MongoDB transaction when the server supports it.

    ``func`` receives the session (or ``None`` on standalone) and may raise to
    abort the transaction; the exception propagates to the caller.
    """
    if client is not None and soporta_transacciones():
        with client.start_session() as sess:
            return sess.with_transaction(func)
    return func(None)

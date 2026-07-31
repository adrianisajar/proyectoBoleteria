import contextlib

from flask import Flask, Response, current_app, jsonify

from database import boletas, configuracion, facturas, rifas, vendedores
from motores.config_service import require_collections
from motores.constants import CONFIG_ID
from optimizar_db import REQUIRED_INDEXES

_COLLECTIONS = {
    "boletas": boletas,
    "vendedores": vendedores,
    "facturas": facturas,
    "rifas": rifas,
    "configuracion": configuracion,
}


def _missing_indexes() -> list[str]:
    """Return names of required indexes that are absent (read-only)."""
    missing = []
    for nombre_col, collection in _COLLECTIONS.items():
        if collection is None:
            continue
        required = REQUIRED_INDEXES.get(nombre_col, [])
        if not required:
            continue
        existing = set()
        with contextlib.suppress(Exception):
            existing = {frozenset(dict(doc["key"]).items()) for doc in collection.list_indexes()}
        for key_spec, expected_name in required:
            normalized = {key_spec: 1} if isinstance(key_spec, str) else dict(key_spec)
            if frozenset(normalized.items()) not in existing:
                missing.append(f"{nombre_col}.{expected_name}")
    return missing


def register_routes(app: Flask) -> None:
    """Register the /health liveness probe route."""

    @app.route("/health")
    def health() -> Response | tuple[Response, int]:
        """Health probe: collections, factura_counter, config doc and required indexes."""
        try:
            require_collections()
            counter = configuracion.find_one({"_id": CONFIG_ID}, {"factura_counter": 1})
            missing = _missing_indexes()
            indices_ok = not missing
            payload = {
                "status": "ok" if indices_ok else "degraded",
                "db": "connected",
                "factura_counter": (counter or {}).get("factura_counter", 0),
                "config_presente": counter is not None,
                "indices": {"ok": indices_ok, "faltantes": missing},
            }
            return jsonify(payload)
        except Exception as exc:
            current_app.logger.error("Health check fall\u00f3: %s: %s", type(exc).__name__, exc)
            return jsonify({"status": "error", "db": "no disponible"}), 503

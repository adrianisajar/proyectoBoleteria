from typing import Any

from flask import Flask, Response, current_app, jsonify, render_template, request


def safe_error_message(exc: Exception, default: str = "Error interno del servidor.") -> str:
    """Log the exception detail server-side and return a generic client-safe message."""
    current_app.logger.error("Error interno: %s: %s", type(exc).__name__, exc)
    return default


def _es_solicitud_api() -> bool:
    """Return True when the request expects a JSON error (API path or Accept header)."""
    path = (request.path or "").lower()
    if path.startswith("/api/") or path == "/health":
        return True
    accept = (request.headers.get("Accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


def _render_error(codigo: int, titulo: str, mensaje: str, exc: Exception | None = None) -> tuple[str | Response, int]:
    """Build an error response (HTML page or JSON for API requests)."""
    if exc is not None:
        current_app.logger.error(
            "Error %s en %s: %s: %s",
            codigo,
            request.path,
            type(exc).__name__,
            exc,
        )
    if _es_solicitud_api():
        return jsonify({"ok": False, "error": mensaje, "codigo": codigo}), codigo
    return render_template("error.html", codigo=codigo, titulo=titulo, mensaje=mensaje), codigo


def register_error_handlers(app: Flask) -> None:
    """Register consistent responses (HTML or JSON) for 400, 404, 413 and 500 errors."""

    @app.errorhandler(400)
    def bad_request(exc: Any) -> tuple[str | Response, int]:
        """Render the 400 error response (bad request / CSRF rejection)."""
        mensaje = getattr(exc, "description", None) or "Solicitud inv\u00e1lida."
        return _render_error(400, "Solicitud inv\u00e1lida", str(mensaje))

    @app.errorhandler(404)
    def not_found(exc: Any) -> tuple[str | Response, int]:
        """Render the 404 error response."""
        return _render_error(
            404,
            "Página no encontrada",
            "La página que buscas no existe o fue movida.",
        )

    @app.errorhandler(413)
    def payload_too_large(exc: Any) -> tuple[str | Response, int]:
        """Render the 413 error response (upload exceeds MAX_CONTENT_LENGTH)."""
        limite = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
        mensaje = f"El archivo supera el tamaño máximo permitido ({int(limite / 1024 / 1024)} MB)."
        return _render_error(413, "Archivo demasiado grande", mensaje)

    @app.errorhandler(500)
    def server_error(exc: Any) -> tuple[str | Response, int]:
        """Render the 500 error response (with exception logged)."""

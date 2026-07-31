import hmac
import secrets

from flask import Flask, abort, current_app, request, session

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_FIELD_NAME = "csrf_token"
_HEADER_NAME = "X-CSRF-Token"
_SESSION_KEY = "_csrf_token"


def generate_csrf_token() -> str:
    """Return the session CSRF token, creating it lazily on first use."""
    if _SESSION_KEY not in session:
        session[_SESSION_KEY] = secrets.token_urlsafe(32)
    return session[_SESSION_KEY]


def _validate_csrf() -> None:
    """Reject unsafe requests that lack a valid session CSRF token."""
    if request.method in _SAFE_METHODS:
        return
    if current_app.config.get("TESTING"):
        return
    expected = session.get(_SESSION_KEY)
    if not expected:
        abort(400, description="Falta el token CSRF.")
    submitted = request.form.get(_FIELD_NAME) or request.headers.get(_HEADER_NAME)
    if not submitted or not hmac.compare_digest(submitted, expected):
        abort(400, description="Token CSRF inv\u00e1lido o caducado. Recargue la p\u00e1gina.")


def register_csrf(app: Flask) -> None:
    """Register CSRF validation and expose the token generator to templates."""
    app.before_request(_validate_csrf)
    app.context_processor(lambda: {"csrf_token": generate_csrf_token})

import logging
import os
import sys
import threading
import webbrowser

from waitress import serve

# ── Custom compact logging ──────────────────────────────────────────
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
logging.getLogger("waitress").setLevel(logging.WARNING)
logging.getLogger("flask").setLevel(logging.WARNING)
# Suppress noisy Werkzeug request logs (our middleware handles logging)
wz = logging.getLogger("werkzeug")
wz.setLevel(logging.WARNING)
wz.disabled = True
# Enable request logging from our middleware
logging.getLogger("motores.flask_integration").setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


def _status(msg: str):
    print(f"  -> {msg}")


def _ok(msg: str):
    print(f"  [OK] {msg}")


def _fail(msg: str):
    print(f"  [FAIL] {msg}")


# ── ASCII-only output (safe on cp1252 consoles) ──
ARROW = "->"


# ─────────────────────────────────────────────────────────────────────

from app import app
from motores.shared import get_config, require_collections, sync_ticket_statuses


def abrir_navegador(host, port, delay=1.5):
    threading.Timer(delay, lambda: webbrowser.open(f"http://{host}:{port}")).start()


if __name__ == "__main__":
    try:
        _status("Verificando conexión a MongoDB...")
        require_collections()
        config = get_config(force=True)
        valor_boleta = int(config["valor_boleta"])
        sync_ticket_statuses(valor_boleta)
        _ok("Base de datos lista")
    except Exception as exc:
        _fail(f"Error al conectar: {exc}")
        _fail("No se puede iniciar el servidor sin conexión a MongoDB.")
        sys.exit(1)

    host = os.getenv("FLASK_HOST") or "127.0.0.1"
    port = int(os.getenv("PORT") or "5000")
    debug = os.getenv("FLASK_DEBUG") == "1"

    browser_host = "127.0.0.1" if host == "0.0.0.0" else host
    if os.getenv("OPEN_BROWSER") == "1":
        abrir_navegador(browser_host, port)
    _ok(f"Servidor iniciado {ARROW} http://{browser_host}:{port}")
    print()

    if debug:
        app.run(host=host, port=port, debug=True, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8, channel_timeout=30)

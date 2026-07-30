import logging
import os
import threading
import webbrowser
from waitress import serve

# ── Custom compact logging ──────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("waitress").setLevel(logging.WARNING)
logging.getLogger("flask").setLevel(logging.WARNING)
# Suppress HTTP request logs from Werkzeug
wz = logging.getLogger("werkzeug")
wz.setLevel(logging.WARNING)
wz.disabled = True


def _status(msg: str):
    print(f"  -> {msg}")


def _ok(msg: str):
    print(f"  ✓ {msg}")


def _fail(msg: str):
    print(f"  ✗ {msg}")
# ─────────────────────────────────────────────────────────────────────

from app import app
from motores.shared import get_config, sync_ticket_statuses, require_collections

try:
    _status("Verificando conexión a MongoDB...")
    require_collections()
    config = get_config(force=True)
    valor_boleta = int(config["valor_boleta"])
    sync_ticket_statuses(valor_boleta)
    _ok("Base de datos lista")
except Exception as exc:
    _fail(f"Error al conectar: {exc}")


def abrir_navegador(host, port, delay=1.5):
    threading.Timer(delay, lambda: webbrowser.open(f"http://{host}:{port}")).start()


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    browser_host = "127.0.0.1" if host == '0.0.0.0' else host
    abrir_navegador(browser_host, port)
    _ok(f"Servidor iniciado → http://{browser_host}:{port}")
    print()

    if debug:
        app.run(host=host, port=port, debug=True, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8, channel_timeout=30)

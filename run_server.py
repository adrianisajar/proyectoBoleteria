import os
import threading
import webbrowser
from waitress import serve

from app import app
from motores.shared import get_config, sync_ticket_statuses, require_collections

try:
    require_collections()
    config = get_config(force=True)
    valor_boleta = int(config["valor_boleta"])
    sync_ticket_statuses(valor_boleta)
except Exception as exc:
    print(f"[startup] sync_ticket_statuses skipped: {exc}")


def abrir_navegador(host, port, delay=1.5):
    threading.Timer(delay, lambda: webbrowser.open(f"http://{host}:{port}")).start()


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    browser_host = "127.0.0.1" if host == '0.0.0.0' else host
    abrir_navegador(browser_host, port)
    print(f"Servidor iniciado en http://{browser_host}:{port}")

    if debug:
        app.run(host=host, port=port, debug=True, threaded=True)
    else:
        serve(app, host=host, port=port, threads=8, channel_timeout=30)

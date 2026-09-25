import logging
import os
import sys
from datetime import timedelta

from dotenv import load_dotenv

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    dotenv_path = os.path.join(os.path.dirname(sys.executable), ".env")
    if not load_dotenv(dotenv_path):
        load_dotenv()
else:
    load_dotenv()

from flask import Flask
from flask_session import Session

from database import client
from motores.boletas import register_routes as register_boletas
from motores.compradores import register_routes as register_compradores
from motores.csrf import register_csrf
from motores.egresos import register_routes as register_egresos
from motores.errores import register_error_handlers
from motores.facturacion import register_routes as register_facturacion
from motores.facturacion_cliente import register_routes as register_facturacion_cliente
from motores.facturacion_vendedor import register_routes as register_facturacion_vendedor
from motores.health import register_routes as register_health
from motores.pagos import register_routes as register_pagos
from motores.reportes import register_routes as register_reportes
from motores.rifas import register_routes as register_rifas
from motores.shared import register_before_request, register_context_processor, register_request_logging, register_template_filters
from motores.traslados import register_routes as register_traslados
from motores.usuarios import ensure_initial_admin
from motores.usuarios import register_routes as register_usuarios

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    app = Flask(__name__, template_folder=os.path.join(sys._MEIPASS, "templates"), static_folder=os.path.join(sys._MEIPASS, "static"))
else:
    app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY no está definida. Agrega 'SECRET_KEY=...' al archivo .env antes de iniciar la aplicación.")
if os.getenv("TESTING") == "1":
    logging.getLogger(__name__).warning("TESTING=True en .env se ignora en producción. Solo se aplica en tests.")
if app.debug:
    app.jinja_env.auto_reload = True

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE") or "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(os.getenv("SESSION_COOKIE_DAYS") or "7"))
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_MB") or "16") * 1024 * 1024

# Sesión compartida en MongoDB (funciona en ambas instancias, cualquier dominio)
app.config["SESSION_TYPE"] = "mongodb"
app.config["SESSION_MONGODB"] = client
app.config["SESSION_MONGODB_DB"] = os.getenv("MONGO_DB") or "sistema_boleteria"
app.config["SESSION_MONGODB_COLLECT"] = "sessions"
app.config["SESSION_PERMANENT"] = True
try:
    Session(app)
except Exception as exc:
    logging.getLogger(__name__).critical(
        "No se pudo inicializar Flask-Session con MongoDB: %s. Verifica que MONGO_URI sea correcta y Atlas esté accesible.", exc
    )
    raise SystemExit(1) from exc

# Índice único en sessions.id — evita full-scan en cada lectura de sesión
try:
    client[app.config["SESSION_MONGODB_DB"]][app.config["SESSION_MONGODB_COLLECT"]].create_index("id", unique=True, name="sessions_id_1")
except Exception:
    logging.getLogger(__name__).warning("No se pudo crear índice sessions.id (se creará en el próximo arranque).")

if os.getenv("TRUST_PROXY_HEADERS") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

register_template_filters(app)
register_before_request(app)
register_request_logging(app)
register_context_processor(app)
register_csrf(app)

register_rifas(app)
register_boletas(app)
register_pagos(app)
register_facturacion(app)
register_facturacion_cliente(app)
register_facturacion_vendedor(app)
register_reportes(app)
register_compradores(app)
register_egresos(app)
register_traslados(app)
register_usuarios(app)
register_health(app)
register_error_handlers(app)

try:
    ensure_initial_admin()
except Exception as exc:
    import logging

    logging.getLogger(__name__).warning("ensure_initial_admin: %s", exc)

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")

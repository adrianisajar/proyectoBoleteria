import contextlib
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
from motores.shared import register_before_request, register_context_processor, register_template_filters
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
if app.debug:
    app.jinja_env.auto_reload = True

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(os.getenv("SESSION_COOKIE_DAYS", "7")))
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_MB", "16")) * 1024 * 1024

register_template_filters(app)
register_before_request(app)
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

with contextlib.suppress(Exception):
    ensure_initial_admin()

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")

import os
import sys

from flask import Flask

from motores.boletas import register_routes as register_boletas
from motores.compradores import register_routes as register_compradores
from motores.errores import register_error_handlers
from motores.facturacion import register_routes as register_facturacion
from motores.facturacion_cliente import register_routes as register_facturacion_cliente
from motores.facturacion_vendedor import register_routes as register_facturacion_vendedor
from motores.health import register_routes as register_health
from motores.pagos import register_routes as register_pagos
from motores.reportes import register_routes as register_reportes
from motores.rifas import register_routes as register_rifas
from motores.shared import register_before_request, register_context_processor, register_template_filters

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    app = Flask(__name__, template_folder=os.path.join(sys._MEIPASS, "templates"), static_folder=os.path.join(sys._MEIPASS, "static"))
else:
    app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY no está definida. Agrega 'SECRET_KEY=...' al archivo .env antes de iniciar la aplicación.")
if app.debug:
    app.jinja_env.auto_reload = True

register_template_filters(app)
register_before_request(app)
register_context_processor(app)

register_rifas(app)
register_boletas(app)
register_pagos(app)
register_facturacion(app)
register_facturacion_cliente(app)
register_facturacion_vendedor(app)
register_reportes(app)
register_compradores(app)
register_health(app)
register_error_handlers(app)

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")

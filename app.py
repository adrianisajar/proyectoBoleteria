import os
import sys

from flask import Flask

from motores.shared import register_template_filters, register_before_request, register_context_processor
from motores.rifas import register_routes as register_rifas
from motores.boletas import register_routes as register_boletas
from motores.pagos import register_routes as register_pagos
from motores.facturacion import register_routes as register_facturacion
from motores.facturacion_cliente import register_routes as register_facturacion_cliente
from motores.facturacion_vendedor import register_routes as register_facturacion_vendedor
from motores.reportes import register_routes as register_reportes


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    app = Flask(__name__, template_folder=os.path.join(sys._MEIPASS, "templates"), static_folder=os.path.join(sys._MEIPASS, "static"))
else:
    app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave_desarrollo_boleteria")
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

if __name__ == "__main__":
    app.run(debug=True)

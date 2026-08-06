import os
from datetime import date

# ── IDs ──
CONFIG_ID = "rifa"

# ── Vendor ──
VENDEDOR_LOCAL = "LOCAL"
VENDEDOR_LOCAL_LABEL = "Local"
VENDEDOR_SIN_ASIGNAR = ""

# ── Payment ──
METODO_EFECTIVO = "efectivo"
METODO_TRANSFERENCIA = "transferencia"
METODOS_PAGO = {METODO_EFECTIVO, METODO_TRANSFERENCIA}
REFERENCIA_N_A = "N/A"

# ── User ──
USUARIO_SISTEMA = "sistema"
ROL_ADMIN = "admin"
ROL_CAJA = "cajero"
ROLES = {ROL_ADMIN, ROL_CAJA}
SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "1800"))
ADMIN_INICIAL_USUARIO = os.getenv("ADMIN_INICIAL_USUARIO", "admin")
ADMIN_INICIAL_PASSWORD = os.getenv("ADMIN_INICIAL_PASSWORD", "admin")

# ── Ticket states ──
ESTADO_DISPONIBLE = "disponible"
ESTADO_ASIGNADA = "asignada"
ESTADO_SEPARADA = "separada"
ESTADO_ABONANDO = "abonando"
ESTADO_PAGADA = "pagada"
ESTADOS_BOLETA = {ESTADO_DISPONIBLE, ESTADO_ASIGNADA, ESTADO_SEPARADA, ESTADO_ABONANDO, ESTADO_PAGADA}

# ── Ticket numbers ──
BOLETA_MIN = 0
BOLETA_MAX = 9999

# ── Vendor operations ──
OPERACIONES_VENDEDOR = {"guardar", "asignar", "quitar", "eliminar", "registrar_fecha_adquisicion"}

# ── Query limits ──
CONSULTA_LIMIT_DEFAULT = 50
CONSULTA_LIMIT_MAX = 200

# ── Commissions ──
COMISION_DEFAULT_TIERS = [
    {"min": 0, "valor": 0},
    {"min": 10, "valor": 10000},
    {"min": 21, "valor": 15000},
    {"min": 51, "valor": 20000},
]

# ── Default config ──
DEFAULT_CONFIG = {
    "_id": CONFIG_ID,
    "nombre_rifa": os.getenv("NOMBRE_RIFA", "Asociacion De Vendedores Rifas Transparencia"),
    "valor_boleta": int(os.getenv("VALOR_BOLETA", "70000")),
    "cantidad_boletas": 10000,
    "premio_mayor": "",
    "estado": "activa",
    "nombre_empresa": "",
    "direccion": "",
    "telefono": "",
    "ciudad": "",
    "footer_texto": "Documento interno, no tiene validez fiscal.",
    "observaciones_recaudo": "Todos los pagos fueron registrados correctamente.\nLas boletas actualizan autom\u00e1ticamente su saldo en el sistema.",
    "comisiones_tiers": COMISION_DEFAULT_TIERS,
}

# ── Default rifa ──
DEFAULT_RIFA = {
    "nombre": os.getenv("NOMBRE_RIFA", "Asociacion De Vendedores Rifas Transparencia"),
    "anio": date.today().year,
    "valor_boleta": int(os.getenv("VALOR_BOLETA", "70000")),
    "cantidad_boletas": 10000,
    "premio_mayor": "",
    "comisiones_tiers": COMISION_DEFAULT_TIERS,
    "estado": "activa",
    "creada_en": None,
}

# ── Excel ──
MODELO_RIFA_HEADERS = [
    "NUMERO DE BOLETA ",
    "TOTAL ABONO ",
    "FECHA ADQUISICION",
    "VENDEDOR (A)",
    "COMPRADOR(A)",
    "DIRECCION ",
    "TELEFONO ",
    "FECHA ",
    "FACT",
    "ABONO 1",
    "FECHA",
    "FACT",
    "ABONO 2",
    "FECHA",
    "FACT",
    "ABONO 3",
    "FECHA",
    "FACT",
    "ABONO 4",
    "FECHA",
    "FACT",
    "ABONO 5",
    "FECHA",
    "FACT",
    "ABONO 6",
    "FECHA ",
    "FACT",
    "ABONO 7",
    "TOTAL ABONOS EFECTIVO",
    "VS",
    "FECHA",
    "FACT",
    "TFR 1",
    "FECHA",
    "FACT",
    "TFR 2",
    "FECHA",
    "FACT",
    "TFR 3",
    "FECHA",
    "FACT",
    "TFR 4",
    "FECHA",
    "FACT",
    "TFR 5",
    "PAGOS TOTAL TFR",
    "TOTAL ABONADO ",
]

XLSX_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_REL_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}

import os
from datetime import datetime

from database import boletas, configuracion, facturas, rifas, vendedores
from motores.fechas import now_local

CONFIG_ID = "rifa"

COMISION_DEFAULT_TIERS = [
    {"min": 0, "valor": 0},
    {"min": 10, "valor": 10000},
    {"min": 21, "valor": 15000},
    {"min": 51, "valor": 20000},
]


def crear_indices():
    boletas.create_index([("rifa_id", 1), ("_id", 1)])
    boletas.create_index([("vendedor_id", 1), ("_id", 1)])
    boletas.create_index([("estado", 1), ("_id", 1)])
    boletas.create_index([("total_abonado", 1), ("_id", 1)])
    boletas.create_index([("vendedor_id", 1), ("estado", 1)])
    boletas.create_index([("historial_pagos.fecha", 1)])
    boletas.create_index("cliente.telefono")
    boletas.create_index("cliente.nombre")
    boletas.create_index("historial_pagos.metodo")
    boletas.create_index("historial_pagos.referencia")
    vendedores.create_index("telefono")
    facturas.create_index([("fecha", -1)])
    facturas.create_index("tipo")
    rifas.create_index("estado")


def crear_rifa():
    rifa = {
        "nombre": os.getenv("NOMBRE_RIFA", "Rifa Principal"),
        "anio": now_local().year,
        "valor_boleta": int(os.getenv("VALOR_BOLETA", "100000")),
        "cantidad_boletas": 10000,
        "premio_mayor": "",
        "comisiones_tiers": COMISION_DEFAULT_TIERS,
        "estado": "activa",
        "creada_en": now_local(),
    }
    result = rifas.update_one({"estado": "activa"}, {"$setOnInsert": rifa}, upsert=True)
    if result.upserted_id:
        rifa["_id"] = result.upserted_id
    else:
        rifa = rifas.find_one({"estado": "activa"})
    return rifa


def crear_configuracion_base():
    configuracion.update_one(
        {"_id": CONFIG_ID},
        {"$setOnInsert": {"factura_counter": 0}},
        upsert=True,
    )


def crear_boleta(numero, rifa_id):
    return {
        "_id": numero,
        "rifa_id": rifa_id,
        "vendedor_id": "",
        "cliente": {"nombre": "", "telefono": "", "direccion": ""},
        "estado": "disponible",
        "total_abonado": 0,
        "historial_pagos": [],
    }


def inicializar_rifa():
    if boletas is None:
        raise RuntimeError("No hay conexión activa a MongoDB.")

    print("Preparando la colección boletas...")
    boletas.delete_many({})
    vendedores.delete_many({})
    facturas.delete_many({})
    rifas.delete_many({})
    configuracion.delete_many({})

    rifa = crear_rifa()
    rifa_id = rifa["_id"]

    print(f"Generando 10,000 boletas para '{rifa['nombre']}'...")
    documentos = [crear_boleta(numero, rifa_id) for numero in range(10000)]
    boletas.insert_many(documentos)
    crear_configuracion_base()
    crear_indices()

    total = boletas.count_documents({})
    print(f"Base de datos inicializada con {total} boletas en estado 'disponible'.")


if __name__ == "__main__":
    inicializar_rifa()

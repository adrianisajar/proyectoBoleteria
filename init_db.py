import os

from database import auditoria, boletas, configuracion, vendedores

CONFIG_ID = "rifa"


def crear_indices():
    boletas.create_index([("vendedor_id", 1), ("_id", 1)])
    boletas.create_index([("estado", 1), ("_id", 1)])
    boletas.create_index([("total_abonado", 1), ("_id", 1)])
    boletas.create_index([("vendedor_id", 1), ("estado", 1)])
    boletas.create_index([("historial_pagos.fecha", 1)])
    boletas.create_index("cliente.telefono")
    boletas.create_index("cliente.nombre")
    boletas.create_index("historial_pagos.metodo")
    boletas.create_index("historial_pagos.facturero")
    boletas.create_index("historial_pagos.referencia")
    vendedores.create_index("telefono")
    auditoria.create_index([("fecha", -1)])
    auditoria.create_index("accion")


def crear_configuracion_base():
    comision = int(os.getenv("COMISION_POR_BOLETA", os.getenv("COMISION_PORCENTAJE", "10000")))
    configuracion.update_one(
        {"_id": CONFIG_ID},
        {
            "$setOnInsert": {
                "_id": CONFIG_ID,
                "nombre_rifa": os.getenv("NOMBRE_RIFA", "Rifa Principal"),
                "valor_boleta": int(os.getenv("VALOR_BOLETA", "100000")),
                "comision_por_boleta": comision,
            }
        },
        upsert=True,
    )
    configuracion.update_one(
        {"_id": CONFIG_ID, "comision_por_boleta": {"$exists": False}},
        {"$set": {"comision_por_boleta": comision}},
    )


def crear_boleta(numero):
    return {
        "_id": numero,
        "vendedor_id": "LOCAL",
        "cliente": {
            "nombre": "",
            "telefono": "",
            "direccion": ""
        },
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
    auditoria.delete_many({})

    print("Generando 10,000 boletas (0000 al 9999)...")
    documentos = [crear_boleta(numero) for numero in range(10000)]
    boletas.insert_many(documentos)
    crear_configuracion_base()
    crear_indices()

    total = boletas.count_documents({})
    print(f"Base de datos inicializada con {total} boletas en estado 'disponible'.")

if __name__ == "__main__":
    inicializar_rifa()

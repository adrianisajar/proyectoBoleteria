import os

from database import boletas, configuracion, facturas, login_intentos, reservas, rifas, traslados, usuarios, vendedores
from motores.constants import COMISION_DEFAULT_TIERS, DEFAULT_RIFA
from motores.fechas import now_local
from optimizar_db import REQUIRED_INDEXES

CONFIG_ID = "rifa"


def crear_indices():
    for nombre_col, specs in REQUIRED_INDEXES.items():
        collection = {"boletas": boletas, "vendedores": vendedores, "facturas": facturas, "rifas": rifas, "traslados": traslados, "usuarios": usuarios}.get(
            nombre_col
        )
        if collection is None:
            continue
        for entry in specs:
            key_spec, name = entry[0], entry[1]
            options = dict(entry[2]) if len(entry) > 2 else {}
            if nombre_col == "usuarios" and key_spec == {"usuario": 1}:
                options["unique"] = True
            options.setdefault("name", name)
            if isinstance(key_spec, dict):
                collection.create_index(list(key_spec.items()), **options)
            else:
                collection.create_index(key_spec, **options)


def crear_rifa():
    rifa = {
        "nombre": os.getenv("NOMBRE_RIFA", DEFAULT_RIFA["nombre"]),
        "anio": now_local().year,
        "valor_boleta": int(os.getenv("VALOR_BOLETA", str(DEFAULT_RIFA["valor_boleta"]))),
        "cantidad_boletas": DEFAULT_RIFA["cantidad_boletas"],
        "premio_mayor": DEFAULT_RIFA["premio_mayor"],
        "comisiones_tiers": COMISION_DEFAULT_TIERS,
        "estado": DEFAULT_RIFA["estado"],
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
        {"$setOnInsert": {"factura_counter": 0, "traslado_counter": 0, "vendedor_counter": 0}},
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
        "historial_movimientos": [],
        "fecha_adquisicion": None,
    }


def inicializar_rifa():
    if boletas is None:
        raise RuntimeError("No hay conexión activa a MongoDB.")

    print("Preparando la colección boletas...")
    respuesta = input("Esto ELIMINARÁ todos los datos. Continuar? (s/n): ").strip().lower()
    if respuesta != "s":
        print("Operación cancelada.")
        return
    boletas.delete_many({})
    vendedores.delete_many({})
    facturas.delete_many({})
    rifas.delete_many({})
    configuracion.delete_many({})
    traslados.delete_many({})
    reservas.delete_many({})
    login_intentos.delete_many({})

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

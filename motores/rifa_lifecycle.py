from database import boletas, configuracion, facturas, rifas, traslados, vendedores
from motores.cache import invalidate_config_cache, invalidate_dashboard_cache
from motores.config_service import require_collections
from motores.constants import BOLETA_MAX, BOLETA_MIN, COMISION_DEFAULT_TIERS, CONFIG_ID
from motores.fechas import now_local
from motores.modelos import crear_boleta_base


def crear_indices_boletas() -> None:
    """Create all required indexes across the collections (safe to re-run)."""
    boletas.create_index([("vendedor_id", 1), ("_id", 1)])
    boletas.create_index([("vendedor_id", 1), ("estado", 1)])
    boletas.create_index([("estado", 1), ("_id", 1)])
    boletas.create_index([("total_abonado", 1), ("_id", 1)])
    boletas.create_index([("historial_movimientos.fecha", 1)])
    boletas.create_index("cliente.telefono")
    boletas.create_index("cliente.nombre")
    boletas.create_index("historial_movimientos.metodo")
    boletas.create_index("historial_movimientos.referencia")
    boletas.create_index("historial_movimientos.tipo")
    vendedores.create_index("telefono")
    facturas.create_index([("fecha", -1)])
    facturas.create_index("tipo")
    rifas.create_index("estado")
    traslados.create_index([("fecha", -1)])
    traslados.create_index("boleta_origen")
    traslados.create_index("boleta_destino")


def crear_nueva_rifa(
    nombre: str,
    valor_boleta: int,
    conservar_vendedores: bool,
    cantidad_boletas: int = 10000,
    premio_mayor: str = "",
    estado: str = "activa",
) -> None:
    """Reset all collections for a new rifa (optionally keeping vendor profiles)."""
    require_collections()
    asignaciones = []
    if conservar_vendedores:
        asignaciones = list(vendedores.find({}, {"boletas_asignadas": 1}))

    facturas.delete_many({})
    traslados.delete_many({})
    configuracion.update_one({"_id": CONFIG_ID}, {"$set": {"factura_counter": 0, "traslado_counter": 0}})

    boletas.delete_many({})
    rifas.delete_many({})
    rifa_doc = {
        "nombre": nombre,
        "anio": now_local().year,
        "valor_boleta": valor_boleta,
        "cantidad_boletas": cantidad_boletas,
        "premio_mayor": premio_mayor,
        "comisiones_tiers": COMISION_DEFAULT_TIERS,
        "estado": estado,
        "creada_en": now_local(),
    }
    resultado = rifas.insert_one(rifa_doc)
    nueva_rifa_id = resultado.inserted_id

    boletas.insert_many([crear_boleta_base(numero, nueva_rifa_id) for numero in range(BOLETA_MIN, BOLETA_MAX + 1)])

    if conservar_vendedores:
        for vendedor in asignaciones:
            ids = [number for number in vendedor.get("boletas_asignadas", []) if isinstance(number, int) and BOLETA_MIN <= number <= BOLETA_MAX]
            if ids:
                boletas.update_many({"_id": {"$in": ids}}, {"$set": {"vendedor_id": vendedor["_id"], "estado": "asignada"}})
    else:
        vendedores.delete_many({})

    crear_indices_boletas()

    update = {
        "nombre_rifa": nombre,
        "valor_boleta": valor_boleta,
        "cantidad_boletas": cantidad_boletas,
        "premio_mayor": premio_mayor,
        "estado": estado,
        "creada_en": now_local(),
    }
    configuracion.update_one({"_id": CONFIG_ID}, {"$set": update}, upsert=True)
    invalidate_config_cache()
    invalidate_dashboard_cache()

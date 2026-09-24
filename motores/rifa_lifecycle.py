from database import boletas, configuracion, facturas, reservas, rifas, traslados, vendedores
from motores.cache import invalidate_config_cache, invalidate_dashboard_cache
from motores.config_service import require_collections
from motores.constants import BOLETA_MAX, BOLETA_MIN, COMISION_DEFAULT_TIERS, CONFIG_ID, VENDEDOR_LOCAL
from motores.fechas import now_local
from motores.modelos import crear_boleta_base
from motores.ticket_service import estado_pipeline_expr


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
    rifas.create_index("estado", unique=True, sparse=True)
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
    conservar_reservas: bool = True,
) -> dict:
    """Reset all collections for a new rifa (optionally keeping vendor profiles).

    Fixed reservations survive the rollover: they are re-applied as separadas
    with their buyer data (reserva wins over vendor assignments).
    Returns {"reservas_aplicadas": int, "reservas_omitidas": list}.
    """
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
                boletas.update_many(
                    {"_id": {"$in": ids}},
                    [
                        {"$set": {"vendedor_id": vendedor["_id"]}},
                        {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
                    ],
                )
    else:
        vendedores.delete_many({})

    resumen_reservas = {"reservas_aplicadas": 0, "reservas_omitidas": []}
    aplicadas_ids: list[int] = []
    if conservar_reservas and reservas is not None:
        for reserva in reservas.find({}).sort("_id", 1):
            bid = reserva.get("_id")
            cliente = reserva.get("cliente") or {}
            if not isinstance(bid, int) or not (BOLETA_MIN <= bid <= BOLETA_MAX) or bid >= cantidad_boletas or not str(cliente.get("nombre", "")).strip():
                resumen_reservas["reservas_omitidas"].append(bid)
                continue
            boletas.update_one(
                {"_id": bid},
                [
                    {
                        "$set": {
                            "cliente": {
                                "nombre": str(cliente.get("nombre", "")),
                                "telefono": str(cliente.get("telefono", "")),
                                "direccion": str(cliente.get("direccion", "")),
                            },
                            "vendedor_id": VENDEDOR_LOCAL,
                        }
                    },
                    {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
                ],
            )
            aplicadas_ids.append(bid)
            resumen_reservas["reservas_aplicadas"] += 1
        if aplicadas_ids:
            vendedores.update_many({}, {"$pull": {"boletas_asignadas": {"$in": aplicadas_ids}}})

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
    return resumen_reservas

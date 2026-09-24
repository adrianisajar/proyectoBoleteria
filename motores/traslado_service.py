"""Traslado de saldo entre boletas (cambio de n\u00famero).

A traslado moves accrued saldo from an origin ticket to a destination ticket.
It has its own comprobante (collection ``traslados``) and is tracked in the
unified ledger of BOTH boletas: ``traslado_salida`` on the origin and
``traslado_entrada`` on the destination. Egresos are never involved.
"""

import contextlib

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

import database
from database import boletas, configuracion, traslados
from motores.cache import invalidate_dashboard_cache
from motores.config_service import get_config
from motores.constants import CONFIG_ID, MOV_TRASLADO_ENTRADA, MOV_TRASLADO_SALIDA, MOVIMIENTOS_FIELD
from motores.fechas import now_local
from motores.ticket_service import estado_pipeline_expr, movimiento_neto_expr


def next_traslado_id() -> int:
    """Return the next traslado consecutive, skipping ids already in use."""
    for _retry in range(50):
        result = configuracion.find_one_and_update(
            {"_id": CONFIG_ID},
            {"$inc": {"traslado_counter": 1}},
            upsert=True,
            return_document=True,
        )
        candidate = int(result["traslado_counter"])
        if traslados.count_documents({"_id": candidate}) == 0:
            return candidate
    raise RuntimeError("No se pudo obtener un id de traslado libre tras 50 intentos.")


def _append_movimiento(boleta_id: int, mov: dict, valor_boleta: int, *, min_total: int = 0, max_total: int | None = None) -> UpdateOne:
    filtro: dict = {"_id": boleta_id}
    exprs = []
    if min_total:
        exprs.append({"$gte": [{"$ifNull": ["$total_abonado", 0]}, min_total]})
    if max_total is not None:
        exprs.append({"$lte": [{"$ifNull": ["$total_abonado", 0]}, max_total]})
    if len(exprs) == 1:
        filtro["$expr"] = exprs[0]
    elif len(exprs) > 1:
        filtro["$expr"] = {"$and": exprs}
    return UpdateOne(
        filtro,
        [
            {"$set": {MOVIMIENTOS_FIELD: {"$concatArrays": [{"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}, {"$literal": [mov]}]}}},
            {"$set": {"total_abonado": movimiento_neto_expr()}},
            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
        ],
    )


def registrar_traslado(
    traslado_id: int,
    origen: int,
    destino: int,
    valor: int,
    fecha: str,
    vendedor_id: str,
    vendedor_nombre: str,
    usuario: str,
    usuario_nombre: str,
    observaciones: str = "",
) -> None:
    """Move saldo and its receipt in one MongoDB transaction."""
    mov_origen = {
        "tipo": MOV_TRASLADO_SALIDA,
        "fecha": fecha,
        "valor": valor,
        "registrado_en": now_local(),
        "usuario": usuario,
        "traslado_id": traslado_id,
        "contraparte": destino,
    }
    mov_destino = {
        "tipo": MOV_TRASLADO_ENTRADA,
        "fecha": fecha,
        "valor": valor,
        "registrado_en": now_local(),
        "usuario": usuario,
        "traslado_id": traslado_id,
        "contraparte": origen,
    }

    client = getattr(database, "_client", None)
    if client is None:
        raise RuntimeError("MongoDB no está disponible para registrar el traslado.")
    valor_boleta = _valor_boleta()

    def aplicar(session) -> None:
        resultado = boletas.bulk_write(
            [
                _append_movimiento(origen, mov_origen, valor_boleta, min_total=valor),
                _append_movimiento(destino, mov_destino, valor_boleta, max_total=valor_boleta - valor),
            ],
            ordered=True,
            session=session,
        )
        if resultado.matched_count != 2:
            raise ValueError("El saldo cambió mientras se registraba el traslado. Verifique e intente de nuevo.")
        with contextlib.suppress(DuplicateKeyError):
            traslados.insert_one(
                {
                    "_id": traslado_id,
                    "fecha": fecha,
                    "boleta_origen": origen,
                    "boleta_destino": destino,
                    "valor": valor,
                    "vendedor_id": vendedor_id,
                    "vendedor_nombre": vendedor_nombre,
                    "usuario_id": usuario,
                    "usuario_nombre": usuario_nombre,
                    "registrado_en": now_local(),
                    "observaciones": observaciones,
                },
                session=session,
            )

    with client.start_session() as session:
        session.with_transaction(aplicar)
    invalidate_dashboard_cache()


def revertir_traslado(traslado_id: int, valor_boleta: int) -> None:
    """Remove both movements of a traslado and recompute net balance/estado."""
    boletas.update_many(
        {MOVIMIENTOS_FIELD + ".traslado_id": traslado_id},
        [
            {
                "$set": {
                    MOVIMIENTOS_FIELD: {
                        "$filter": {
                            "input": {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]},
                            "cond": {"$ne": ["$$this.traslado_id", traslado_id]},
                        }
                    }
                }
            },
            {"$set": {"total_abonado": movimiento_neto_expr()}},
            {"$set": {"estado": estado_pipeline_expr(valor_boleta)}},
        ],
    )
    traslados.delete_one({"_id": traslado_id})
    invalidate_dashboard_cache()


def _valor_boleta() -> int:
    config = get_config()
    return int(config.get("valor_boleta", 10000) or 10000)

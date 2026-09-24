from database import boletas
from motores.cache import invalidate_dashboard_cache
from motores.constants import MOV_PAGO, MOV_TRASLADO_ENTRADA, MOV_TRASLADO_SALIDA, MOVIMIENTOS_FIELD, VENDEDOR_LOCAL, VENDEDOR_SIN_ASIGNAR


class EstadoBoletaInvalido(Exception):
    """Raised when a ticket state transition violates the FSM matrix."""


# ──────────────────────────────────────────────────────────────────────────────
# FSM — Matriz de transiciones de estado permitidas
# ──────────────────────────────────────────────────────────────────────────────
# Estado siempre se DERIVA de (total_abonado, cliente, vendedor_id) nunca se
# setea directamente.  Las únicas excepciones son init_db (disponible) y
# rifa_lifecycle (pipeline).  Cada función que modifica boletas DEBE terminar
# con un ``$set: {estado: estado_pipeline_expr(valor_boleta)}`` en su pipeline.
#
# Transiciones válidas (12):
#   disponible  → asignada    (vendedor real asignado, sin cliente)
#   disponible  → separada    (cliente guardado, sin vendedor real)
#   disponible  → abonando    (primer abono parcial)
#   disponible  → pagada      (pago total directo)
#   asignada    → abonando    (primer abono a boleta asignada)
#   asignada    → pagada      (pago total a boleta asignada)
#   separada    → abonando    (primer abono a boleta separada)
#   separada    → pagada      (pago total a boleta separada)
#   abonando    → pagada      (acumulado >= valor_boleta)
#   pagada      → pagada      (pago adicional con confirmar_pagadas=1)
#   pagada      → abonando    (rollback por anulación o traslado saliente)
#   pagada      → separada    (rollback que deja total=0 con cliente y sin vendedor real)
#   pagada      → asignada    (rollback que deja total=0 con vendedor real)
#   pagada      → disponible  (rollback que deja total=0 sin cliente ni vendedor)
#
# Notas:
#   - No existe transición asignada → separada (un vendedor real no se pierde
#     al guardar cliente; se preserva).
#   - La degradación de pagada se maneja automáticamente por
#     estado_pipeline_expr al recalcular total_abonado vía movimiento_neto_expr.
# ──────────────────────────────────────────────────────────────────────────────


def estado_para_total(
    total_abonado: int,
    valor_boleta: int,
    vendedor_id: str | None = None,
    cliente_nombre: str | None = None,
) -> str:
    """Derive the ticket estado from its paid total, client info and vendor assignment.

    Pure function — identical logic to ``estado_pipeline_expr`` but runs in
    Python (used for in-memory previews, validation and tests).

    FSM transition matrix (derived, never set directly):
        total_abonado >= valor_boleta  →  pagada
        total_abonado > 0             →  abonando
        tiene_cliente & vendedor_real  →  asignada
        tiene_cliente & !vendedor_real →  separada
        !tiene_cliente & vendedor_real →  asignada
        !tiene_cliente & !vendedor_real → disponible
    """
    if total_abonado >= valor_boleta:
        return "pagada"
    if total_abonado > 0:
        return "abonando"
    tiene_cliente = bool((cliente_nombre or "").strip())
    tiene_vendedor_real = bool(
        vendedor_id and vendedor_id not in (VENDEDOR_SIN_ASIGNAR, VENDEDOR_LOCAL, None)
    )
    if tiene_cliente:
        return "asignada" if tiene_vendedor_real else "separada"
    if tiene_vendedor_real:
        return "asignada"
    return "disponible"


def sync_ticket_statuses(valor_boleta: int) -> None:
    """Recalculate 'estado' for every ticket based on total_abonado (batched pipeline)."""
    batch_size = 1000
    last_id = -1
    while True:
        batch_ids = [doc["_id"] for doc in boletas.find({"_id": {"$gt": last_id}}, {"_id": 1}).sort("_id", 1).limit(batch_size)]
        if not batch_ids:
            break
        boletas.update_many(
            {"_id": {"$in": batch_ids}},
            [{"$set": {"estado": estado_pipeline_expr(valor_boleta)}}],
        )
        last_id = batch_ids[-1]
        if len(batch_ids) < batch_size:
            break
    invalidate_dashboard_cache()


def estado_pipeline_expr(valor_boleta: int) -> dict:
    """Return an aggregation pipeline expression that derives 'estado' from a doc.

    Server-side equivalent of ``estado_para_total`` — runs atomically inside
    MongoDB update pipelines.  Every write that touches boletas MUST end with::

        {"$set": {"estado": estado_pipeline_expr(valor_boleta)}}

    This ensures the FSM is evaluated identically everywhere and no caller
    can set an invalid state.
    """
    valor_literal = {"$literal": int(valor_boleta)}
    tiene_cliente = {"$ne": [{"$ifNull": ["$cliente.nombre", ""]}, ""]}
    tiene_vendedor_real = {
        "$and": [
            {"$ne": [{"$ifNull": ["$vendedor_id", ""]}, ""]},
            {"$ne": [{"$ifNull": ["$vendedor_id", ""]}, VENDEDOR_LOCAL]},
        ]
    }
    separada_o_asignada = {
        "$cond": [
            tiene_vendedor_real,
            "asignada",
            "separada",
        ]
    }
    return {
        "$cond": [
            {"$gte": ["$total_abonado", valor_literal]},
            "pagada",
            {
                "$cond": [
                    {"$gt": ["$total_abonado", 0]},
                    "abonando",
                    {
                        "$cond": [
                            tiene_cliente,
                            separada_o_asignada,
                            {"$cond": [tiene_vendedor_real, "asignada", "disponible"]},
                        ]
                    },
                ]
            },
        ]
    }


def movimiento_neto_expr() -> dict:
    """Aggregation expression: net `total_abonado` from the unified ledger.

    Income (pagos + traslados de entrada) adds; traslados de salida subtract;
    egresos are excluded because they are independent movements that never
    amortize a ticket. Legacy entries without `tipo` count as `pago`.
    """
    movimientos = {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}
    ingresos = {
        "$filter": {
            "input": movimientos,
            "cond": {"$in": [{"$ifNull": ["$$this.tipo", MOV_PAGO]}, [MOV_PAGO, MOV_TRASLADO_ENTRADA]]},
        }
    }
    salidas = {
        "$filter": {
            "input": movimientos,
            "cond": {"$eq": [{"$ifNull": ["$$this.tipo", ""]}, MOV_TRASLADO_SALIDA]},
        }
    }
    return {
        "$max": [
            {"$literal": 0},
            {
                "$subtract": [
                    {
                        "$reduce": {
                            "input": ingresos,
                            "initialValue": 0,
                            "in": {"$add": ["$$value", {"$ifNull": ["$$this.valor", 0]}]},
                        }
                    },
                    {
                        "$reduce": {
                            "input": salidas,
                            "initialValue": 0,
                            "in": {"$add": ["$$value", {"$ifNull": ["$$this.valor", 0]}]},
                        }
                    },
                ]
            },
        ]
    }

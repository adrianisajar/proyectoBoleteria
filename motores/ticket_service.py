from database import boletas
from motores.cache import invalidate_dashboard_cache
from motores.constants import MOV_PAGO, MOV_TRASLADO_ENTRADA, MOV_TRASLADO_SALIDA, MOVIMIENTOS_FIELD, VENDEDOR_LOCAL, VENDEDOR_SIN_ASIGNAR


def estado_para_total(total_abonado: int, valor_boleta: int, cliente: str | None = None, vendedor_id: str | None = None) -> str:
    """Derive the ticket estado from its paid total, client info and vendor assignment."""
    if total_abonado >= valor_boleta:
        return "pagada"
    if total_abonado > 0:
        return "abonando"
    if vendedor_id == VENDEDOR_LOCAL:
        return "separada"
    if vendedor_id and vendedor_id not in (VENDEDOR_SIN_ASIGNAR, None):
        return "asignada"
    return "disponible"


def sync_ticket_statuses(valor_boleta: int) -> None:
    """Recalculate 'estado' for every ticket based on total_abonado (bulk pipeline)."""
    boletas.update_many(
        {},
        [{"$set": {"estado": estado_pipeline_expr(valor_boleta)}}],
    )
    invalidate_dashboard_cache()


def estado_pipeline_expr(valor_boleta: int) -> dict:
    """Return an aggregation pipeline expression that derives 'estado' from a doc."""
    valor_literal = {"$literal": int(valor_boleta)}
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
                            {"$eq": [{"$ifNull": ["$vendedor_id", ""]}, VENDEDOR_LOCAL]},
                            "separada",
                            {"$cond": [{"$ne": [{"$ifNull": ["$vendedor_id", ""]}, ""]}, "asignada", "disponible"]},
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

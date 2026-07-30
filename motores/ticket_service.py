from database import boletas
from motores.constants import VENDEDOR_LOCAL, VENDEDOR_SIN_ASIGNAR
from motores.cache import invalidate_dashboard_cache


def estado_para_total(total_abonado: int, valor_boleta: int, cliente: str | None = None, vendedor_id: str | None = None) -> str:
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
    boletas.update_many(
        {},
        [
            {
                "$set": {
                    "estado": estado_pipeline_expr(valor_boleta)
                }
            }
        ],
    )
    invalidate_dashboard_cache()


def estado_pipeline_expr(valor_boleta: int) -> dict:
    valor_literal = {"$literal": int(valor_boleta)}
    return {
        "$cond": [
            {"$gte": ["$total_abonado", valor_literal]}, "pagada",
            {"$cond": [
                {"$gt": ["$total_abonado", 0]}, "abonando",
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$vendedor_id", ""]}, VENDEDOR_LOCAL]}, "separada",
                    {"$cond": [
                        {"$ne": [{"$ifNull": ["$vendedor_id", ""]}, ""]}, "asignada",
                        "disponible"
                    ]}
                ]}
            ]}
        ]
    }

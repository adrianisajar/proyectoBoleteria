"""Egreso movements on the unified ticket ledger.

Egresos are independent movements (e.g. vendedor commission) that NEVER modify
``total_abonado`` nor ``estado``: income stays intact and the net collection is
computed from both income and egresos (see ``get_dashboard_stats``).
"""

from pymongo import UpdateOne

from database import boletas
from motores.cache import invalidate_dashboard_cache
from motores.constants import MOV_EGRESO, MOVIMIENTOS_FIELD
from motores.fechas import now_local


def build_egreso_detalle(boleta_ids: list[int], factura_id: int) -> list[dict]:
    """Build invoice detail lines from egreso movements of the given tickets."""
    docs = list(boletas.find({"_id": {"$in": boleta_ids}}, sort=[("_id", 1)]))
    detalle = []
    for doc in docs:
        for mov in doc.get(MOVIMIENTOS_FIELD) or []:
            if mov.get("tipo") != MOV_EGRESO or mov.get("factura_id") != factura_id:
                continue
            entry = {
                "boleta": doc["_id"],
                "fecha": str(mov.get("fecha", "")),
                "valor": int(mov.get("valor", 0) or 0),
                "metodo": mov.get("metodo", ""),
            }
            if mov.get("referencia"):
                entry["referencia"] = mov["referencia"]
            if mov.get("banco"):
                entry["banco"] = mov["banco"]
            detalle.append(entry)
    return detalle


def registrar_egresos(factura_id: int, rows: list[dict], fecha: str, usuario: str, sub_tipo: str) -> None:
    """Append an egreso movement per ticket row (bulk, ordered=False)."""
    ops = []
    for r in rows:
        mov = {
            "tipo": MOV_EGRESO,
            "fecha": fecha,
            "valor": int(r["valor"]),
            "metodo": r["metodo"],
            "registrado_en": now_local(),
            "usuario": usuario,
            "factura_id": factura_id,
            "egreso_tipo": sub_tipo,
        }
        if r.get("referencia"):
            mov["referencia"] = r["referencia"]
        if r.get("banco"):
            mov["banco"] = r["banco"]
        ops.append(
            UpdateOne(
                {"_id": r["boleta"]},
                [{"$set": {MOVIMIENTOS_FIELD: {"$concatArrays": [{"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}, {"$literal": [mov]}]}}}],
            )
        )
    boletas.bulk_write(ops, ordered=False)
    invalidate_dashboard_cache()


def rollback_egresos_por_factura(factura_id: int) -> None:
    """Remove egreso movements tied to a factura (does NOT touch total_abonado/estado)."""
    boletas.update_many(
        {MOVIMIENTOS_FIELD + ".factura_id": factura_id},
        [
            {
                "$set": {
                    MOVIMIENTOS_FIELD: {
                        "$filter": {
                            "input": {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]},
                            "cond": {
                                "$not": [
                                    {
                                        "$and": [
                                            {"$eq": [{"$ifNull": ["$$this.factura_id", None]}, factura_id]},
                                            {"$eq": [{"$ifNull": ["$$this.tipo", ""]}, MOV_EGRESO]},
                                        ]
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        ],
    )
    invalidate_dashboard_cache()

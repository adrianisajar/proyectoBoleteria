"""Egreso movements on the unified ticket ledger.

Egresos are independent movements (e.g. vendedor commission) that NEVER modify
``total_abonado`` nor ``estado``: income stays intact and the net collection is
computed from both income and egresos (see ``get_dashboard_stats``).
"""

import uuid

from pymongo import UpdateOne

from database import boletas
from motores.cache import invalidate_dashboard_cache
from motores.constants import MOV_EGRESO, MOVIMIENTOS_FIELD
from motores.fechas import now_local


def build_egreso_detalle(boleta_ids: list[int], factura_id: int) -> list[dict]:
    """Build invoice detail lines from egreso movements of the given tickets."""
    docs = list(boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1, MOVIMIENTOS_FIELD: 1}, sort=[("_id", 1)]))
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
    """Append an egreso movement per ticket row (bulk, ordered=False).

    Each operation includes a filter constraint to enforce the egreso limit
    atomically: the total egresado for a ticket cannot exceed total_abonado.
    On partial failure, all written movements are rolled back.
    """
    batch_id = f"egreso_{factura_id}_{uuid.uuid4().hex[:8]}"
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
            "_temp_batch_id": batch_id,
        }
        if r.get("referencia"):
            mov["referencia"] = r["referencia"]
        if r.get("banco"):
            mov["banco"] = r["banco"]
        # Pre-compute existing egreso total for this ticket via $expr so the
        # constraint is evaluated atomically on the server side.
        total_egreso = {
            "$reduce": {
                "input": {
                    "$filter": {
                        "input": {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]},
                        "cond": {"$eq": [{"$ifNull": ["$$this.tipo", ""]}, MOV_EGRESO]},
                    }
                },
                "initialValue": 0,
                "in": {"$add": ["$$value", {"$ifNull": ["$$this.valor", 0]}]},
            }
        }
        ops.append(
            UpdateOne(
                {
                    "_id": r["boleta"],
                    "$expr": {"$lte": [{"$add": [total_egreso, int(r["valor"])]}, {"$ifNull": ["$total_abonado", 0]}]},
                },
                [{"$set": {MOVIMIENTOS_FIELD: {"$concatArrays": [{"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}, {"$literal": [mov]}]}}}],
            )
        )
    result = boletas.bulk_write(ops, ordered=False)
    if result.matched_count < len(ops):
        # Rollback: remove all movements tagged with this batch_id.
        _rollback_egreso_batch(batch_id)
        omitidas = len(ops) - result.matched_count
        raise ValueError(f"{omitidas} boleta(s) no aceptaron el egreso (superarían lo abonado). Se revirtieron los cambios, intente de nuevo.")
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


def _rollback_egreso_batch(batch_id: str) -> None:
    """Remove movements tagged with a temporary batch id (single atomic pipeline).

    The previous implementation used two separate ``update_many`` calls: first
    ``$unset`` the ``_temp_batch_id`` field, then ``$filter`` to remove the
    movement.  After the ``$unset``, the second query could no longer match
    the documents (the field was gone), so the egreso movements **remained** in
    the ledger permanently — a silent data corruption on partial rollback.

    Fixed: single pipeline that queries for the batch tag, filters the movement
    out, and leaves the document consistent in one atomic MongoDB operation.
    Egresos never affect ``total_abonado`` or ``estado``, so no recomputation
    is needed.
    """
    movimientos = {"$ifNull": ["$" + MOVIMIENTOS_FIELD, []]}
    boletas.update_many(
        {MOVIMIENTOS_FIELD + "._temp_batch_id": batch_id},
        [
            {
                "$set": {
                    MOVIMIENTOS_FIELD: {
                        "$filter": {
                            "input": movimientos,
                            "cond": {"$ne": ["$$this._temp_batch_id", batch_id]},
                        }
                    }
                }
            }
        ],
    )
    invalidate_dashboard_cache()

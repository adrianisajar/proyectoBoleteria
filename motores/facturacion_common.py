from typing import Any

from database import boletas
from motores.constants import METODO_PAGO_DELIO, METODO_TRANSFERENCIA, MOVIMIENTOS_FIELD


def validar_filas_transferencia(form_rows: list[dict[str, Any]], errors: list[str]) -> list[str]:
    """Validate transfer payments: require reference+banco, detect global duplicates.

    Pago a Delio rows are skipped entirely - they need no reference or banco.
    Uses a single batch query instead of N individual lookups.
    """
    for r in form_rows:
        if r["metodo"] == METODO_PAGO_DELIO:
            continue
        if r["metodo"] == METODO_TRANSFERENCIA:
            if not r.get("referencia", "").strip():
                errors.append(f"Referencia obligatoria para transferencia en boleta #{r['boleta']:04d}.")
            if not r.get("banco", "").strip():
                errors.append(f"Banco obligatorio para transferencia en boleta #{r['boleta']:04d}.")
    if not errors:
        seen_refs: set[tuple[str, str]] = set()
        unique_refs: list[tuple[str, str]] = []
        for r in form_rows:
            if r["metodo"] == METODO_PAGO_DELIO:
                continue
            if r["metodo"] == METODO_TRANSFERENCIA:
                ref_key = (r["referencia"].strip(), r["banco"].strip())
                if ref_key not in seen_refs:
                    seen_refs.add(ref_key)
                    unique_refs.append(ref_key)
        if unique_refs:
            or_conditions = [
                {"historial_movimientos": {"$elemMatch": {"$or": [{"tipo": "pago"}, {"tipo": {"$exists": False}}], "metodo": METODO_TRANSFERENCIA, "referencia": ref, "banco": banco}}}
                for ref, banco in unique_refs
            ]
            existing = {doc["_id"] for doc in boletas.find({"$or": or_conditions}, {"_id": 1})}
            if existing:
                dup_docs = list(boletas.find({"_id": {"$in": list(existing)}}, {"_id": 1}))
                dup_map: dict[tuple[str, str], int] = {}
                for doc in dup_docs:
                    for mov in doc.get(MOVIMIENTOS_FIELD) or []:
                        if mov.get("metodo") == METODO_TRANSFERENCIA and mov.get("referencia") and mov.get("banco"):
                            k = (str(mov["referencia"]).strip(), str(mov["banco"]).strip())
                            if k in seen_refs and k not in dup_map:
                                dup_map[k] = doc["_id"]
                for ref, banco in unique_refs:
                    if (ref, banco) in dup_map:
                        errors.append(f"Ya existe un pago por transferencia con referencia {ref} y banco {banco} (boleta #{dup_map[(ref, banco)]:04d}).")
    return errors


def deduplicar_filas_boleta(form_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove duplicate boleta entries (same boleta + same method), return (deduped, count_removed)."""
    seen = set()
    deduped = []
    for r in form_rows:
        key = (r["boleta"], r.get("metodo", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped, len(form_rows) - len(deduped)


def verificar_boletas_existen(boleta_ids: list[int]) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Return (docs_map, missing_ids) for given ticket IDs.

    Projection includes only fields used by all callers: invoice creation,
    validation, and egreso flows.
    """
    projection = {"_id": 1, "estado": 1, "vendedor_id": 1, "total_abonado": 1, "cliente": 1, MOVIMIENTOS_FIELD: 1}
    docs_map = {d["_id"]: d for d in boletas.find({"_id": {"$in": boleta_ids}}, projection)}
    missing = [b for b in boleta_ids if b not in docs_map]
    return docs_map, missing

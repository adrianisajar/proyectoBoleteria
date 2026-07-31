from typing import Any

from database import boletas
from motores.constants import METODO_TRANSFERENCIA
from motores.payment_service import buscar_transferencia_duplicada


def validar_filas_transferencia(form_rows: list[dict[str, Any]], errors: list[str]) -> list[str]:
    """Validate transfer payments: require reference+banco, detect global duplicates."""
    for r in form_rows:
        if r["metodo"] == METODO_TRANSFERENCIA:
            if not r.get("referencia", "").strip():
                errors.append(f"Referencia obligatoria para transferencia en boleta #{r['boleta']:04d}.")
            if not r.get("banco", "").strip():
                errors.append(f"Banco obligatorio para transferencia en boleta #{r['boleta']:04d}.")
    if not errors:
        seen_refs = set()
        for r in form_rows:
            if r["metodo"] == METODO_TRANSFERENCIA:
                ref_key = (r["referencia"].strip(), r["banco"].strip())
                if ref_key in seen_refs:
                    continue
                seen_refs.add(ref_key)
                dup = buscar_transferencia_duplicada(r["referencia"].strip(), r["banco"].strip())
                if dup:
                    errors.append(
                        f"Ya existe un pago por transferencia con referencia {r['referencia'].strip()} y banco {r['banco'].strip()} (boleta #{dup['_id']:04d})."
                    )
    return errors


def deduplicar_filas_boleta(form_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Remove duplicate boleta entries, return (deduped, count_removed)."""
    seen = set()
    deduped = []
    for r in form_rows:
        if r["boleta"] not in seen:
            seen.add(r["boleta"])
            deduped.append(r)
    return deduped, len(form_rows) - len(deduped)


def verificar_boletas_existen(boleta_ids: list[int]) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Return (docs_map, missing_ids) for given ticket IDs."""
    docs_map = {d["_id"]: d for d in boletas.find({"_id": {"$in": boleta_ids}})}
    missing = [b for b in boleta_ids if b not in docs_map]
    return docs_map, missing

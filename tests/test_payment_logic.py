import pytest

from database import boletas
from motores.facturacion_common import deduplicar_filas_boleta, verificar_boletas_existen
from motores.payment_service import (
    build_factura_detalle,
    buscar_transferencia_duplicada,
    next_factura_id,
    registrar_abono_lote,
)


def test_next_factura_id_incrementa(client):
    assert next_factura_id() == 1
    assert next_factura_id() == 2
    assert next_factura_id() == 3


def test_build_factura_detalle(client):
    boletas.update_one(
        {"_id": 5},
        {
            "$set": {
                "historial_pagos": [
                    {"fecha": "2026-07-01", "valor": 40000, "metodo": "efectivo", "factura_id": 9},
                    {"fecha": "2026-07-02", "valor": 30000, "metodo": "transferencia", "referencia": "R1", "banco": "B", "factura_id": 10},
                    {"fecha": "2026-07-03", "valor": 10000, "metodo": "efectivo", "factura_id": 99},
                ]
            }
        },
    )
    detalle = build_factura_detalle([5], 10)
    assert len(detalle) == 1
    assert detalle[0]["valor"] == 30000
    assert detalle[0]["metodo"] == "transferencia"
    assert detalle[0]["referencia"] == "R1"


def test_buscar_transferencia_duplicada(client):
    boletas.update_one(
        {"_id": 5},
        {
            "$set": {
                "historial_pagos": [
                    {"fecha": "2026-07-01", "valor": 40000, "metodo": "transferencia", "referencia": "REF-X", "banco": "BBVA"},
                ]
            }
        },
    )
    assert buscar_transferencia_duplicada("REF-X", "BBVA") is not None
    assert buscar_transferencia_duplicada("REF-Y", "BBVA") is None
    # Nueva regla: misma referencia en OTRO banco = duplicado entre facturas
    assert buscar_transferencia_duplicada("REF-X", "OTRO_BANCO") is not None


def test_registrar_abono_lote(client):
    form = {"fecha": "2026-07-30", "metodo": "efectivo"}
    result = registrar_abono_lote([1, 2], form, 30000)
    assert result.modified_count == 2
    b = boletas.find_one({"_id": 1})
    assert b["total_abonado"] == 30000
    assert b["estado"] == "abonando"


def test_registrar_abono_lote_transferencia_duplicada(client):
    boletas.update_one(
        {"_id": 5},
        {
            "$set": {
                "historial_pagos": [
                    {"fecha": "2026-07-01", "valor": 40000, "metodo": "transferencia", "referencia": "REF-DUP", "banco": "B"},
                ]
            }
        },
    )
    form = {"fecha": "2026-07-30", "metodo": "transferencia", "referencia": "REF-DUP", "banco": "B"}
    with pytest.raises(ValueError):
        registrar_abono_lote([6], form, 30000)


def test_deduplicar_filas_boleta():
    rows = [{"boleta": 1}, {"boleta": 1}, {"boleta": 2}]
    deduped, removed = deduplicar_filas_boleta(rows)
    assert removed == 1
    assert [r["boleta"] for r in deduped] == [1, 2]


def test_verificar_boletas_existen(client):
    docs_map, missing = verificar_boletas_existen([1, 2, 9999])
    assert set(missing) == {9999}
    assert docs_map[1]["_id"] == 1

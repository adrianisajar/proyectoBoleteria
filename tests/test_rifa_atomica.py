"""El rollover de rifa debe ser atómico (transacción) cuando el servidor la soporta.

El camino exitoso ya está cubierto en test_reservas.py (que restaura config/rifas);
aquí se cubre el revert: si falla a mitad, no se queda la rifa vieja borrada.
"""

import pytest

from database import boletas, facturas, rifas
from motores import rifa_lifecycle


@pytest.mark.skipif("not rifa_lifecycle._soporta_transacciones()", reason="El servidor no soporta transacciones (standalone)")
def test_rollover_se_revierte_si_falla_a_mitad(monkeypatch):
    facturas.insert_one({"_id": "FALLA_TEST", "tipo": "cliente", "valor_total": 1})
    rifas_antes = rifas.count_documents({})
    boletas_antes = boletas.count_documents({})

    def _boom(numero, rifa_id):
        raise RuntimeError("fallo simulado para prueba")

    monkeypatch.setattr(rifa_lifecycle, "crear_boleta_base", _boom)

    with pytest.raises(RuntimeError, match="fallo simulado"):
        rifa_lifecycle.crear_nueva_rifa("Rifa Rota", 50000, True)

    # La transacción revirtió los delete_many: no quedó la rifa vieja borrada.
    assert facturas.find_one({"_id": "FALLA_TEST"}) is not None
    assert rifas.count_documents({}) == rifas_antes
    assert boletas.count_documents({}) == boletas_antes

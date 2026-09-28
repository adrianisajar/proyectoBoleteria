from database import boletas
from motores.dashboard_service import get_dashboard_stats


def test_asignada_con_cliente_no_cae_en_disponibles():
    """La FSM marca 'asignada' con vendedor real aunque tenga cliente; el dashboard debe contarla igual."""
    base = get_dashboard_stats(force=True)
    assert base["asignadas"] == 0
    assert base["separadas"] == 0

    boletas.update_one(
        {"_id": 1},
        {"$set": {"vendedor_id": "VEND_0001", "cliente": {"nombre": "MARIA LOPEZ", "telefono": "", "direccion": ""}}},
    )
    stats = get_dashboard_stats(force=True)
    assert stats["total"] == base["total"]
    assert stats["asignadas"] == 1
    assert stats["disponibles"] == base["disponibles"] - 1
    assert stats["separadas"] == 0


def test_separada_sin_vendedor_no_cuenta_como_asignada():
    base = get_dashboard_stats(force=True)
    boletas.update_one({"_id": 2}, {"$set": {"cliente": {"nombre": "JUAN GOMEZ", "telefono": "", "direccion": ""}}})
    stats = get_dashboard_stats(force=True)
    assert stats["separadas"] == 1
    assert stats["asignadas"] == 0
    assert stats["disponibles"] == base["disponibles"] - 1

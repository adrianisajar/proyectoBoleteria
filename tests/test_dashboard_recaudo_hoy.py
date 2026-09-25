from database import boletas
from motores.constants import MOV_PAGO
from motores.dashboard_service import get_dashboard_stats
from motores.fechas import now_local


def _pago(boleta_id, valor, metodo):
    boletas.update_one(
        {"_id": boleta_id},
        {
            "$set": {
                "estado": "abonando",
                "total_abonado": valor,
                "historial_movimientos": [{"tipo": MOV_PAGO, "fecha": now_local().date().isoformat(), "valor": valor, "metodo": metodo, "factura_id": 1}],
            }
        },
    )


def test_recaudo_hoy_desglosado_por_metodo(client):
    _pago(1, 30000, "efectivo")
    _pago(2, 45000, "transferencia")
    stats = get_dashboard_stats(force=True)
    assert stats["recaudo_hoy"] == 75000
    assert stats["recaudo_hoy_efectivo"] == 30000
    assert stats["recaudo_hoy_transferencia"] == 45000
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Efectivo hoy" in body
    assert "Transferencia hoy" in body

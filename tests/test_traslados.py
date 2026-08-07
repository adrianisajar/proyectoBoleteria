from database import boletas, traslados
from motores.constants import MOV_TRASLADO_ENTRADA, MOV_TRASLADO_SALIDA


def _post_traslado(client, origen="0001", destino="0002", valor="10000", vendedor_id="VEND01", fecha="2026-07-30"):
    return client.post(
        "/traslados/nuevo",
        data={
            "origen": origen,
            "destino": destino,
            "valor": valor,
            "fecha": fecha,
            "vendedor_id": vendedor_id,
        },
    )


def _cargar_saldo(boleta_id, valor, vendedor_id="VEND01"):
    boletas.update_one(
        {"_id": boleta_id},
        {
            "$set": {
                "vendedor_id": vendedor_id,
                "estado": "abonando" if valor < 70000 else "pagada",
                "total_abonado": valor,
                "historial_movimientos": [{"tipo": "pago", "fecha": "2026-07-01", "valor": valor, "metodo": "efectivo"}],
            }
        },
    )


def test_traslado_ok_mueve_saldo(client):
    _cargar_saldo(1, 30000)
    resp = _post_traslado(client)
    assert resp.status_code == 302
    assert "/traslados/" in resp.headers["Location"]

    t = traslados.find_one({"boleta_origen": 1, "boleta_destino": 2})
    assert t is not None
    assert t["valor"] == 10000
    assert t["vendedor_id"] == "VEND01"

    b1 = boletas.find_one({"_id": 1})
    b2 = boletas.find_one({"_id": 2})
    assert b1["total_abonado"] == 20000
    assert b2["total_abonado"] == 10000
    tipos1 = [m["tipo"] for m in b1["historial_movimientos"]]
    tipos2 = [m["tipo"] for m in b2["historial_movimientos"]]
    assert tipos1.count(MOV_TRASLADO_SALIDA) == 1
    assert tipos2.count(MOV_TRASLADO_ENTRADA) == 1
    salida = b1["historial_movimientos"][-1]
    assert salida["tipo"] == MOV_TRASLADO_SALIDA
    assert salida["traslado_id"] == t["_id"]
    assert salida["contraparte"] == 2


def test_traslado_caja_permitido(client_caja):
    resp = client_caja.get("/traslados")
    assert resp.status_code == 200
    resp = client_caja.get("/traslados/nuevo")
    assert resp.status_code == 200
    _cargar_saldo(1, 30000)
    resp = _post_traslado(client_caja)
    assert resp.status_code == 302
    assert traslados.count_documents({}) == 1


def test_traslado_sin_saldo_origen(client):
    _cargar_saldo(1, 0)
    resp = _post_traslado(client, valor="10000")
    assert resp.status_code == 200
    assert "no tiene saldo" in resp.get_data(as_text=True)
    assert traslados.count_documents({}) == 0


def test_traslado_valor_supera_saldo(client):
    _cargar_saldo(1, 5000)
    resp = _post_traslado(client, valor="10000")
    assert resp.status_code == 200
    assert "supera el saldo disponible" in resp.get_data(as_text=True)
    assert traslados.count_documents({}) == 0


def test_traslado_destino_excede_valor_boleta(client):
    _cargar_saldo(1, 70000)
    _cargar_saldo(2, 60000)
    resp = _post_traslado(client, valor="20000")
    assert resp.status_code == 200
    assert "superando el valor de la boleta" in resp.get_data(as_text=True)
    assert traslados.count_documents({}) == 0


def test_traslado_origen_destino_igual(client):
    _cargar_saldo(1, 30000)
    resp = _post_traslado(client, origen="0001", destino="0001")
    assert resp.status_code == 200
    assert "deben ser distintas" in resp.get_data(as_text=True)
    assert traslados.count_documents({}) == 0


def test_traslado_boleta_inexistente(client):
    resp = _post_traslado(client, origen="9999")
    assert resp.status_code == 200
    assert "Boletas no encontradas" in resp.get_data(as_text=True)
    assert traslados.count_documents({}) == 0


def test_traslado_requiere_vendedor(client):
    _cargar_saldo(1, 30000)
    resp = _post_traslado(client, vendedor_id="")
    assert resp.status_code == 200
    assert traslados.count_documents({}) == 0


def test_traslado_valor_cero_rechazado(client):
    _cargar_saldo(1, 30000)
    resp = _post_traslado(client, valor="0")
    assert resp.status_code == 200
    assert "mayor que cero" in resp.get_data(as_text=True)
    assert traslados.count_documents({}) == 0


def test_traslado_list_renders(client):
    _cargar_saldo(1, 30000)
    _post_traslado(client)
    resp = client.get("/traslados")
    assert resp.status_code == 200
    assert "Traslados de saldo" in resp.get_data(as_text=True)


def test_ver_traslado_renders(client):
    _cargar_saldo(1, 30000)
    _post_traslado(client)
    t = traslados.find_one({})
    resp = client.get(f"/traslados/{t['_id']}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "COMPROBANTE DE TRASLADO DE SALDO" in html
    assert "10,000" in html

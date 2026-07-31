import hashlib

from app import app as flask_app
from database import boletas, facturas


def _post_factura(client, boletas_, montos, metodos=None, referencias=None, bancos=None, nombre="JUAN PEREZ", telefono="3001234567", fecha="2026-07-30"):
    if metodos is None:
        metodos = ["efectivo"] * len(boletas_)
    if referencias is None:
        referencias = [""] * len(boletas_)
    if bancos is None:
        bancos = [""] * len(boletas_)
    return client.post(
        "/facturas/nueva/cliente",
        data={
            "nombre": nombre,
            "telefono": telefono,
            "direccion": "CRA 1",
            "fecha": fecha,
            "boleta[]": boletas_,
            "monto[]": montos,
            "metodo[]": metodos,
            "referencia[]": referencias,
            "banco[]": bancos,
        },
    )


def test_factura_cliente_pago_total(client):
    resp = _post_factura(client, ["0010"], ["70000"])
    assert resp.status_code == 302
    assert "/facturas/" in resp.headers["Location"]

    f = facturas.find_one({"tipo": "cliente"})
    assert f is not None
    assert f["valor_total"] == 70000
    assert f["cliente"]["nombre"] == "JUAN PEREZ"
    assert f["boletas"] == [10]

    b = boletas.find_one({"_id": 10})
    assert b["estado"] == "pagada"
    assert b["total_abonado"] == 70000
    assert b["cliente"]["nombre"] == "JUAN PEREZ"
    assert b["vendedor_id"] == "LOCAL"


def test_factura_cliente_abono_parcial(client):
    resp = _post_factura(client, ["0010"], ["30000"])
    assert resp.status_code == 302

    b = boletas.find_one({"_id": 10})
    assert b["estado"] == "abonando"
    assert b["total_abonado"] == 30000
    f = facturas.find_one({"tipo": "cliente"})
    assert f["valor_total"] == 30000


def test_factura_cliente_multiples_boletas(client):
    resp = _post_factura(client, ["0010", "0011", "0012"], ["30000", "40000", "70000"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "cliente"})
    assert f["valor_total"] == 140000
    assert f["boletas"] == [10, 11, 12]
    assert boletas.find_one({"_id": 10})["total_abonado"] == 30000
    assert boletas.find_one({"_id": 11})["total_abonado"] == 40000
    assert boletas.find_one({"_id": 12})["total_abonado"] == 70000


def test_factura_cliente_boleta_inexistente(client):
    resp = _post_factura(client, ["9999"], ["70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_fecha_futura(client):
    resp = _post_factura(client, ["0010"], ["70000"], fecha="2099-01-01")
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_nombre_obligatorio(client):
    resp = _post_factura(client, ["0010"], ["70000"], nombre="")
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_monto_excede_valor(client):
    resp = _post_factura(client, ["0010"], ["999999"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_boleta_pagada_rechazada(client):
    boletas.update_one({"_id": 20}, {"$set": {"estado": "pagada", "total_abonado": 70000}})
    resp = _post_factura(client, ["0020"], ["70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_transferencia_sin_referencia(client):
    resp = _post_factura(client, ["0010"], ["30000"], metodos=["transferencia"], referencias=[""], bancos=["BANCOLOMBIA"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_transferencia_ok(client):
    resp = _post_factura(client, ["0010"], ["30000"], metodos=["transferencia"], referencias=["REF-ABC-1"], bancos=["BANCOLOMBIA"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "cliente"})
    assert f["valor_total"] == 30000
    assert f["detalle"][0]["metodo"] == "transferencia"
    assert f["detalle"][0]["referencia"] == "REF-ABC-1"


def test_factura_cliente_referencia_duplicada_en_db(client):
    boletas.update_one(
        {"_id": 21},
        {
            "$set": {
                "estado": "abonando",
                "total_abonado": 30000,
                "historial_pagos": [{"fecha": "2026-07-01", "valor": 30000, "metodo": "transferencia", "referencia": "REF-DUP", "banco": "DAVIVIENDA"}],
            }
        },
    )
    resp = _post_factura(client, ["0010"], ["30000"], metodos=["transferencia"], referencias=["REF-DUP"], bancos=["DAVIVIENDA"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_ver_factura_cliente_renders(client):
    _post_factura(client, ["0010"], ["70000"])
    f = facturas.find_one({"tipo": "cliente"})
    resp = client.get(f"/facturas/{f['_id']}")
    assert resp.status_code == 200


def test_anular_factura_cliente(client):
    _post_factura(client, ["0010"], ["70000"])
    f = facturas.find_one({"tipo": "cliente"})
    fid = f["_id"]
    h = hashlib.sha256(f"{fid}:False:{flask_app.secret_key}".encode()).hexdigest()[:16]
    resp = client.post(f"/facturas/{fid}/anular", data={"motivo": "Error de digitacion", "anulacion_hash": h})
    assert resp.status_code == 302
    b = boletas.find_one({"_id": 10})
    assert b["total_abonado"] == 0
    assert b["estado"] == "separada"
    f = facturas.find_one({"_id": fid})
    assert f["anulada"] is True

from database import boletas, facturas, vendedores


def _crear_vendedor_con_boletas(vendedor_id="VEND01", ids=(1, 2)):
    vendedores.insert_one({"_id": vendedor_id, "nombre": "Ana Gomez", "telefono": "311", "boletas_asignadas": list(ids)})
    boletas.update_many({"_id": {"$in": list(ids)}}, {"$set": {"vendedor_id": vendedor_id, "estado": "asignada"}})


def _post_factura(client, vendedor_id, boletas_, montos, metodos=None, referencias=None, bancos=None, fecha="2026-07-30"):
    if metodos is None:
        metodos = ["efectivo"] * len(boletas_)
    if referencias is None:
        referencias = [""] * len(boletas_)
    if bancos is None:
        bancos = [""] * len(boletas_)
    return client.post(
        "/facturas/nueva/vendedor",
        data={
            "vendedor_id": vendedor_id,
            "fecha": fecha,
            "boleta[]": boletas_,
            "monto[]": montos,
            "metodo[]": metodos,
            "referencia[]": referencias,
            "banco[]": bancos,
        },
    )


def test_factura_vendedor_pago_total(client):
    _crear_vendedor_con_boletas(ids=(1, 2))
    resp = _post_factura(client, "VEND01", ["0001", "0002"], ["70000", "70000"])
    assert resp.status_code == 302
    assert "/facturas/" in resp.headers["Location"]

    f = facturas.find_one({"tipo": "vendedor"})
    assert f["estado"] == "completa"
    assert f["valor_total"] == 140000
    assert f["vendedor_id"] == "VEND01"
    assert f["vendedor_nombre"] == "Ana Gomez"
    assert len(f["detalle"]) == 2
    assert f["total_vendidas"] == 2
    assert boletas.find_one({"_id": 1})["estado"] == "pagada"
    assert boletas.find_one({"_id": 2})["estado"] == "pagada"


def test_factura_vendedor_abono_parcial(client):
    _crear_vendedor_con_boletas(ids=(1,))
    resp = _post_factura(client, "VEND01", ["0001"], ["30000"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "vendedor"})
    assert f["valor_total"] == 30000
    assert boletas.find_one({"_id": 1})["estado"] == "abonando"
    assert boletas.find_one({"_id": 1})["total_abonado"] == 30000


def test_factura_vendedor_boleta_ajena(client):
    _crear_vendedor_con_boletas(ids=(1,))
    resp = _post_factura(client, "VEND01", ["0001", "0005"], ["70000", "70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({"tipo": "vendedor"}) == 0


def test_factura_vendedor_boleta_duplicada_rechazada(client):
    _crear_vendedor_con_boletas(ids=(1, 2))
    resp = _post_factura(client, "VEND01", ["0001", "0001"], ["30000", "30000"])
    assert resp.status_code == 200
    assert "duplicadas" in resp.get_data(as_text=True).lower()
    assert facturas.count_documents({"tipo": "vendedor"}) == 0
    assert boletas.find_one({"_id": 1})["total_abonado"] == 0


def test_factura_vendedor_boleta_duplicada_en_fila_rechazada(client):
    _crear_vendedor_con_boletas(ids=(1,))
    resp = _post_factura(client, "VEND01", ["0001, 0001"], ["30000"])
    assert resp.status_code == 200
    assert "duplicadas" in resp.get_data(as_text=True).lower()
    assert facturas.count_documents({"tipo": "vendedor"}) == 0


def test_factura_vendedor_sin_vendedor(client):
    resp = _post_factura(client, "", ["0001"], ["70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_vendedor_sobrepasa_rollback(client):
    vendedores.insert_one({"_id": "VEND01", "nombre": "Ana", "telefono": "", "boletas_asignadas": [3]})
    boletas.update_one(
        {"_id": 3},
        {
            "$set": {
                "vendedor_id": "VEND01",
                "estado": "abonando",
                "total_abonado": 60000,
                "historial_pagos": [{"fecha": "2026-07-01", "valor": 60000, "metodo": "efectivo"}],
            }
        },
    )
    resp = _post_factura(client, "VEND01", ["0003"], ["50000"])
    assert resp.status_code == 200

    f = facturas.find_one({"tipo": "vendedor"})
    assert f["estado"] == "error"
    b = boletas.find_one({"_id": 3})
    assert b["total_abonado"] == 60000
    assert b["historial_pagos"][0]["valor"] == 60000


def test_factura_vendedor_transferencia_ok(client):
    _crear_vendedor_con_boletas(ids=(1,))
    resp = _post_factura(client, "VEND01", ["0001"], ["30000"], metodos=["transferencia"], referencias=["REF-V-1"], bancos=["NEQUI"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "vendedor"})
    assert f["detalle"][0]["metodo"] == "transferencia"
    assert f["detalle"][0]["referencia"] == "REF-V-1"


def test_ver_factura_vendedor_renders(client):
    _crear_vendedor_con_boletas(ids=(1,))
    _post_factura(client, "VEND01", ["0001"], ["70000"])
    f = facturas.find_one({"tipo": "vendedor"})
    resp = client.get(f"/facturas/{f['_id']}")
    assert resp.status_code == 200

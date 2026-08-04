from database import boletas, configuracion, liquidaciones, vendedores


def _crear_vendedor(vendedor_id="VEND01", ids=(1, 2, 3, 4)):
    vendedores.insert_one({"_id": vendedor_id, "nombre": "Ana Gomez", "telefono": "311", "boletas_asignadas": list(ids)})
    boletas.update_many({"_id": {"$in": list(ids)}}, {"$set": {"vendedor_id": vendedor_id}})


def _pagar_boletas(ids, valor):
    boletas.update_many(
        {"_id": {"$in": list(ids)}},
        {
            "$set": {
                "total_abonado": valor,
                "estado": "pagada" if valor >= 70000 else "abonando",
                "historial_pagos": [{"fecha": "2026-07-30", "valor": valor, "metodo": "efectivo"}],
            }
        },
    )


def _obtener_csrf(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    with client.session_transaction() as s:
        return s["_csrf_token"]


def test_liquidaciones_panel_vacio(client):
    resp = client.get("/liquidaciones")
    assert resp.status_code == 200
    assert "Liquidaciones" in resp.get_data(as_text=True)


def test_liquidaciones_resumen(client):
    _crear_vendedor(ids=(1, 2, 3, 4))
    _pagar_boletas((1, 2, 3), 70000)  # 3 pagadas al 100%
    _pagar_boletas((4,), 30000)  # 1 con abono parcial
    resp = client.get("/liquidaciones")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "VEND01" in body
    assert "Ana Gomez" in body


def test_generar_liquidacion_comision(client):
    _crear_vendedor(ids=(1, 2, 3, 4))
    _pagar_boletas((1, 2, 3), 70000)
    _pagar_boletas((4,), 30000)
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND01")
    resp = client.post(
        "/liquidaciones/vendedor/VEND01/generar",
        data={"csrf_token": tok, "observaciones": "Cierre rifa"},
    )
    assert resp.status_code == 302
    liqui = liquidaciones.find_one({"vendedor_id": "VEND01"})
    assert liqui is not None
    assert liqui["boletas_pagadas"] == 3
    assert liqui["boletas_vendidas"] == 4
    assert liqui["comision_por_boleta"] == 0  # 3 < 10 vendidas -> tier 0
    assert liqui["total_comision"] == 0
    assert liqui["estado"] == "liquidada"
    assert liqui["total_liquidado"] == 0
    assert liqui["pendiente_pagar"] == 0
    assert liqui["observaciones"] == "Cierre rifa"


def test_generar_liquidacion_tier_superior(client):
    _crear_vendedor("VEND02", ids=tuple(range(1, 21)))
    _pagar_boletas(tuple(range(1, 21)), 70000)  # 20 pagadas -> tier 10k
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND02")
    client.post("/liquidaciones/vendedor/VEND02/generar", data={"csrf_token": tok})
    liqui = liquidaciones.find_one({"vendedor_id": "VEND02"})
    assert liqui["total_comision"] == 20 * 10000
    assert liqui["estado"] == "liquidada"
    assert liqui["total_liquidado"] == 20 * 10000
    assert liqui["pendiente_pagar"] == 0


def test_generar_liquidacion_queda_liquidada(client):
    _crear_vendedor("VEND03", ids=tuple(range(1, 16)))
    _pagar_boletas(tuple(range(1, 16)), 70000)  # 15 pagadas -> tier 10k, total 150000
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND03")
    client.post("/liquidaciones/vendedor/VEND03/generar", data={"csrf_token": tok})
    liqui = liquidaciones.find_one({"vendedor_id": "VEND03"})
    assert liqui["total_comision"] == 150000
    assert liqui["estado"] == "liquidada"
    assert liqui["total_liquidado"] == 150000
    assert liqui["pendiente_pagar"] == 0
    assert liqui["pagos"] == []


def test_comprobante_liquidacion_renders(client):
    _crear_vendedor(ids=(1, 2, 3))
    _pagar_boletas((1, 2, 3), 70000)
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND01")
    client.post("/liquidaciones/vendedor/VEND01/generar", data={"csrf_token": tok})
    liqui = liquidaciones.find_one({"vendedor_id": "VEND01"})
    resp = client.get(f"/liquidaciones/{liqui['_id']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "COMPROBANTE DE LIQUIDACIÓN" in body
    assert "Ana Gomez" in body


def test_liquidacion_detalle_vendedor(client):
    _crear_vendedor(ids=(1, 2, 3))
    _pagar_boletas((1,), 70000)
    _pagar_boletas((2,), 20000)
    resp = client.get("/liquidaciones/vendedor/VEND01")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Boletas pendientes de pago" in body
    assert "#0002" in body or "#0003" in body


def test_liquidacion_id_contador_incrementa(client):
    _crear_vendedor("VEND05", ids=(1,))
    _pagar_boletas((1,), 70000)
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND05")
    client.post("/liquidaciones/vendedor/VEND05/generar", data={"csrf_token": tok})
    c = configuracion.find_one({"_id": "rifa"})
    assert c["liquidacion_counter"] >= 1


def test_generar_liquidacion_rechaza_sin_confirmacion(client):
    _crear_vendedor("VEND06", ids=(1,))
    _pagar_boletas((1,), 70000)
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND06")
    client.post("/liquidaciones/vendedor/VEND06/generar", data={"csrf_token": tok})
    total_antes = liquidaciones.count_documents({})

    resp = client.post(
        "/liquidaciones/vendedor/VEND06/generar",
        data={"csrf_token": tok},
    )
    assert resp.status_code == 302
    assert liquidaciones.count_documents({}) == total_antes
    assert "confirmaci" in resp.headers["Location"].lower() or resp.headers["Location"] == "/liquidaciones/vendedor/VEND06"


def test_generar_liquidacion_permite_regen_con_confirmacion(client):
    _crear_vendedor("VEND07", ids=(1,))
    _pagar_boletas((1,), 70000)
    tok = _obtener_csrf(client, "/liquidaciones/vendedor/VEND07")
    client.post("/liquidaciones/vendedor/VEND07/generar", data={"csrf_token": tok})
    total_antes = liquidaciones.count_documents({})

    resp = client.post(
        "/liquidaciones/vendedor/VEND07/generar",
        data={"csrf_token": tok, "confirmar_regen": "1"},
    )
    assert resp.status_code == 302
    assert liquidaciones.count_documents({}) == total_antes + 1

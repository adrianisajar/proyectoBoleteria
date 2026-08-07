from database import boletas, vendedores


def _guardar_vendedor(client, vendedor_id="VEND01", nombre="Vendedor Uno"):
    return client.post(
        "/vendedores",
        data={
            "vendedor_id": "",
            "nombre": nombre,
            "telefono": "3001234567",
            "operacion": "guardar",
            "boletas": "",
        },
    )


def _asignar(client, vendedor_id, boletas_str):
    return client.post(
        "/vendedores",
        data={
            "vendedor_id": vendedor_id,
            "nombre": "",
            "telefono": "",
            "operacion": "asignar",
            "boletas": boletas_str,
        },
    )


def test_crear_vendedor_normaliza_id(client):
    resp = _guardar_vendedor(client, nombre="Vendedor Uno")
    assert resp.status_code == 302
    v = vendedores.find_one({"_id": "VENDEDOR_UNO"})
    assert v is not None
    assert v["nombre"] == "Vendedor Uno"
    assert v["boletas_asignadas"] == []


def test_asignar_boletas(client):
    _guardar_vendedor(client)
    resp = _asignar(client, "VENDEDOR_UNO", "0001, 0002, 0003")
    assert resp.status_code == 302

    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == "VENDEDOR_UNO"
    assert b1["estado"] == "asignada"
    v = vendedores.find_one({"_id": "VENDEDOR_UNO"})
    assert sorted(v["boletas_asignadas"]) == [1, 2, 3]


def test_quitar_boletas(client):
    _guardar_vendedor(client)
    _asignar(client, "VENDEDOR_UNO", "0001, 0002")
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VENDEDOR_UNO",
            "nombre": "",
            "telefono": "",
            "operacion": "quitar",
            "boletas": "0001, 0002",
        },
    )
    assert resp.status_code == 302
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == ""
    assert b1["estado"] == "disponible"


def test_asignar_boletas_incompletas_rechazadas(client):
    _guardar_vendedor(client)
    resp = _asignar(client, "VENDEDOR_UNO", "0001, 42")
    assert resp.status_code == 200
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == ""
    v = vendedores.find_one({"_id": "VENDEDOR_UNO"})
    assert v["boletas_asignadas"] == []


def test_asignar_con_pagos_rechazado(client):
    boletas.update_one(
        {"_id": 5},
        {
            "$set": {
                "total_abonado": 70000,
                "estado": "pagada",
                "historial_movimientos": [{"valor": 70000, "fecha": "2026-07-01"}],
            }
        },
    )
    _guardar_vendedor(client)
    resp = _asignar(client, "VENDEDOR_UNO", "0005")
    assert resp.status_code == 200
    b5 = boletas.find_one({"_id": 5})
    assert b5["vendedor_id"] == ""
    assert vendedores.find_one({"_id": "VENDEDOR_UNO"})["boletas_asignadas"] == []


def test_quitar_boleta_ajena_rechazado(client):
    boletas.update_one({"_id": 6}, {"$set": {"vendedor_id": "OTRO", "estado": "asignada"}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": [6]})
    _guardar_vendedor(client)
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VENDEDOR_UNO",
            "nombre": "",
            "telefono": "",
            "operacion": "quitar",
            "boletas": "0006",
        },
    )
    assert resp.status_code == 200
    b6 = boletas.find_one({"_id": 6})
    assert b6["vendedor_id"] == "OTRO"


def test_eliminar_vendedor_libera_boletas(client):
    _guardar_vendedor(client)
    _asignar(client, "VENDEDOR_UNO", "0001, 0002")
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VENDEDOR_UNO",
            "nombre": "",
            "telefono": "",
            "operacion": "eliminar",
            "boletas": "",
        },
    )
    assert resp.status_code == 302
    assert vendedores.find_one({"_id": "VENDEDOR_UNO"}) is None
    assert boletas.find_one({"_id": 1})["vendedor_id"] == ""


def test_eliminar_vendedor_con_pagos_bloqueado(client):
    vendedores.insert_one({"_id": "VEND01", "nombre": "Con pagos", "boletas_asignadas": [7]})
    boletas.update_one(
        {"_id": 7},
        {
            "$set": {
                "vendedor_id": "VEND01",
                "estado": "pagada",
                "total_abonado": 70000,
                "historial_movimientos": [{"valor": 70000, "fecha": "2026-07-01"}],
            }
        },
    )
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND01",
            "nombre": "",
            "telefono": "",
            "operacion": "eliminar",
            "boletas": "",
        },
    )
    assert resp.status_code == 200
    assert vendedores.find_one({"_id": "VEND01"}) is not None


def test_api_vendedores_busqueda(client):
    _guardar_vendedor(client)
    resp = client.get("/api/vendedores?q=vend")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(v["_id"] == "VENDEDOR_UNO" for v in data)

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


def test_crear_vendedor_id_secuencial(client):
    resp = _guardar_vendedor(client, nombre="Vendedor Uno")
    assert resp.status_code == 302
    v = vendedores.find_one({"_id": "VEND_0001"})
    assert v is not None
    assert v["nombre"] == "Vendedor Uno"
    assert v["boletas_asignadas"] == []


def test_crear_vendedor_ids_secuenciales(client):
    _guardar_vendedor(client, nombre="Vendedor Uno")
    _guardar_vendedor(client, nombre="Vendedor Dos")
    assert vendedores.find_one({"_id": "VEND_0001"}) is not None
    v2 = vendedores.find_one({"_id": "VEND_0002"})
    assert v2 is not None
    assert v2["nombre"] == "Vendedor Dos"


def test_crear_vendedor_salta_id_existente(client):
    vendedores.insert_one({"_id": "VEND_0001", "nombre": "Ocupado", "boletas_asignadas": []})
    _guardar_vendedor(client, nombre="Nuevo")
    assert vendedores.find_one({"_id": "VEND_0001"})["nombre"] == "Ocupado"
    v = vendedores.find_one({"_id": "VEND_0002"})
    assert v is not None
    assert v["nombre"] == "Nuevo"


def test_asignar_boletas(client):
    _guardar_vendedor(client)
    resp = _asignar(client, "VEND_0001", "0001, 0002, 0003")
    assert resp.status_code == 302

    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == "VEND_0001"
    assert b1["estado"] == "asignada"
    v = vendedores.find_one({"_id": "VEND_0001"})
    assert sorted(v["boletas_asignadas"]) == [1, 2, 3]


def test_quitar_boletas(client):
    _guardar_vendedor(client)
    _asignar(client, "VEND_0001", "0001, 0002")
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND_0001",
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
    resp = _asignar(client, "VEND_0001", "0001, 42")
    assert resp.status_code == 200
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == ""
    v = vendedores.find_one({"_id": "VEND_0001"})
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
    resp = _asignar(client, "VEND_0001", "0005")
    assert resp.status_code == 200
    b5 = boletas.find_one({"_id": 5})
    assert b5["vendedor_id"] == ""
    assert vendedores.find_one({"_id": "VEND_0001"})["boletas_asignadas"] == []


def test_quitar_boleta_ajena_rechazado(client):
    boletas.update_one({"_id": 6}, {"$set": {"vendedor_id": "OTRO", "estado": "asignada"}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": [6]})
    _guardar_vendedor(client)
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND_0001",
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
    _asignar(client, "VEND_0001", "0001, 0002")
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND_0001",
            "nombre": "",
            "telefono": "",
            "operacion": "eliminar",
            "boletas": "",
        },
    )
    assert resp.status_code == 302
    assert vendedores.find_one({"_id": "VEND_0001"}) is None
    assert boletas.find_one({"_id": 1})["vendedor_id"] == ""


def test_operacion_sin_vendedor_seleccionado_rechazada(client):
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "",
            "nombre": "Vendedor Uno",
            "telefono": "",
            "operacion": "asignar",
            "boletas": "0001",
        },
    )
    assert resp.status_code == 200
    assert vendedores.count_documents({"_id": "VEND_0001"}) == 0


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
    assert any(v["_id"] == "VEND_0001" for v in data)

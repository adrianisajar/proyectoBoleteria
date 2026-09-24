from database import boletas, vendedores
from motores.cache import invalidate_vendor_panel_cache


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


def _asignar(client, vendedor_id, boletas_str, confirmar_pagos=""):
    return client.post(
        "/vendedores",
        data={
            "vendedor_id": vendedor_id,
            "nombre": "",
            "telefono": "",
            "operacion": "asignar",
            "boletas": boletas_str,
            "confirmar_pagos": confirmar_pagos,
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


def test_asignar_con_fecha_registra_fecha(client):
    _guardar_vendedor(client)
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND_0001",
            "nombre": "",
            "telefono": "",
            "operacion": "asignar",
            "boletas": "0001, 0002",
            "fecha_asignacion": "2026-07-15",
        },
    )
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] == "2026-07-15"
    assert boletas.find_one({"_id": 2})["fecha_adquisicion"] == "2026-07-15"


def test_reasignar_con_fecha_sobrescribe(client):
    _guardar_vendedor(client)
    _asignar(client, "VEND_0001", "0001")
    boletas.update_one({"_id": 1}, {"$set": {"fecha_adquisicion": "2026-06-01"}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": []})
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "OTRO",
            "nombre": "",
            "telefono": "",
            "operacion": "asignar",
            "boletas": "0001",
            "fecha_asignacion": "2026-08-01",
        },
    )
    assert resp.status_code == 302
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == "OTRO"
    assert b1["fecha_adquisicion"] == "2026-08-01"


def test_asignar_sin_fecha_conserva_existente(client):
    _guardar_vendedor(client)
    boletas.update_one({"_id": 1}, {"$set": {"fecha_adquisicion": "2026-06-01"}})
    resp = _asignar(client, "VEND_0001", "0001")
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] == "2026-06-01"


def test_reasignar_sin_fecha_limpia_fecha(client):
    _guardar_vendedor(client)
    _asignar(client, "VEND_0001", "0001")
    boletas.update_one({"_id": 1}, {"$set": {"fecha_adquisicion": "2026-06-01"}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": []})
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "OTRO",
            "nombre": "",
            "telefono": "",
            "operacion": "asignar",
            "boletas": "0001",
            "fecha_asignacion": "",
        },
    )
    assert resp.status_code == 302
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == "OTRO"
    assert b1.get("fecha_adquisicion") is None


def test_reasignar_mismo_vendedor_sin_fecha_conserva(client):
    _guardar_vendedor(client)
    _asignar(client, "VEND_0001", "0001")
    boletas.update_one({"_id": 1}, {"$set": {"fecha_adquisicion": "2026-06-01"}})
    resp = _asignar(client, "VEND_0001", "0001")
    assert resp.status_code == 302
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == "VEND_0001"
    assert b1["fecha_adquisicion"] == "2026-06-01"


def test_asignar_fecha_futura_rechazada(client):
    _guardar_vendedor(client)
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND_0001",
            "nombre": "",
            "telefono": "",
            "operacion": "asignar",
            "boletas": "0001",
            "fecha_asignacion": "2099-01-01",
        },
    )
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 1}).get("fecha_adquisicion") in (None, "")
    assert boletas.find_one({"_id": 1})["vendedor_id"] == ""


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


def test_asignar_con_pagos_confirmado_reasigna(client):
    boletas.update_many(
        {"_id": {"$in": [5, 6]}},
        {"$set": {"vendedor_id": "OTRO", "estado": "asignada"}},
    )
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": [5, 6]})
    boletas.update_one(
        {"_id": 5},
        {
            "$set": {
                "total_abonado": 30000,
                "estado": "abonando",
                "historial_movimientos": [{"tipo": "pago", "valor": 30000, "fecha": "2026-07-01", "metodo": "efectivo"}],
            }
        },
    )
    _guardar_vendedor(client)
    resp = _asignar(client, "VEND_0001", "0005, 0006", confirmar_pagos="1")
    assert resp.status_code == 302
    b5 = boletas.find_one({"_id": 5})
    assert b5["vendedor_id"] == "VEND_0001"
    assert b5["total_abonado"] == 30000
    assert b5["estado"] == "abonando"
    assert len(b5["historial_movimientos"]) == 1
    assert boletas.find_one({"_id": 6})["vendedor_id"] == "VEND_0001"
    assert vendedores.find_one({"_id": "OTRO"})["boletas_asignadas"] == []
    assert sorted(vendedores.find_one({"_id": "VEND_0001"})["boletas_asignadas"]) == [5, 6]


def test_cambiar_nombre_ok(client):
    _guardar_vendedor(client, nombre="Nombre Viejo")
    _asignar(client, "VEND_0001", "0001")
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND_0001",
            "nombre": "Nombre Viejo",
            "telefono": "",
            "operacion": "cambiar_nombre",
            "boletas": "",
            "nuevo_nombre": "Nombre Nuevo",
        },
    )
    assert resp.status_code == 302
    assert vendedores.find_one({"_id": "VEND_0001"})["nombre"] == "Nombre Nuevo"
    assert boletas.find_one({"_id": 1})["vendedor_id"] == "VEND_0001"


def test_cambiar_nombre_local_bloqueado(client):
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "LOCAL",
            "nombre": "Otro",
            "telefono": "",
            "operacion": "cambiar_nombre",
            "boletas": "",
            "nuevo_nombre": "Otro",
        },
    )
    assert resp.status_code == 200
    assert vendedores.find_one({"_id": "LOCAL"}) is None


def test_cambiar_nombre_inexistente_rechazado(client):
    resp = client.post(
        "/vendedores",
        data={
            "vendedor_id": "VEND99",
            "nombre": "Falso",
            "telefono": "",
            "operacion": "cambiar_nombre",
            "boletas": "",
            "nuevo_nombre": "Falso",
        },
    )
    assert resp.status_code == 200
    assert vendedores.find_one({"_id": "VEND99"}) is None


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


def test_eliminar_vendedor_deja_boletas_disponibles(client):
    _guardar_vendedor(client)
    _asignar(client, "VEND_0001", "0001, 0002")
    boletas.update_one(
        {"_id": 1},
        {"$set": {"cliente": {"nombre": "JUAN", "telefono": "300", "direccion": ""}, "fecha_adquisicion": "2026-07-10"}},
    )
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
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == ""
    assert b1["estado"] == "disponible"
    assert (b1.get("cliente") or {}).get("nombre", "") == ""
    assert b1.get("fecha_adquisicion") is None


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


def test_vendedores_orden_por_nombre(client):
    _guardar_vendedor(client, nombre="Zeta")
    _guardar_vendedor(client, nombre="Alfa")
    invalidate_vendor_panel_cache()
    try:
        resp = client.get("/vendedores?sort_by=nombre&sort_dir=asc")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert html.index("Alfa") < html.index("Zeta")

        resp = client.get("/vendedores?sort_by=nombre&sort_dir=desc")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert html.index("Zeta") < html.index("Alfa")
    finally:
        invalidate_vendor_panel_cache()


def test_vendedores_sort_invalido_usa_default(client):
    resp = client.get("/vendedores?sort_by=__invalido__&sort_dir=zzz")
    assert resp.status_code == 200


def test_vendedores_redirect_preserva_sort(client):
    resp = client.post(
        "/vendedores?sort_by=nombre&sort_dir=desc",
        data={
            "vendedor_id": "",
            "nombre": "Ordenado",
            "telefono": "",
            "operacion": "guardar",
            "boletas": "",
        },
    )
    assert resp.status_code == 302
    assert "sort_by=nombre" in resp.headers["Location"]
    assert "sort_dir=desc" in resp.headers["Location"]

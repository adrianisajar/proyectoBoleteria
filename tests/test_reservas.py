from database import boletas, configuracion, reservas, rifas, vendedores
from motores.rifa_lifecycle import crear_nueva_rifa


def _agregar(client, boleta, nombre="JUAN PEREZ", telefono="300", direccion=""):
    return client.post(
        "/compradores/reservas",
        data={"action": "agregar", "boleta": str(boleta), "nombre": nombre, "telefono": telefono, "direccion": direccion},
    )


def test_reservas_requiere_admin(client_caja):
    resp = client_caja.get("/compradores/reservas")
    assert resp.status_code == 403


def test_agregar_reserva_ok(client):
    resp = _agregar(client, 42)
    assert resp.status_code == 302
    r = reservas.find_one({"_id": 42})
    assert r is not None
    assert r["cliente"]["nombre"] == "JUAN PEREZ"
    assert r["cliente"]["telefono"] == "300"


def test_agregar_reserva_duplicada_rechazada(client):
    _agregar(client, 42)
    _agregar(client, 42)
    assert reservas.count_documents({"_id": 42}) == 1


def test_agregar_reserva_invalida(client):
    _agregar(client, "XX")
    _agregar(client, 43, nombre="")
    assert reservas.count_documents({}) == 0


def test_eliminar_reserva(client):
    _agregar(client, 42)
    resp = client.post("/compradores/reservas", data={"action": "eliminar", "boleta": "42"})
    assert resp.status_code == 302
    assert reservas.find_one({"_id": 42}) is None


def test_listar_reservas(client):
    _agregar(client, 42, nombre="Ana")
    resp = client.get("/compradores/reservas")
    assert resp.status_code == 200
    assert "ANA" in resp.get_data(as_text=True)


def test_aviso_reserva_en_validar_asignar(client):
    _agregar(client, 30, nombre="Fijo")
    resp = client.post("/api/validar-boletas-vendedor", json={"boletas": [30], "operacion": "asignar", "vendedor_id": ""})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    item = data["resultados"][0]
    assert item["ok"] is True
    assert "Reservada para FIJO" in item["aviso"]


def test_badge_reserva_en_consultas(client):
    _agregar(client, 30, nombre="Fijo")
    resp = client.get("/consultas?numero=0030")
    assert resp.status_code == 200
    assert "Reservada para" in resp.get_data(as_text=True)


def test_bulk_mismo_cliente_varias_boletas(client):
    resp = client.post(
        "/api/compradores/reservas",
        json={
            "rows": [
                {"boleta": 10, "nombre": "Fijo", "telefono": "300", "direccion": ""},
                {"boleta": 11, "nombre": "Fijo", "telefono": "300", "direccion": ""},
                {"boleta": 12, "nombre": "Fijo", "telefono": "300", "direccion": ""},
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["guardadas"] == 3
    assert reservas.count_documents({"cliente.nombre": "FIJO"}) == 3


def test_bulk_duplicadas_y_reservadas(client):
    _agregar(client, 10, nombre="Otro")
    resp = client.post(
        "/api/compradores/reservas",
        json={
            "rows": [
                {"boleta": 10, "nombre": "Fijo", "telefono": "", "direccion": ""},
                {"boleta": 11, "nombre": "Fijo", "telefono": "", "direccion": ""},
                {"boleta": 11, "nombre": "Fijo", "telefono": "", "direccion": ""},
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["guardadas"] == 1
    assert data["ya_reservadas"] == [10]
    assert data["duplicadas"] == [11]
    assert reservas.find_one({"_id": 10})["cliente"]["nombre"] == "OTRO"


def test_bulk_invalidas_y_no_existe(client):
    resp = client.post(
        "/api/compradores/reservas",
        json={
            "rows": [
                {"boleta": "XX", "nombre": "Fijo", "telefono": "", "direccion": ""},
                {"boleta": 11, "nombre": "", "telefono": "", "direccion": ""},
                {"boleta": 9999, "nombre": "Fijo", "telefono": "", "direccion": ""},
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["guardadas"] == 0
    assert len(data["invalidas"]) == 2
    assert data["no_existe"] == [9999]
    assert reservas.count_documents({}) == 0


def test_bulk_requiere_admin(client_caja):
    resp = client_caja.post("/api/compradores/reservas", json={"rows": []})
    assert resp.status_code == 403


def test_rollover_conserva_reservas(client):
    reservas.insert_one({"_id": 10, "cliente": {"nombre": "FIJO UNO", "telefono": "", "direccion": ""}})
    reservas.insert_one({"_id": 20, "cliente": {"nombre": "FIJO DOS", "telefono": "311", "direccion": ""}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": [20]})
    config_antes = configuracion.find_one({"_id": "rifa"})
    rifas_antes = list(rifas.find({}))
    rifa_orig = boletas.find_one({"_id": 0})["rifa_id"]
    try:
        resumen = crear_nueva_rifa("Rifa Nueva", 70000, True, cantidad_boletas=500, conservar_reservas=True)
        assert resumen["reservas_aplicadas"] == 2
        assert resumen["reservas_omitidas"] == []
        b10 = boletas.find_one({"_id": 10})
        assert b10["estado"] == "separada"
        assert b10["vendedor_id"] == "LOCAL"
        assert b10["cliente"]["nombre"] == "FIJO UNO"
        assert b10["total_abonado"] == 0
        b20 = boletas.find_one({"_id": 20})
        assert b20["estado"] == "separada"
        assert b20["vendedor_id"] == "LOCAL"
        assert b20["cliente"]["nombre"] == "FIJO DOS"
        assert 20 not in (vendedores.find_one({"_id": "OTRO"}) or {}).get("boletas_asignadas", [])
    finally:
        boletas.delete_many({"_id": {"$gte": 500}})
        boletas.update_many({"_id": {"$lt": 500}}, {"$set": {"rifa_id": rifa_orig}})
        rifas.delete_many({})
        if rifas_antes:
            rifas.insert_many(rifas_antes)
        configuracion.replace_one({"_id": "rifa"}, config_antes, upsert=True)


def test_rollover_sin_conservar_omite(client):
    reservas.insert_one({"_id": 10, "cliente": {"nombre": "FIJO UNO", "telefono": "", "direccion": ""}})
    config_antes = configuracion.find_one({"_id": "rifa"})
    rifas_antes = list(rifas.find({}))
    rifa_orig = boletas.find_one({"_id": 0})["rifa_id"]
    try:
        resumen = crear_nueva_rifa("Rifa Nueva", 70000, False, cantidad_boletas=500, conservar_reservas=False)
        assert resumen["reservas_aplicadas"] == 0
        b10 = boletas.find_one({"_id": 10})
        assert b10["estado"] == "disponible"
        assert (b10.get("cliente") or {}).get("nombre", "") == ""
    finally:
        boletas.delete_many({"_id": {"$gte": 500}})
        boletas.update_many({"_id": {"$lt": 500}}, {"$set": {"rifa_id": rifa_orig}})
        rifas.delete_many({})
        if rifas_antes:
            rifas.insert_many(rifas_antes)
        configuracion.replace_one({"_id": "rifa"}, config_antes, upsert=True)

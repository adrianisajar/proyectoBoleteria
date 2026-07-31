from database import boletas


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["db"] == "connected"
    assert isinstance(data["factura_counter"], int)
    assert data["config_presente"] is True
    assert isinstance(data["indices"], dict)


def test_api_boleta_existente(client):
    resp = client.get("/api/boletas/1")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["boleta"] == "0001"


def test_api_boleta_fuera_de_rango(client):
    resp = client.get("/api/boletas/99999")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_boleta_inexistente(client):
    resp = client.get("/api/boletas/9000")
    assert resp.status_code == 404
    assert resp.is_json


def test_api_clientes_requiere_minimo_2(client):
    resp = client.get("/api/clientes?q=a")
    assert resp.get_json() == []


def test_api_clientes_encuentra_cliente(client):
    boletas.update_one({"_id": 5}, {"$set": {"cliente": {"nombre": "PEDRO DIAZ", "telefono": "3001112233", "direccion": ""}}})
    resp = client.get("/api/clientes?q=pedro")
    items = resp.get_json()
    assert isinstance(items, list)
    assert any(i["nombre"] == "PEDRO DIAZ" for i in items)


def test_api_404_devuelve_json(client):
    resp = client.get("/api/ruta/inexistente")
    assert resp.status_code == 404
    assert resp.is_json
    assert resp.get_json()["ok"] is False
    assert resp.get_json()["codigo"] == 404


def test_api_404_no_devuelve_html(client):
    resp = client.get("/api/ruta/inexistente")
    assert resp.status_code == 404
    assert "Página no encontrada" not in resp.get_data(as_text=True)


def test_consulta_page_renders(client):
    resp = client.get("/consultas")
    assert resp.status_code == 200


def test_dashboard_renders(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200


def test_404_renders_error_page(client):
    resp = client.get("/ruta/inexistente")
    assert resp.status_code == 404
    assert "Página no encontrada" in resp.get_data(as_text=True)

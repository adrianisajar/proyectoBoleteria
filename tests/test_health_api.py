from conftest import _seed_once

from database import boletas, rifas
from motores.config_service import get_rifa_activa


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


def test_nueva_rifa_actualiza_parametros(client):
    client.get("/configuracion")
    with client.session_transaction() as s:
        tok = s["_csrf_token"]
    try:
        resp = client.post(
            "/rifas/nueva",
            data={
                "csrf_token": tok,
                "nombre_rifa_nueva": "Rifa de prueba",
                "valor_boleta_nueva": "50.000",
                "cantidad_boletas": "8000",
                "confirmacion": "NUEVA RIFA",
            },
        )
        assert resp.status_code == 302
        rifa = get_rifa_activa(force=True)
        assert rifa["nombre"] == "Rifa de prueba"
        assert rifa["valor_boleta"] == 50000
        assert rifa["cantidad_boletas"] == 8000
        assert rifas.count_documents({"estado": "activa"}) == 1
    finally:
        _seed_once()

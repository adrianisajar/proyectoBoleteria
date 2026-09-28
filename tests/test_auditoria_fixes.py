"""Tests de regresión de los fixes de la auditoría general."""

from conftest import ADMIN_PASSWORD, ADMIN_USUARIO

from database import boletas, vendedores
from motores.ticket_service import update_ticket_safe


def test_update_ticket_safe_con_doc_sin_version(client):
    """CRITICAL: docs creados antes del locking no tienen _version."""
    boletas.update_one({"_id": 7}, {"$unset": {"_version": ""}})
    assert update_ticket_safe(7, [{"$set": {"vendedor_id": "VENDX"}}]) is True
    doc = boletas.find_one({"_id": 7})
    assert doc["vendedor_id"] == "VENDX"
    assert doc["_version"] == 1


def test_update_ticket_safe_incrementa_version(client):
    assert update_ticket_safe(8, [{"$set": {"vendedor_id": ""}}]) is True
    doc = boletas.find_one({"_id": 8})
    assert isinstance(doc.get("_version"), int) and doc["_version"] >= 1


def test_guardar_boleta_funciona(client):
    resp = client.post(
        "/boletas/5/guardar",
        data={"nombre": "MARIA PEREZ", "telefono": "3001234567", "direccion": "CALLE 1", "vendedor_id": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Datos guardados" in resp.get_data(as_text=True)
    doc = boletas.find_one({"_id": 5})
    assert doc["cliente"]["nombre"] == "MARIA PEREZ"
    assert doc["estado"] == "separada"


def test_limpiar_boleta_funciona(client):
    client.post("/boletas/6/guardar", data={"nombre": "JUAN GOMEZ", "telefono": "300", "direccion": "", "vendedor_id": ""})
    resp = client.post("/boletas/6/limpiar", follow_redirects=True)
    assert resp.status_code == 200
    assert "eliminados" in resp.get_data(as_text=True)
    doc = boletas.find_one({"_id": 6})
    assert doc["cliente"]["nombre"] == ""
    assert doc["estado"] == "disponible"


def test_login_regenera_sid_de_sesion_previa(client_anon):
    """HIGH: el sid debe cambiar al hacer login (anti fixation)."""
    client_anon.get("/login")
    sid_previo = client_anon.get_cookie("session").value
    with client_anon.session_transaction() as sess:
        token = sess.get("_csrf_token")
    resp = client_anon.post(
        "/login",
        data={"usuario": ADMIN_USUARIO, "password": ADMIN_PASSWORD, "csrf_token": token},
    )
    assert resp.status_code == 302
    sid_nuevo = client_anon.get_cookie("session").value
    assert sid_nuevo != sid_previo


def test_asignar_rapido_reutiliza_vendedor_existente_por_nombre(client):
    """HIGH: la búsqueda por nombre ($in con {$regex} inválido) no debe duplicar."""
    vendedores.insert_one({"_id": "VEND0001", "nombre": "JOSÉ PEREZ", "boletas_asignadas": []})
    resp = client.post(
        "/vendedores/api/asignar-rapido",
        json={"assignments": [{"vendedor_id": "", "vendedor_nombre": "josé   perez", "boleta": 3}]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["assigned"] == 1
    assert vendedores.count_documents({}) == 1
    assert boletas.find_one({"_id": 3})["vendedor_id"] == "VEND0001"


def test_asignar_rapido_quita_boleta_del_vendedor_anterior(client):
    """HIGH: el $pull debe ejecutarse aunque el doc proyectado no tenga vendedor_id."""
    vendedores.insert_many(
        [
            {"_id": "VEND0001", "nombre": "ALFA", "boletas_asignadas": [4]},
            {"_id": "VEND0002", "nombre": "BETA", "boletas_asignadas": []},
        ]
    )
    boletas.update_one({"_id": 4}, {"$set": {"vendedor_id": "VEND0001"}})
    resp = client.post(
        "/vendedores/api/asignar-rapido",
        json={"assignments": [{"vendedor_id": "VEND0002", "vendedor_nombre": "BETA", "boleta": 4}]},
    )
    assert resp.status_code == 200
    assert resp.get_json()["assigned"] == 1
    assert 4 not in vendedores.find_one({"_id": "VEND0001"})["boletas_asignadas"]
    assert 4 in vendedores.find_one({"_id": "VEND0002"})["boletas_asignadas"]
    assert boletas.find_one({"_id": 4})["vendedor_id"] == "VEND0002"

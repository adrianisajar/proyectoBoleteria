import pytest

from app import app as flask_app


@pytest.fixture()
def csrf_client():
    """Client with TESTING disabled so CSRF enforcement is active."""
    flask_app.config.update(TESTING=False)
    client = flask_app.test_client()
    yield client
    flask_app.config.update(TESTING=True)


def test_post_sin_token_csrf_rechazado(csrf_client):
    resp = csrf_client.post("/api/validar-boletas-vendedor", json={"boletas": []})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False


def test_post_con_token_csrf_aceptado(csrf_client):
    csrf_client.get("/dashboard")
    with csrf_client.session_transaction() as sess:
        token = sess["_csrf_token"]
    resp = csrf_client.post(
        "/api/validar-referencias-vendedor",
        json={"rows": []},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_post_token_incorrecto_csrf_rechazado(csrf_client):
    csrf_client.get("/dashboard")
    resp = csrf_client.post(
        "/api/validar-referencias-vendedor",
        json={"rows": []},
        headers={"X-CSRF-Token": "token-invalido"},
    )
    assert resp.status_code == 400


def test_form_htmx_csrf_rechazado_sin_token(csrf_client):
    resp = csrf_client.post("/vendedores", data={"operacion": "guardar"})
    assert resp.status_code == 400

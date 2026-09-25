from conftest import login


def test_static_no_abre_sesion_en_mongodb(client, monkeypatch):
    from flask_session.mongodb import MongoDBSessionInterface

    def _boom(self, app, request):  # type: ignore[no-untyped-def]
        raise AssertionError("open_session no debe consultar MongoDB para /static/")

    monkeypatch.setattr(MongoDBSessionInterface, "_retrieve_session_data", _boom)
    resp = client.get("/static/js/app.js")
    assert resp.status_code == 200


def test_pagina_normal_si_usa_sesion(client):
    resp = login(client)
    assert resp.status_code == 302
    resp = client.get("/consultas")
    assert resp.status_code == 200

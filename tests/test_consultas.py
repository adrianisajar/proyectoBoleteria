from database import boletas
from motores.consulta_service import build_consulta_context


def _ctx(params):
    filters, query, errors, page, limite, offset, has_filters, numero_exacto = build_consulta_context(params)
    return filters, query, errors, page, limite, offset, has_filters, numero_exacto


def test_fecha_adquisicion_eq(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "2026-08-05", "fecha_adquisicion_op": "eq"})
    assert errors == []
    assert query["fecha_adquisicion"] == {"$eq": "2026-08-05"}


def test_fecha_adquisicion_gte(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "2026-08-05", "fecha_adquisicion_op": "gte"})
    assert errors == []
    assert query["fecha_adquisicion"] == {"$gte": "2026-08-05"}


def test_fecha_adquisicion_lte(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "2026-08-05", "fecha_adquisicion_op": "lte"})
    assert errors == []
    assert query["fecha_adquisicion"] == {"$lte": "2026-08-05"}


def test_fecha_adquisicion_formato_invalido(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "05/08/2026", "fecha_adquisicion_op": "eq"})
    assert any("formato AAAA-MM-DD" in e for e in errors)
    assert "fecha_adquisicion" not in query


def test_fecha_adquisicion_operador_invalido(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "2026-08-05", "fecha_adquisicion_op": "gt"})
    assert any("Operador de fecha de adquisición inválido" in e for e in errors)
    assert "fecha_adquisicion" not in query


def test_fecha_adquisicion_sin_fecha_no_aplica(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "", "fecha_adquisicion_op": "gte"})
    assert errors == []
    assert "fecha_adquisicion" not in query


def test_fecha_adquisicion_se_combina_con_vendedor(client):
    _f, query, errors, *_ = _ctx({"fecha_adquisicion": "2026-08-05", "fecha_adquisicion_op": "gte", "vendedor_id": "VEND01"})
    assert errors == []
    assert query["vendedor_id"] == "VEND01"
    assert query["fecha_adquisicion"] == {"$gte": "2026-08-05"}


def test_fecha_adquisicion_activa_has_filters(client):
    _f, _q, _e, _p, _l, _o, has_filters, _n = _ctx({"fecha_adquisicion": "2026-08-05", "fecha_adquisicion_op": "eq"})
    assert has_filters is True


def test_consultas_filtro_fecha_eq(client):
    boletas.update_many(
        {},
        {
            "$set": {
                "fecha_adquisicion": None,
                "vendedor_id": "",
                "estado": "disponible",
                "total_abonado": 0,
                "historial_pagos": [],
            }
        },
    )
    boletas.update_many({"_id": {"$in": [1, 2, 3]}}, {"$set": {"fecha_adquisicion": "2026-08-05"}})
    boletas.update_many({"_id": {"$in": [4, 5]}}, {"$set": {"fecha_adquisicion": "2026-08-06"}})
    boletas.update_many({"_id": {"$in": [6, 7]}}, {"$set": {"fecha_adquisicion": "2026-08-04"}})

    resp = client.get("/consultas?fecha_adquisicion=2026-08-05&fecha_adquisicion_op=eq")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for bid in (1, 2, 3):
        assert f"#{bid:04d}" in html
    for bid in (4, 5, 6, 7):
        assert f"#{bid:04d}" not in html


def test_consultas_filtro_fecha_gte(client):
    boletas.update_many({"_id": {"$in": [1, 2, 3]}}, {"$set": {"fecha_adquisicion": "2026-08-05"}})
    boletas.update_many({"_id": {"$in": [4, 5]}}, {"$set": {"fecha_adquisicion": "2026-08-06"}})
    boletas.update_many({"_id": {"$in": [6, 7]}}, {"$set": {"fecha_adquisicion": "2026-08-04"}})

    resp = client.get("/consultas?fecha_adquisicion=2026-08-05&fecha_adquisicion_op=gte")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for bid in (1, 2, 3, 4, 5):
        assert f"#{bid:04d}" in html
    for bid in (6, 7):
        assert f"#{bid:04d}" not in html


def test_consultas_filtro_fecha_lte(client):
    boletas.update_many({"_id": {"$in": [1, 2, 3]}}, {"$set": {"fecha_adquisicion": "2026-08-05"}})
    boletas.update_many({"_id": {"$in": [4, 5]}}, {"$set": {"fecha_adquisicion": "2026-08-06"}})
    boletas.update_many({"_id": {"$in": [6, 7]}}, {"$set": {"fecha_adquisicion": "2026-08-04"}})

    resp = client.get("/consultas?fecha_adquisicion=2026-08-05&fecha_adquisicion_op=lte")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for bid in (1, 2, 3, 6, 7):
        assert f"#{bid:04d}" in html
    for bid in (4, 5):
        assert f"#{bid:04d}" not in html


def test_consultas_filtro_fecha_excluye_boletas_sin_fecha(client):
    boletas.update_many({"_id": {"$in": [1, 2]}}, {"$set": {"fecha_adquisicion": "2026-08-05"}})

    resp = client.get("/consultas?fecha_adquisicion=2026-08-05&fecha_adquisicion_op=eq")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "#0001" in html
    assert "#0002" in html
    # Las boletas sin fecha de adquisición no deben aparecer.
    assert "#0003" not in html

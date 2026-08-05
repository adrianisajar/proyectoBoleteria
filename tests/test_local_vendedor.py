from database import boletas, facturas, vendedores
from motores.constants import VENDEDOR_LOCAL, VENDEDOR_LOCAL_LABEL
from motores.vendor_service import get_vendedores_snapshot


def _asignar_local(ids=(1, 2)):
    boletas.update_many({"_id": {"$in": list(ids)}}, {"$set": {"vendedor_id": VENDEDOR_LOCAL, "estado": "separada"}})


def _post_op(client, vendedor_id, operacion, boletas_str="", fecha="2026-07-20", boletas_fecha=None):
    data = {
        "vendedor_id": vendedor_id,
        "nombre": "",
        "telefono": "",
        "operacion": operacion,
        "boletas": boletas_str,
        "fecha_adquisicion": fecha,
    }
    if boletas_fecha:
        data["boletas_fecha[]"] = boletas_fecha
    return client.post("/vendedores", data=data)


def _post_factura_vendedor(client, vendedor_id, boletas_, montos, fecha="2026-07-30"):
    return client.post(
        "/facturas/nueva/vendedor",
        data={
            "vendedor_id": vendedor_id,
            "fecha": fecha,
            "boleta[]": boletas_,
            "monto[]": montos,
            "metodo[]": ["efectivo"] * len(boletas_),
            "referencia[]": [""] * len(boletas_),
            "banco[]": [""] * len(boletas_),
        },
    )


# ── listado del panel ───────────────────────────────────


def test_local_aparece_en_snapshot(client):
    _asignar_local((1, 2))
    lista, _resumen = get_vendedores_snapshot()
    local = next(v for v in lista if v["_id"] == VENDEDOR_LOCAL)
    assert local["es_local"] is True
    assert local["nombre"] == VENDEDOR_LOCAL_LABEL
    assert local["cantidad"] == 2
    assert local["comision"] == 0


def test_local_no_cuenta_en_resumen(client):
    _asignar_local((1, 2))
    _vendedores, resumen = get_vendedores_snapshot()
    assert resumen["total_vendedores"] == 0


# ── operaciones del panel ───────────────────────────────


def test_asignar_a_local(client):
    resp = _post_op(client, VENDEDOR_LOCAL, "asignar", boletas_str="0001, 0002")
    assert resp.status_code == 302
    b1 = boletas.find_one({"_id": 1})
    assert b1["vendedor_id"] == VENDEDOR_LOCAL
    assert b1["estado"] == "separada"
    assert vendedores.find_one({"_id": VENDEDOR_LOCAL}) is None


def test_asignar_a_local_no_crea_documento_vendedor(client):
    resp = _post_op(client, VENDEDOR_LOCAL, "asignar", boletas_str="0001")
    assert resp.status_code == 302
    assert vendedores.find_one({"_id": VENDEDOR_LOCAL}) is None


def test_quitar_de_local(client):
    _asignar_local((1, 2))
    boletas.update_many({"_id": {"$in": [1, 2]}}, {"$set": {"fecha_adquisicion": "2026-07-10"}})
    resp = _post_op(client, VENDEDOR_LOCAL, "quitar", boletas_str="0001, 0002")
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["vendedor_id"] == ""
    assert boletas.find_one({"_id": 1})["estado"] == "disponible"
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] is None


def test_registrar_fecha_en_local(client):
    _asignar_local((1, 2))
    resp = _post_op(client, VENDEDOR_LOCAL, "registrar_fecha_adquisicion", boletas_fecha=["0001", "0002"])
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] == "2026-07-20"
    assert boletas.find_one({"_id": 2})["fecha_adquisicion"] == "2026-07-20"


def test_registrar_fecha_local_rechaza_ajena(client):
    _asignar_local((1,))
    boletas.update_one({"_id": 5}, {"$set": {"vendedor_id": "OTRO", "estado": "asignada"}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": [5]})
    resp = _post_op(client, VENDEDOR_LOCAL, "registrar_fecha_adquisicion", boletas_fecha=["0001", "0005"])
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")
    assert boletas.find_one({"_id": 5})["fecha_adquisicion"] in (None, "")


def test_eliminar_local_bloqueado(client):
    _asignar_local((1,))
    resp = _post_op(client, VENDEDOR_LOCAL, "eliminar")
    assert resp.status_code == 200
    assert vendedores.find_one({"_id": VENDEDOR_LOCAL}) is None
    assert boletas.find_one({"_id": 1})["vendedor_id"] == VENDEDOR_LOCAL


def test_guardar_local_bloqueado(client):
    resp = _post_op(client, VENDEDOR_LOCAL, "guardar")
    assert resp.status_code == 200
    assert vendedores.find_one({"_id": VENDEDOR_LOCAL}) is None


# ── API ─────────────────────────────────────────────────


def test_api_vendedores_incluye_local(client):
    resp = client.get("/api/vendedores")
    assert resp.status_code == 200
    data = resp.get_json()
    assert any(v["_id"] == VENDEDOR_LOCAL for v in data)


def test_api_vendedor_local_boletas(client):
    _asignar_local((1, 2))
    resp = client.get(f"/api/vendedores/{VENDEDOR_LOCAL}/boletas")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert [b["numero"] for b in data["boletas"]] == ["0001", "0002"]


# ── factura de vendedor (recaudo) ───────────────────────


def test_factura_vendedor_local(client):
    _asignar_local((1, 2))
    resp = _post_factura_vendedor(client, VENDEDOR_LOCAL, ["0001", "0002"], ["70000", "70000"])
    assert resp.status_code == 302
    factura = facturas.find_one({"tipo": "vendedor"})
    assert factura is not None
    assert factura["vendedor_id"] == VENDEDOR_LOCAL
    assert factura["vendedor_nombre"] == VENDEDOR_LOCAL_LABEL
    assert factura["total_comision"] == 0
    assert boletas.find_one({"_id": 1})["estado"] == "pagada"
    assert boletas.find_one({"_id": 1})["vendedor_id"] == VENDEDOR_LOCAL
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")


def test_factura_vendedor_local_rechaza_ajena(client):
    _asignar_local((1,))
    resp = _post_factura_vendedor(client, VENDEDOR_LOCAL, ["0001", "0002"], ["70000", "70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0

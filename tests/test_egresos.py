import hashlib

from app import app as flask_app
from database import boletas, facturas, vendedores
from motores.constants import MOV_EGRESO, MOV_PAGO


def _post_egreso(
    client,
    vendedor_id="VEND01",
    boletas_=("0001",),
    valores=("20000",),
    metodos=None,
    referencias=None,
    bancos=None,
    fecha="2026-07-30",
    egreso_tipo="comision_vendedor",
):
    n = len(boletas_)
    if metodos is None:
        metodos = ["efectivo"] * n
    if referencias is None:
        referencias = [""] * n
    if bancos is None:
        bancos = [""] * n
    return client.post(
        "/facturas/egreso/nueva",
        data={
            "vendedor_id": vendedor_id,
            "fecha": fecha,
            "egreso_tipo": egreso_tipo,
            "boleta[]": list(boletas_),
            "valor[]": list(valores),
            "metodo[]": metodos,
            "referencia[]": referencias,
            "banco[]": bancos,
        },
    )


def test_egreso_crea_factura_y_movimiento_sin_tocar_saldo(client):
    boletas.update_one(
        {"_id": 1},
        {
            "$set": {
                "vendedor_id": "VEND01",
                "estado": "pagada",
                "total_abonado": 70000,
                "historial_movimientos": [{"tipo": MOV_PAGO, "fecha": "2026-07-01", "valor": 70000, "metodo": "efectivo", "factura_id": 50}],
            }
        },
    )
    resp = _post_egreso(client, boletas_=("0001",), valores=("20000",))
    assert resp.status_code == 302
    assert "/facturas/" in resp.headers["Location"]

    f = facturas.find_one({"tipo": "egreso"})
    assert f is not None
    assert f["egreso_tipo"] == "comision_vendedor"
    assert f["valor_total"] == 20000
    assert f["boletas"] == [1]

    b = boletas.find_one({"_id": 1})
    assert b["total_abonado"] == 70000
    assert b["estado"] == "pagada"
    tipos = [m["tipo"] for m in b["historial_movimientos"]]
    assert tipos.count(MOV_EGRESO) == 1
    egreso = b["historial_movimientos"][-1]
    assert egreso["tipo"] == MOV_EGRESO
    assert egreso["valor"] == 20000
    assert egreso["factura_id"] == f["_id"]


def test_egreso_multiples_boletas_una_fila(client):
    resp = _post_egreso(client, boletas_=("0001, 0002",), valores=("14000",))
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "egreso"})
    assert sorted(f["boletas"]) == [1, 2]
    assert f["valor_total"] == 28000
    for bid in (1, 2):
        tipos = [m["tipo"] for m in boletas.find_one({"_id": bid})["historial_movimientos"]]
        assert tipos.count(MOV_EGRESO) == 1


def test_egreso_caja_permitido(client_caja):
    resp = client_caja.get("/facturas/egreso")
    assert resp.status_code == 200
    resp = client_caja.get("/facturas/egreso/nueva")
    assert resp.status_code == 200


def test_egreso_caja_registra_operacion(client_caja):
    resp = _post_egreso(client_caja, boletas_=("0001",), valores=("10000",))
    assert resp.status_code == 302
    assert facturas.count_documents({"tipo": "egreso"}) == 1


def test_api_boletas_vendedor_egreso(client):
    vendedores.insert_one({"_id": "VEND01", "nombre": "Vendedor Uno"})
    boletas.update_one(
        {"_id": 3},
        {
            "$set": {
                "vendedor_id": "VEND01",
                "estado": "abonando",
                "total_abonado": 30000,
                "historial_movimientos": [
                    {"tipo": MOV_PAGO, "fecha": "2026-07-01", "valor": 30000, "metodo": "efectivo", "factura_id": 60},
                    {"tipo": MOV_EGRESO, "fecha": "2026-07-02", "valor": 10000, "metodo": "efectivo", "factura_id": 61},
                ],
            }
        },
    )
    boletas.update_one({"_id": 4}, {"$set": {"vendedor_id": "VEND01", "estado": "pagada", "total_abonado": 70000}})

    resp = client.get("/api/egresos/boletas/VEND01")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    nums = [b["numero"] for b in data["boletas"]]
    assert nums == [3, 4]
    b3 = next(b for b in data["boletas"] if b["numero"] == 3)
    assert b3["total_ingresado"] == 30000
    assert b3["total_egresado"] == 10000
    assert b3["tiene_egresos_previos"] is True
    b4 = next(b for b in data["boletas"] if b["numero"] == 4)
    assert b4["tiene_egresos_previos"] is False

    resp = client.get("/api/egresos/boletas/LOCAL")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    resp = client.get("/api/egresos/boletas/NO_EXISTE")
    assert resp.status_code == 404


def test_egreso_sin_vendedor(client):
    resp = _post_egreso(client, vendedor_id="")
    assert resp.status_code == 200
    assert facturas.count_documents({"tipo": "egreso"}) == 0


def test_egreso_valor_supera_maximo(client):
    resp = _post_egreso(client, boletas_=("0001",), valores=("80000",))
    assert resp.status_code == 200
    assert "supera el m\u00e1ximo" in resp.get_data(as_text=True)
    assert facturas.count_documents({"tipo": "egreso"}) == 0


def test_egreso_valor_cero_rechazado(client):
    resp = _post_egreso(client, boletas_=("0001",), valores=("0",))
    assert resp.status_code == 200
    assert "mayor que cero" in resp.get_data(as_text=True)
    assert facturas.count_documents({"tipo": "egreso"}) == 0


def test_egreso_transferencia_requiere_referencia(client):
    resp = _post_egreso(client, boletas_=("0001",), valores=("10000",), metodos=["transferencia"])
    assert resp.status_code == 200
    assert "referencia bancaria es obligatoria" in resp.get_data(as_text=True)
    assert facturas.count_documents({"tipo": "egreso"}) == 0


def test_egreso_boleta_inexistente(client):
    resp = _post_egreso(client, boletas_=("9999",), valores=("10000",))
    assert resp.status_code == 200
    assert "Boletas no encontradas" in resp.get_data(as_text=True)
    assert facturas.count_documents({"tipo": "egreso"}) == 0


def test_egreso_sin_filas(client):
    resp = _post_egreso(client, boletas_=("",), valores=("10000",))
    assert resp.status_code == 200
    assert facturas.count_documents({"tipo": "egreso"}) == 0


def test_egreso_list_renders(client):
    _post_egreso(client, boletas_=("0001",), valores=("10000",))
    resp = client.get("/facturas/egreso")
    assert resp.status_code == 200
    assert "Facturas de egreso" in resp.get_data(as_text=True)


def test_egreso_form_renders(client):
    resp = client.get("/facturas/egreso/nueva")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "vendedorSearch" in html
    assert "boletasBody" in html
    assert "Agregar fila" not in html


def test_ver_factura_egreso_renders(client):
    _post_egreso(client, boletas_=("0001",), valores=("10000",))
    f = facturas.find_one({"tipo": "egreso"})
    resp = client.get(f"/facturas/{f['_id']}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "COMPROBANTE DE EGRESO" in html
    assert "10,000" in html


def test_anular_egreso_retira_movimientos(client):
    _post_egreso(client, boletas_=("0001",), valores=("20000",))
    f = facturas.find_one({"tipo": "egreso"})
    fid = f["_id"]
    h = hashlib.sha256(f"{fid}:False:{flask_app.secret_key}".encode()).hexdigest()[:16]
    resp = client.post(f"/facturas/{fid}/anular", data={"motivo": "Comision mal calculada", "anulacion_hash": h})
    assert resp.status_code == 302

    b = boletas.find_one({"_id": 1})
    assert all(m["tipo"] != MOV_EGRESO for m in b["historial_movimientos"])
    assert b["total_abonado"] == 0
    f = facturas.find_one({"_id": fid})
    assert f["anulada"] is True

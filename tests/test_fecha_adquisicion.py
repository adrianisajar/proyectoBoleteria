from database import boletas, facturas, vendedores
from motores.payment_service import registrar_abono_lote


def _asignar_vendedor(vendedor_id="VEND01", ids=(1, 2)):
    vendedores.insert_one({"_id": vendedor_id, "nombre": "Ana Gomez", "telefono": "311", "boletas_asignadas": list(ids)})
    boletas.update_many({"_id": {"$in": list(ids)}}, {"$set": {"vendedor_id": vendedor_id, "estado": "asignada"}})


def _quitar(client, vendedor_id, boletas_str):
    return client.post(
        "/vendedores",
        data={
            "vendedor_id": vendedor_id,
            "nombre": "",
            "telefono": "",
            "operacion": "quitar",
            "boletas": boletas_str,
        },
    )


def _registrar_fecha(client, vendedor_id, fecha, boletas_list):
    return client.post(
        "/vendedores",
        data={
            "vendedor_id": vendedor_id,
            "nombre": "",
            "telefono": "",
            "operacion": "registrar_fecha_adquisicion",
            "boletas": "",
            "fecha_adquisicion": fecha,
        }
        | {"boletas_fecha[]": boletas_list},
    )


def _post_factura_cliente(client, boletas_, montos, fecha="2026-07-30"):
    return client.post(
        "/facturas/nueva/cliente",
        data={
            "nombre": "JUAN PEREZ",
            "telefono": "3001234567",
            "direccion": "CRA 1",
            "fecha": fecha,
            "boleta[]": boletas_,
            "monto[]": montos,
            "metodo[]": ["efectivo"] * len(boletas_),
            "referencia[]": [""] * len(boletas_),
            "banco[]": [""] * len(boletas_),
        },
    )


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


# ── registrar_abono_lote ───────────────────────────────


def test_registrar_abono_lote_no_registra_fecha(client):
    form = {"fecha": "2026-07-30", "metodo": "efectivo"}
    registrar_abono_lote([1, 2], form, 30000)
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")
    assert boletas.find_one({"_id": 2})["fecha_adquisicion"] in (None, "")


def test_registrar_abono_lote_no_sobreescribe_fecha(client):
    boletas.update_one({"_id": 1}, {"$set": {"fecha_adquisicion": "2026-07-01"}})
    form = {"fecha": "2026-07-30", "metodo": "efectivo"}
    registrar_abono_lote([1], form, 30000)
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] == "2026-07-01"


def test_registrar_abono_lote_deja_vacio_sin_registrar(client):
    boletas.update_one({"_id": 1}, {"$set": {"fecha_adquisicion": ""}})
    form = {"fecha": "2026-08-01", "metodo": "efectivo"}
    registrar_abono_lote([1], form, 30000)
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")


# ── factura de cliente ─────────────────────────────────


def test_factura_cliente_no_registra_fecha(client):
    resp = _post_factura_cliente(client, ["0010", "0011"], ["70000", "70000"])
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] in (None, "")
    assert boletas.find_one({"_id": 11})["fecha_adquisicion"] in (None, "")


def test_factura_cliente_no_sobreescribe_fecha_existente(client):
    boletas.update_one({"_id": 10}, {"$set": {"fecha_adquisicion": "2026-07-01"}})
    resp = _post_factura_cliente(client, ["0010"], ["70000"])
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] == "2026-07-01"


def test_factura_cliente_fecha_de_factura_no_modifica_boleta(client):
    resp = _post_factura_cliente(client, ["0010"], ["70000"], fecha="2026-06-15")
    assert resp.status_code == 302
    assert facturas.find_one({"tipo": "cliente"}) is not None
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] in (None, "")


# ── factura de vendedor ────────────────────────────────


def test_factura_vendedor_no_registra_fecha(client):
    _asignar_vendedor(ids=(1, 2))
    resp = _post_factura_vendedor(client, "VEND01", ["0001", "0002"], ["70000", "70000"])
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")
    assert boletas.find_one({"_id": 2})["fecha_adquisicion"] in (None, "")


# ── devolución de boletas (quitar / eliminar vendedor) ──


def test_quitar_limpia_fecha_adquisicion(client):
    _asignar_vendedor(ids=(1, 2))
    boletas.update_many({"_id": {"$in": [1, 2]}}, {"$set": {"fecha_adquisicion": "2026-07-30"}})
    resp = _quitar(client, "VEND01", "0001, 0002")
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["vendedor_id"] == ""
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] is None
    assert boletas.find_one({"_id": 2})["fecha_adquisicion"] is None


def test_eliminar_vendedor_limpia_fecha_adquisicion(client):
    _asignar_vendedor(ids=(1, 2))
    boletas.update_many({"_id": {"$in": [1, 2]}}, {"$set": {"fecha_adquisicion": "2026-07-30"}})
    resp = client.post(
        "/vendedores",
        data={"vendedor_id": "VEND01", "nombre": "", "telefono": "", "operacion": "eliminar", "boletas": ""},
    )
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["vendedor_id"] == ""
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] is None


# ── operación "registrar fecha de adquisición" ─────────


def test_registrar_fecha_ok(client):
    _asignar_vendedor(ids=(1, 2))
    resp = _registrar_fecha(client, "VEND01", "2026-07-20", ["0001", "0002"])
    assert resp.status_code == 302
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] == "2026-07-20"
    assert boletas.find_one({"_id": 2})["fecha_adquisicion"] == "2026-07-20"


def test_registrar_fecha_rechaza_boleta_ajena(client):
    _asignar_vendedor(ids=(1,))
    boletas.update_one({"_id": 5}, {"$set": {"vendedor_id": "OTRO", "estado": "asignada"}})
    vendedores.insert_one({"_id": "OTRO", "nombre": "Otro", "boletas_asignadas": [5]})
    resp = _registrar_fecha(client, "VEND01", "2026-07-20", ["0001", "0005"])
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")
    assert boletas.find_one({"_id": 5})["fecha_adquisicion"] in (None, "")


def test_registrar_fecha_rechaza_fecha_futura(client):
    _asignar_vendedor(ids=(1,))
    resp = _registrar_fecha(client, "VEND01", "2099-01-01", ["0001"])
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")


def test_registrar_fecha_requiere_seleccion(client):
    _asignar_vendedor(ids=(1,))
    resp = _registrar_fecha(client, "VEND01", "2026-07-20", [])
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")


def test_registrar_fecha_requiere_fecha(client):
    _asignar_vendedor(ids=(1,))
    resp = _registrar_fecha(client, "VEND01", "", ["0001"])
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 1})["fecha_adquisicion"] in (None, "")


# ── compradores (ingreso rápido) ───────────────────────


def test_compradores_guarda_cliente_sin_tocar_fecha(client):
    resp = client.post(
        "/compradores/rapido",
        json={"rows": [{"boleta": 10, "nombre": "Pedro", "telefono": "300", "direccion": "CRA 2", "fecha_adquisicion": ""}]},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert boletas.find_one({"_id": 10})["cliente"]["nombre"] == "PEDRO"
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] in (None, "")


def test_compradores_solo_fecha_sin_datos_se_ignora(client):
    resp = client.post(
        "/compradores/rapido",
        json={"rows": [{"boleta": 10, "nombre": "", "telefono": "", "direccion": "", "fecha_adquisicion": "2026-07-10"}]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert 10 in data["sin_datos"]
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] in (None, "")


def test_compradores_fila_sin_datos_se_ignora(client):
    resp = client.post(
        "/compradores/rapido",
        json={"rows": [{"boleta": 10, "nombre": "", "telefono": "", "direccion": ""}]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert 10 in data["sin_datos"]
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] in (None, "")


def test_compradores_no_sobreescribe_fecha_existente(client):
    boletas.update_one({"_id": 10}, {"$set": {"fecha_adquisicion": "2026-07-01"}})
    resp = client.post(
        "/compradores/rapido",
        json={"rows": [{"boleta": 10, "nombre": "Pedro", "telefono": "", "direccion": ""}]},
    )
    assert resp.status_code == 200
    assert boletas.find_one({"_id": 10})["fecha_adquisicion"] == "2026-07-01"

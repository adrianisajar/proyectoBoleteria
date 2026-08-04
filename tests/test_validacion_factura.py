from database import boletas

VALOR_BOLETA = 70000


def _validar(client, payload):
    return client.post("/api/validar-factura", json=payload)


def test_validar_factura_cliente_ok(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "telefono": "3001234567",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": str(VALOR_BOLETA), "metodo": "efectivo"}],
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["can_submit"] is True
    assert data["total_errores"] == 0


def test_validar_factura_nombre_obligatorio(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": "30000", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert "El nombre del cliente es obligatorio." in data["campo_errores"]["nombre"]


def test_validar_factura_fecha_futura(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2099-01-01",
            "filas": [{"boletas": "0010", "monto": "30000", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert len(data["campo_errores"]["fecha"]) == 1


def test_validar_factura_boleta_inexistente(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "9999", "monto": "30000", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert any("#9999 no existe." in e for e in data["filas"][0]["boletas"])


def test_validar_factura_boleta_duplicada(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2026-07-30",
            "filas": [
                {"boletas": "0010", "monto": "30000", "metodo": "efectivo"},
                {"boletas": "0010", "monto": "30000", "metodo": "efectivo"},
            ],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert all("est\u00e1 repetida" in e for r in data["filas"] for e in r["boletas"])


def test_validar_factura_boleta_pagada(client):
    boletas.update_one({"_id": 20}, {"$set": {"estado": "pagada", "total_abonado": VALOR_BOLETA}})
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0020", "monto": "30000", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert any("#0020 ya est\u00e1 pagada" in e for e in data["filas"][0]["boletas"])


def test_validar_factura_monto_excede_valor(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": "999999", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert len(data["filas"][0]["monto"]) == 1


def test_validar_factura_transferencia_sin_referencia(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": "30000", "metodo": "transferencia", "referencia": "", "banco": "BANCOLOMBIA"}],
        },
    )
    data = resp.get_json()
    assert len(data["filas"][0]["referencia"]) == 1
    assert len(data["filas"][0]["banco"]) == 0


def test_validar_factura_transferencia_sin_banco(client):
    resp = _validar(
        client,
        {
            "tipo": "cliente",
            "nombre": "JUAN PEREZ",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": "30000", "metodo": "transferencia", "referencia": "REF-X", "banco": ""}],
        },
    )
    data = resp.get_json()
    assert len(data["filas"][0]["banco"]) == 1
    assert "El banco es obligatorio para transferencia." in data["filas"][0]["banco"]


def test_validar_factura_vendedor_requiere_vendedor(client):
    resp = _validar(
        client,
        {
            "tipo": "vendedor",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": "30000", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert "Debe seleccionar un vendedor." in data["campo_errores"]["vendedor"]


def test_validar_factura_vendedor_ok_local(client):
    boletas.update_one({"_id": 10}, {"$set": {"vendedor_id": "LOCAL"}})
    resp = _validar(
        client,
        {
            "tipo": "vendedor",
            "vendedor_id": "LOCAL",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010", "monto": "30000", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is True
    assert data["total_errores"] == 0


def test_validar_factura_vendedor_monto_multiplica(client):
    resp = _validar(
        client,
        {
            "tipo": "vendedor",
            "vendedor_id": "LOCAL",
            "fecha": "2026-07-30",
            "filas": [{"boletas": "0010, 0011, 0012", "monto": "999999", "metodo": "efectivo"}],
        },
    )
    data = resp.get_json()
    assert data["can_submit"] is False
    assert len(data["filas"][0]["monto"]) == 1


def test_validar_factura_sin_filas(client):
    resp = _validar(client, {"tipo": "cliente", "nombre": "JUAN PEREZ", "fecha": "2026-07-30", "filas": []})
    data = resp.get_json()
    assert data["can_submit"] is False
    assert "Ingrese al menos una boleta." in data["campo_errores"]["boletas"]

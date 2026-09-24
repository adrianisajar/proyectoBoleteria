import hashlib
import hmac
from datetime import datetime, timedelta

from app import app as flask_app
from database import boletas, configuracion, facturas


def _post_factura(client, boletas_, montos, metodos=None, referencias=None, bancos=None, nombre="JUAN PEREZ", telefono="3001234567", fecha="2026-07-30"):
    if metodos is None:
        metodos = ["efectivo"] * len(boletas_)
    if referencias is None:
        referencias = [""] * len(boletas_)
    if bancos is None:
        bancos = [""] * len(boletas_)
    return client.post(
        "/facturas/nueva/cliente",
        data={
            "nombre": nombre,
            "telefono": telefono,
            "direccion": "CRA 1",
            "fecha": fecha,
            "boleta[]": boletas_,
            "monto[]": montos,
            "metodo[]": metodos,
            "referencia[]": referencias,
            "banco[]": bancos,
        },
    )


def test_factura_cliente_pago_total(client):
    resp = _post_factura(client, ["0010"], ["70000"])
    assert resp.status_code == 302
    assert "/facturas/" in resp.headers["Location"]

    f = facturas.find_one({"tipo": "cliente"})
    assert f is not None
    assert f["valor_total"] == 70000
    assert f["cliente"]["nombre"] == "JUAN PEREZ"
    assert f["boletas"] == [10]

    b = boletas.find_one({"_id": 10})
    assert b["estado"] == "pagada"
    assert b["total_abonado"] == 70000
    assert b["cliente"]["nombre"] == "JUAN PEREZ"
    assert b["vendedor_id"] == "LOCAL"
    assert f["creada_en"] is not None


def test_limpieza_no_borra_factura_pendiente_con_movimientos(client):
    factura_id = 456
    facturas.insert_one(
        {
            "_id": factura_id,
            "tipo": "cliente",
            "estado": "pendiente",
            "creada_en": datetime.now() - timedelta(minutes=10),
            "fecha": datetime.now(),
        }
    )
    boletas.update_one(
        {"_id": 10},
        {"$set": {"historial_movimientos": [{"tipo": "pago", "factura_id": factura_id, "valor": 10000}]}},
    )

    assert client.get("/facturas").status_code == 200
    assert facturas.find_one({"_id": factura_id}) is not None


def test_limpieza_borra_pendiente_antigua_sin_movimientos(client):
    factura_id = 457
    facturas.insert_one(
        {
            "_id": factura_id,
            "tipo": "cliente",
            "estado": "pendiente",
            "creada_en": datetime.now() - timedelta(minutes=10),
            "fecha": datetime.now(),
        }
    )

    from motores.facturacion import _ULTIMA_LIMPIEZA_PENDIENTES, _limpiar_facturas_pendientes
    _ULTIMA_LIMPIEZA_PENDIENTES[0] = 0.0
    _limpiar_facturas_pendientes()
    assert facturas.find_one({"_id": factura_id}) is None


def test_factura_cliente_abono_parcial(client):
    resp = _post_factura(client, ["0010"], ["30000"])
    assert resp.status_code == 302

    b = boletas.find_one({"_id": 10})
    assert b["estado"] == "abonando"
    assert b["total_abonado"] == 30000
    f = facturas.find_one({"tipo": "cliente"})
    assert f["valor_total"] == 30000


def test_factura_cliente_multiples_boletas(client):
    resp = _post_factura(client, ["0010", "0011", "0012"], ["30000", "40000", "70000"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "cliente"})
    assert f["valor_total"] == 140000
    assert f["boletas"] == [10, 11, 12]
    assert boletas.find_one({"_id": 10})["total_abonado"] == 30000
    assert boletas.find_one({"_id": 11})["total_abonado"] == 40000
    assert boletas.find_one({"_id": 12})["total_abonado"] == 70000


def test_factura_cliente_boleta_duplicada_rechazada(client):
    resp = _post_factura(client, ["0010", "0010"], ["30000", "30000"])
    assert resp.status_code == 200
    assert "duplicadas" in resp.get_data(as_text=True).lower()
    assert facturas.count_documents({}) == 0
    assert boletas.find_one({"_id": 10})["total_abonado"] == 0


def test_factura_cliente_boleta_inexistente(client):
    resp = _post_factura(client, ["9999"], ["70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_boleta_incompleta_rechazada(client):
    resp = _post_factura(client, ["0"], ["70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0
    assert "4 d\u00edgitos" in resp.get_data(as_text=True)


def test_factura_cliente_fecha_futura(client):
    resp = _post_factura(client, ["0010"], ["70000"], fecha="2099-01-01")
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_nombre_obligatorio(client):
    resp = _post_factura(client, ["0010"], ["70000"], nombre="")
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_monto_excede_valor(client):
    resp = _post_factura(client, ["0010"], ["999999"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_monto_vacio_rechazado(client):
    resp = _post_factura(client, ["0010"], [""])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0
    assert boletas.find_one({"_id": 10})["total_abonado"] == 0


def test_factura_cliente_monto_cero_rechazado(client):
    resp = _post_factura(client, ["0010"], ["0"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0
    assert boletas.find_one({"_id": 10})["total_abonado"] == 0


def test_factura_cliente_una_fila_sin_monto_rechaza_toda(client):
    resp = _post_factura(client, ["0010", "0011"], ["30000", ""])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0
    assert boletas.find_one({"_id": 10})["total_abonado"] == 0
    assert boletas.find_one({"_id": 11})["total_abonado"] == 0


def test_factura_cliente_boleta_pagada_rechazada(client):
    boletas.update_one({"_id": 20}, {"$set": {"estado": "pagada", "total_abonado": 70000}})
    resp = _post_factura(client, ["0020"], ["70000"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_transferencia_sin_referencia(client):
    resp = _post_factura(client, ["0010"], ["30000"], metodos=["transferencia"], referencias=[""], bancos=["BANCOLOMBIA"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_factura_cliente_transferencia_ok(client):
    resp = _post_factura(client, ["0010"], ["30000"], metodos=["transferencia"], referencias=["REF-ABC-1"], bancos=["BANCOLOMBIA"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "cliente"})
    assert f["valor_total"] == 30000
    assert f["detalle"][0]["metodo"] == "transferencia"
    assert f["detalle"][0]["referencia"] == "REF-ABC-1"


def test_factura_cliente_referencia_duplicada_en_db(client):
    boletas.update_one(
        {"_id": 21},
        {
            "$set": {
                "estado": "abonando",
                "total_abonado": 30000,
                "historial_movimientos": [{"fecha": "2026-07-01", "valor": 30000, "metodo": "transferencia", "referencia": "REF-DUP", "banco": "DAVIVIENDA"}],
            }
        },
    )
    resp = _post_factura(client, ["0010"], ["30000"], metodos=["transferencia"], referencias=["REF-DUP"], bancos=["DAVIVIENDA"])
    assert resp.status_code == 200
    assert facturas.count_documents({}) == 0


def test_ver_factura_cliente_renders(client):
    _post_factura(client, ["0010"], ["70000"])
    f = facturas.find_one({"tipo": "cliente"})
    resp = client.get(f"/facturas/{f['_id']}")
    assert resp.status_code == 200


def test_factura_cliente_almacena_snapshot_boletas_info(client):
    resp = _post_factura(client, ["0010"], ["30000"])
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "cliente"})
    info = f["boletas_info"]["10"]
    assert info["valor_boleta"] == 70000
    assert info["vendedor_id"] == "LOCAL"
    assert "total_abonado" not in info
    assert "saldo_pendiente" not in info
    assert "estado" not in info


def test_ver_factura_cliente_estatica_pese_a_pagos_nuevos(client):
    _post_factura(client, ["0010"], ["70000"])
    f = facturas.find_one({"tipo": "cliente"})
    fid = f["_id"]

    boletas.update_one(
        {"_id": 10},
        {
            "$set": {
                "historial_movimientos": [
                    {"fecha": "2026-07-30", "valor": 70000, "metodo": "efectivo", "factura_id": fid},
                    {"fecha": "2026-07-29", "valor": 70000, "metodo": "efectivo"},
                ],
                "total_abonado": 140000,
                "estado": "pagada",
            }
        },
    )

    resp = client.get(f"/facturas/{fid}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "70,000" in html
    assert "140,000" not in html
    stored = facturas.find_one({"_id": fid})
    assert "total_abonado" not in stored["boletas_info"]["10"]
    assert stored["valor_total"] == 70000


def test_ver_factura_legacy_sin_snapshot_se_respalda(client):
    boletas.update_one(
        {"_id": 30},
        {
            "$set": {
                "vendedor_id": "LOCAL",
                "total_abonado": 70000,
                "estado": "pagada",
                "historial_movimientos": [{"fecha": "2026-07-01", "valor": 70000, "metodo": "efectivo", "factura_id": 77}],
            }
        },
    )
    facturas.insert_one(
        {
            "_id": 77,
            "tipo": "cliente",
            "fecha": datetime(2026, 7, 1),
            "boletas": [30],
            "detalle": [{"boleta": 30, "fecha": "2026-07-01", "valor": 70000, "metodo": "efectivo"}],
            "valor_total": 70000,
            "cliente": {"nombre": "LEGACY", "telefono": "", "direccion": ""},
            "vendedor_id": "LOCAL",
            "vendedor_nombre": "LOCAL",
        }
    )

    resp = client.get("/facturas/77")
    assert resp.status_code == 200
    f = facturas.find_one({"_id": 77})
    assert f["boletas_info"]["30"]["valor_boleta"] == 70000
    assert f["boletas_info"]["30"]["vendedor_id"] == "LOCAL"
    assert "total_abonado" not in f["boletas_info"]["30"]


def test_anular_factura_cliente(client):
    _post_factura(client, ["0010"], ["70000"])
    f = facturas.find_one({"tipo": "cliente"})
    fid = f["_id"]
    h = hmac.new(flask_app.secret_key.encode(), f"{fid}:False".encode(), hashlib.sha256).hexdigest()[:16]
    resp = client.post(f"/facturas/{fid}/anular", data={"motivo": "Error de digitacion", "anulacion_hash": h})
    assert resp.status_code == 302
    b = boletas.find_one({"_id": 10})
    assert b["total_abonado"] == 0
    assert b["estado"] == "separada"
    f = facturas.find_one({"_id": fid})
    assert f["anulada"] is True


def test_factura_id_salta_colision_por_restauracion(client):
    # Simula una base restaurada: la colección ya tiene facturas con _id >= contador.
    facturas.insert_one({"_id": 4, "tipo": "cliente"})
    configuracion.update_one({"_id": "rifa"}, {"$set": {"factura_counter": 3}})

    resp = _post_factura(client, ["0010"], ["70000"])
    assert resp.status_code == 302

    f = facturas.find_one({"tipo": "cliente", "_id": {"$gt": 4}})
    assert f is not None
    assert f["_id"] > 4
    assert facturas.count_documents({"_id": f["_id"]}) == 1
    assert configuracion.find_one({"_id": "rifa"})["factura_counter"] >= f["_id"]


def test_facturas_cliente_ordena_por_cliente(client):
    _post_factura(client, ["0010"], ["30000"], nombre="ZULU")
    _post_factura(client, ["0011"], ["30000"], nombre="ALFA")
    resp = client.get("/facturas/cliente?sort_by=cliente.nombre&sort_dir=asc")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert html.index("ALFA") < html.index("ZULU")


def _marcar_pagada(bid, total=70000):
    boletas.update_one(
        {"_id": bid},
        {
            "$set": {
                "estado": "pagada",
                "total_abonado": total,
                "historial_movimientos": [{"fecha": "2026-07-01", "valor": total, "metodo": "efectivo"}],
            }
        },
    )


def test_factura_cliente_pagada_sin_confirmar_pide_confirmacion(client):
    _marcar_pagada(10)
    resp = _post_factura(client, ["0010"], ["20000"])
    assert resp.status_code == 200
    html = resp.get_data(as_text=True).lower()
    assert "confirme" in html
    assert "excedente" in html
    assert facturas.count_documents({"tipo": "cliente"}) == 0
    assert boletas.find_one({"_id": 10})["total_abonado"] == 70000


def test_factura_cliente_pagada_confirmada_registra_excedente(client):
    _marcar_pagada(10)
    resp = client.post(
        "/facturas/nueva/cliente",
        data={
            "nombre": "JUAN PEREZ",
            "telefono": "3001234567",
            "direccion": "CRA 1",
            "fecha": "2026-07-30",
            "boleta[]": ["0010"],
            "monto[]": ["20000"],
            "metodo[]": ["efectivo"],
            "referencia[]": [""],
            "banco[]": [""],
            "confirmar_pagadas": "1",
        },
    )
    assert resp.status_code == 302
    f = facturas.find_one({"tipo": "cliente"})
    assert f is not None
    assert f["valor_total"] == 20000
    b = boletas.find_one({"_id": 10})
    assert b["total_abonado"] == 90000
    assert b["estado"] == "pagada"


def test_factura_cliente_abonos_acumulados_superan_valor_permitido(client):
    for _ in range(4):
        resp = _post_factura(client, ["0010"], ["20000"])
        assert resp.status_code == 302
    b = boletas.find_one({"_id": 10})
    assert b["total_abonado"] == 80000
    assert b["estado"] == "pagada"
    assert facturas.count_documents({"tipo": "cliente"}) == 4


def test_factura_cliente_acumulado_puede_exceder_valor(client):
    boletas.update_one(
        {"_id": 10},
        {
            "$set": {
                "estado": "abonando",
                "total_abonado": 60000,
                "historial_movimientos": [{"fecha": "2026-07-01", "valor": 60000, "metodo": "efectivo"}],
            }
        },
    )
    resp = _post_factura(client, ["0010"], ["30000"])
    assert resp.status_code == 302
    b = boletas.find_one({"_id": 10})
    assert b["total_abonado"] == 90000
    assert b["estado"] == "pagada"


def test_factura_cliente_pago_individual_supera_valor_rechazado(client):
    boletas.update_one(
        {"_id": 10},
        {
            "$set": {
                "estado": "abonando",
                "total_abonado": 10000,
                "historial_movimientos": [{"fecha": "2026-07-01", "valor": 10000, "metodo": "efectivo"}],
            }
        },
    )
    resp = _post_factura(client, ["0010"], ["80000"])
    assert resp.status_code == 200
    assert "supera el valor de la boleta" in resp.get_data(as_text=True)
    assert facturas.count_documents({"tipo": "cliente"}) == 0
    assert boletas.find_one({"_id": 10})["total_abonado"] == 10000

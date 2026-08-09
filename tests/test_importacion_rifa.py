import io

import pytest

from database import boletas, vendedores
from motores.excel_export import make_xlsx_response
from motores.excel_service import importar_modelo_rifa, modelo_rifa_report_rows

HEADERS = [
    "NUMERO DE BOLETA",
    "TOTAL ABONO",
    "FECHA ADQUISICION",
    "VENDEDOR (A)",
    "COMPRADOR(A)",
    "DIRECCION",
    "TELEFONO",
]


def _xlsx(headers, rows):
    return make_xlsx_response("t", headers, rows).data


def _importar(headers, rows):
    return importar_modelo_rifa(io.BytesIO(_xlsx(headers, rows)))


def _fila_boleta(numero, vendedor="VEND. ALFA", abono=0):
    return [f"{numero:04d}", abono, "", vendedor, "", "", ""]


def test_importacion_asigna_boletas_y_estado(client):
    rows = [_fila_boleta(n, "VEND. ALFA") for n in range(1, 6)]
    summary = _importar(HEADERS, rows)

    assert summary["boletas_asignadas"] == 5
    assert summary["boletas_actualizadas"] == 5
    assert summary["vendedores"] == 1
    assert summary["boletas_inexistentes"] == []
    alfa = vendedores.find_one({"nombre": "ALFA"})
    assert alfa["_id"] == "VEND_0001"
    for n in range(1, 6):
        b = boletas.find_one({"_id": n})
        assert b["vendedor_id"] == "VEND_0001"
        assert b["estado"] == "asignada"
    assert sorted(alfa["boletas_asignadas"]) == [1, 2, 3, 4, 5]


def test_reimportacion_no_deja_boletas_asignadas_obsoletas(client):
    rows1 = [_fila_boleta(n, "VEND. ALFA") for n in range(1, 11)]
    _importar(HEADERS, rows1)

    rows2 = [_fila_boleta(n, "VEND. BETA") for n in range(1, 11)]
    _importar(HEADERS, rows2)

    alfa = vendedores.find_one({"nombre": "ALFA"})
    beta = vendedores.find_one({"nombre": "BETA"})
    assert alfa["_id"] == "VEND_0001"
    assert beta["_id"] == "VEND_0002"
    assert sorted((alfa or {}).get("boletas_asignadas") or []) == []
    assert sorted(beta["boletas_asignadas"]) == list(range(1, 11))
    assert boletas.find_one({"_id": 1})["vendedor_id"] == "VEND_0002"


def test_importacion_reasigna_boletas_de_otro_vendedor(client):
    rows1 = [_fila_boleta(n, "VEND. ALFA") for n in (1, 2, 3)]
    rows2 = [_fila_boleta(n, "VEND. BETA") for n in (1, 2)]
    _importar(HEADERS, rows1)
    _importar(HEADERS, rows2)

    alfa = vendedores.find_one({"nombre": "ALFA"})
    beta = vendedores.find_one({"nombre": "BETA"})
    assert alfa["_id"] == "VEND_0001"
    assert beta["_id"] == "VEND_0002"
    assert sorted(alfa["boletas_asignadas"]) == [3]
    assert sorted(beta["boletas_asignadas"]) == [1, 2]
    assert boletas.find_one({"_id": 3})["vendedor_id"] == "VEND_0001"


def test_importacion_bloquea_reasignar_boleta_con_pagos(client):
    boletas.update_one(
        {"_id": 5},
        {
            "$set": {
                "total_abonado": 70000,
                "estado": "pagada",
                "historial_movimientos": [{"tipo": "pago", "valor": 70000, "metodo": "efectivo"}],
            }
        },
    )
    boletas.update_one({"_id": 5}, {"$set": {"vendedor_id": ""}})

    rows = [_fila_boleta(n, "VEND. GAMMA") for n in range(1, 6)]
    with pytest.raises(ValueError, match="pagos"):
        _importar(HEADERS, rows)

    assert boletas.find_one({"_id": 5})["vendedor_id"] == ""
    assert vendedores.count_documents({"nombre": "GAMMA"}) == 0


def test_importacion_permite_mantener_vendedor_en_boleta_pagada(client):
    vendedores.insert_one({"_id": "GAMMA", "nombre": "GAMMA", "boletas_asignadas": [5], "telefono": ""})
    boletas.update_many(
        {"_id": 5},
        {
            "$set": {
                "vendedor_id": "GAMMA",
                "total_abonado": 70000,
                "estado": "pagada",
                "historial_movimientos": [{"tipo": "pago", "valor": 70000, "metodo": "efectivo"}],
            }
        },
    )

    rows = [_fila_boleta(n, "VEND. GAMMA") for n in range(1, 6)]
    _importar(HEADERS, rows)

    assert boletas.find_one({"_id": 5})["vendedor_id"] == "GAMMA"
    assert sorted(vendedores.find_one({"_id": "GAMMA"})["boletas_asignadas"]) == [1, 2, 3, 4, 5]


def test_importacion_omite_boletas_inexistentes(client):
    rows = [_fila_boleta(n, "VEND. DELTA") for n in range(1, 11)]
    rows.append(["0555", 0, "", "VEND. DELTA", "", "", ""])
    summary = _importar(HEADERS, rows)

    assert summary["boletas_inexistentes"] == [555]
    assert summary["boletas_asignadas"] == 10
    delta = vendedores.find_one({"nombre": "DELTA"})
    assert delta["_id"] == "VEND_0001"
    assert sorted(delta["boletas_asignadas"]) == list(range(1, 11))
    assert 555 not in delta["boletas_asignadas"]
    assert boletas.count_documents({"_id": 555}) == 0


def test_roundtrip_export_import_no_duplica_vendedor(client):
    vendedores.insert_one({"_id": "JOSE", "nombre": "JOSE PEREZ", "boletas_asignadas": [1, 2, 3], "telefono": ""})
    boletas.update_many({"_id": {"$in": [1, 2, 3]}}, {"$set": {"vendedor_id": "JOSE"}})

    headers, rows = modelo_rifa_report_rows()
    assert "VEND. JOSE PEREZ" in {r[3] for r in rows}

    _importar(headers, rows)

    vids = [v["_id"] for v in vendedores.find({}, {"_id": 1})]
    assert "JOSE_PEREZ" not in vids
    assert boletas.find_one({"_id": 1})["vendedor_id"] == "JOSE"


def test_roundtrip_export_import_no_crea_vendedor_sin_registrar(client):
    boletas.update_many({"_id": {"$in": [10, 11, 12]}}, {"$set": {"vendedor_id": ""}})

    headers, rows = modelo_rifa_report_rows()
    _importar(headers, rows)

    assert vendedores.count_documents({"_id": "SIN_REGISTRAR"}) == 0
    assert boletas.find_one({"_id": 10})["vendedor_id"] == ""


def test_doble_importacion_mismo_archivo_idempotente(client):
    rows = [_fila_boleta(n, "VEND. EPSILON") for n in range(1, 6)]
    _importar(HEADERS, rows)
    _importar(HEADERS, rows)

    epsilon = vendedores.find_one({"nombre": "EPSILON"})
    assert epsilon["_id"] == "VEND_0001"
    assert sorted(epsilon["boletas_asignadas"]) == [1, 2, 3, 4, 5]
    assert vendedores.count_documents({"nombre": "EPSILON"}) == 1


def test_importacion_busca_columnas_por_header(client):
    headers_ordenados = [
        "COMPRADOR(A)",
        "NUMERO DE BOLETA",
        "DIRECCION",
        "VENDEDOR (A)",
        "TELEFONO",
    ]
    rows = [["", f"{n:04d}", "", "VEND. OMEGA", ""] for n in range(1, 4)]
    _importar(headers_ordenados, rows)

    omega = vendedores.find_one({"nombre": "OMEGA"})
    assert omega["_id"] == "VEND_0001"
    assert sorted(omega["boletas_asignadas"]) == [1, 2, 3]
    assert boletas.find_one({"_id": 2})["vendedor_id"] == "VEND_0001"


def test_importacion_rechaza_archivo_sin_columnas_requeridas(client):
    rows = [["0001", "VEND. ALFA"]]
    with pytest.raises(ValueError, match="Faltan columnas"):
        _importar(["NUMERO DE BOLETA"], rows)

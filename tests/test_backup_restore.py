import io
import zipfile

from bson import json_util
from conftest import ADMIN_PASSWORD

from database import boletas, vendedores
from motores.shared import configuracion, facturas, rifas, traslados, usuarios

_COLECCIONES = [
    ("boletas", boletas),
    ("vendedores", vendedores),
    ("facturas", facturas),
    ("rifas", rifas),
    ("configuracion", configuracion),
    ("usuarios", usuarios),
    ("traslados", traslados),
]


def _zip_backup(data: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json_util.dumps(data, ensure_ascii=False))
    return buf.getvalue()


def _restaurar(client, data: dict, follow: bool = False):
    return client.post(
        "/backup",
        data={"accion": "importar", "archivo": (io.BytesIO(_zip_backup(data)), "backup.zip"), "clave_admin": ADMIN_PASSWORD},
        follow_redirects=follow,
    )


def _backup_estado_actual() -> dict:
    """Snapshot the live seeded state, like the export endpoint does."""
    return {nombre: list(col.find({})) for nombre, col in _COLECCIONES if col is not None}


def _backup_valido() -> dict:
    return {
        "boletas": [
            {
                "_id": 1,
                "vendedor_id": "JOSE",
                "cliente": {"nombre": "", "telefono": "", "direccion": ""},
                "estado": "asignada",
                "total_abonado": 0,
                "historial_movimientos": [],
                "fecha_adquisicion": None,
            },
            {
                "_id": 2,
                "vendedor_id": "",
                "cliente": {"nombre": "", "telefono": "", "direccion": ""},
                "estado": "disponible",
                "total_abonado": 0,
                "historial_movimientos": [],
                "fecha_adquisicion": None,
            },
            {
                "_id": 3,
                "vendedor_id": "",
                "cliente": {"nombre": "", "telefono": "", "direccion": ""},
                "estado": "pagada",
                "total_abonado": 70000,
                "historial_movimientos": [{"tipo": "pago", "valor": 70000, "metodo": "efectivo"}],
                "fecha_adquisicion": None,
            },
        ],
        "vendedores": [{"_id": "JOSE", "nombre": "JOSE", "telefono": "", "boletas_asignadas": [1]}],
        "facturas": [],
        "rifas": [],
        "configuracion": [{"_id": "rifa", "factura_counter": 0, "traslado_counter": 0}],
        "usuarios": [],
        "traslados": [],
    }


def test_restore_respaldo_valido(client):
    resp = _restaurar(client, _backup_estado_actual())
    assert resp.status_code == 302
    assert boletas.count_documents({}) == 500
    assert boletas.find_one({"_id": 5}) is not None
    assert usuarios.count_documents({"usuario": "admin"}) == 1


def _assert_no_se_aplico_respaldo():
    assert vendedores.count_documents({"_id": "JOSE"}) == 0
    assert boletas.count_documents({"_id": 1, "vendedor_id": "JOSE"}) == 0


def test_restore_rechaza_boleta_asignada_inexistente(client):
    data = _backup_valido()
    data["vendedores"] = [{"_id": "JOSE", "nombre": "JOSE", "telefono": "", "boletas_asignadas": [99]}]
    resp = _restaurar(client, data)
    assert resp.status_code == 302
    _assert_no_se_aplico_respaldo()


def test_restore_rechaza_vendedor_id_inexistente(client):
    data = _backup_valido()
    data["boletas"][0]["vendedor_id"] = "FANTASMA"
    resp = _restaurar(client, data)
    assert resp.status_code == 302
    _assert_no_se_aplico_respaldo()


def test_restore_rechaza_contador_factura_menor_al_maximo(client):
    data = _backup_valido()
    data["facturas"] = [{"_id": 5, "tipo": "cliente", "valor_total": 70000}]
    data["configuracion"] = [{"_id": "rifa", "factura_counter": 3, "traslado_counter": 0}]
    resp = _restaurar(client, data)
    assert resp.status_code == 302
    _assert_no_se_aplico_respaldo()


def test_restore_rechaza_boleta_sin_id(client):
    data = _backup_valido()
    data["boletas"].append({"_id": "x", "total_abonado": 0})
    resp = _restaurar(client, data)
    assert resp.status_code == 302
    _assert_no_se_aplico_respaldo()


def test_restore_advierte_sobre_divergencia_ledger(client):
    boletas.update_one({"_id": 10}, {"$set": {"total_abonado": 60000, "historial_movimientos": []}})
    data = _backup_estado_actual()
    resp = _restaurar(client, data, follow=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "no coincide con el histórico neto" in body
    assert boletas.find_one({"_id": 10})["total_abonado"] == 60000


def test_restore_rechaza_falta_documento_rifa(client):
    data = _backup_valido()
    data["configuracion"] = []
    resp = _restaurar(client, data)
    assert resp.status_code == 302
    _assert_no_se_aplico_respaldo()


def test_export_import_roundtrip(client):
    resp = client.post("/backup", data={"accion": "exportar"})
    assert resp.status_code == 200
    assert "application/zip" in resp.headers["Content-Type"]
    payload = resp.get_data()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        nombres = zf.namelist()
        assert "boletas.json" in nombres
        data = {n[:-5]: json_util.loads(zf.read(n)) for n in nombres if n.endswith(".json")}
    assert len(data["boletas"]) == 500

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json_util.dumps(data, ensure_ascii=False))
    resp2 = client.post(
        "/backup",
        data={"accion": "importar", "archivo": (io.BytesIO(buf.getvalue()), "backup.zip"), "clave_admin": ADMIN_PASSWORD},
    )
    assert resp2.status_code == 302
    assert boletas.count_documents({}) == 500

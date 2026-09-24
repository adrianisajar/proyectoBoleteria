import io
import zipfile

from bson import json_util
from conftest import ADMIN_PASSWORD, CAJA_PASSWORD, CAJA_USUARIO, login

from database import boletas, configuracion, usuarios
from motores.config_service import get_rifa_activa


def _config_doc():
    return configuracion.find_one({"_id": "rifa"}) or {}


def test_guardar_empresa_requiere_clave(client):
    client.post("/configuracion", data={"action": "guardar_empresa", "nombre_empresa": "EMPRESA NUEVA"})
    assert _config_doc().get("nombre_empresa", "") == ""


def test_guardar_empresa_con_clave_aplica(client):
    resp = client.post(
        "/configuracion",
        data={"action": "guardar_empresa", "nombre_empresa": "EMPRESA NUEVA", "clave_admin": ADMIN_PASSWORD},
    )
    assert resp.status_code == 302
    assert _config_doc().get("nombre_empresa", "") == "EMPRESA NUEVA"


def test_guardar_config_clave_incorrecta_no_aplica(client):
    client.post(
        "/configuracion",
        data={
            "action": "guardar_config",
            "nombre_rifa": "Rifa Cambiada",
            "valor_boleta": "50.000",
            "cantidad_boletas": "8000",
            "clave_admin": "incorrecta",
        },
    )
    assert _config_doc().get("valor_boleta", 70000) == 70000


def test_nueva_rifa_requiere_clave(client):
    client.post(
        "/rifas/nueva",
        data={"nombre_rifa_nueva": "Rifa de prueba", "valor_boleta_nueva": "50.000", "cantidad_boletas": "8000"},
    )
    assert get_rifa_activa(force=True)["nombre"] == "Rifa Test"


def test_cambiar_contrasena_requiere_clave(client, client_anon):
    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    client.post(f"/usuarios/{caja['_id']}/contrasena", data={"password": "nueva-clave"})
    resp = login(client_anon, usuario=CAJA_USUARIO, password=CAJA_PASSWORD)
    assert resp.status_code == 302


def test_restaurar_backup_requiere_clave(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json_util.dumps({"boletas": [], "vendedores": [], "facturas": [], "configuracion": []}))
    buf.seek(0)
    resp = client.post("/backup", data={"accion": "importar", "archivo": (buf, "backup.zip")})
    assert resp.status_code == 302
    assert boletas.count_documents({}) == 500

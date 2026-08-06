import time

from conftest import ADMIN_USUARIO, CAJA_PASSWORD, CAJA_USUARIO, login

from database import facturas, usuarios
from motores.constants import SESSION_IDLE_TIMEOUT_SECONDS
from motores.fechas import now_local


def test_ruta_protegida_redirige_a_login(client_anon):
    resp = client_anon.get("/consultas")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_api_sin_sesion_devuelve_401(client_anon):
    resp = client_anon.get("/api/boletas/1")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_login_exitoso_redirige_y_accede(client_anon):
    resp = login(client_anon)
    assert resp.status_code == 302
    resp = client_anon.get("/dashboard")
    assert resp.status_code == 200
    assert "Admin Test" in resp.get_data(as_text=True)


def test_login_fallido(client_anon):
    resp = client_anon.post("/login", data={"usuario": ADMIN_USUARIO, "password": "incorrecta"})
    assert resp.status_code == 401
    assert "Usuario o contrase" in resp.get_data(as_text=True)


def test_login_usuario_inactivo(client, client_anon):
    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    resp = client.post(f"/usuarios/{caja['_id']}/estado", data={"activo": "0"})
    assert resp.status_code == 302
    resp = login(client_anon, usuario=CAJA_USUARIO, password=CAJA_PASSWORD)
    assert resp.status_code == 401


def test_logout_destruye_sesion(client):
    resp = client.post("/logout")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    resp = client.get("/dashboard")
    assert resp.status_code == 302


def test_caja_no_accede_admin(client_caja):
    for ruta in ("/configuracion", "/vendedores", "/backup"):
        resp = client_caja.get(ruta)
        assert resp.status_code == 403, ruta


def test_caja_accede_consultas(client_caja):
    resp = client_caja.get("/consultas")
    assert resp.status_code == 200
    resp = client_caja.get("/facturas/nueva/cliente")
    assert resp.status_code == 200


def test_api_caja_bloqueado_403_json(client_caja):
    resp = client_caja.get("/api/usuarios")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_crear_usuario(client):
    resp = client.post(
        "/usuarios/crear",
        data={"nombre": "Nuevo", "usuario": "nuevo", "rol": "cajero", "password": "secreto1"},
    )
    assert resp.status_code == 302
    doc = usuarios.find_one({"usuario": "nuevo"})
    assert doc is not None
    assert doc["rol"] == "cajero"
    assert doc["nombre"] == "NUEVO"


def test_crear_usuario_duplicado(client):
    resp = client.post(
        "/usuarios/crear",
        data={"nombre": "Admin", "usuario": ADMIN_USUARIO, "rol": "cajero", "password": "secreto1"},
    )
    assert resp.status_code == 302
    assert usuarios.count_documents({"usuario": ADMIN_USUARIO}) == 1


def test_crear_usuario_password_corta(client):
    resp = client.post(
        "/usuarios/crear",
        data={"nombre": "Corto", "usuario": "corto", "rol": "cajero", "password": "123"},
    )
    assert resp.status_code == 302
    assert usuarios.count_documents({"usuario": "corto"}) == 0


def test_no_desactivar_propio_usuario(client):
    admin = usuarios.find_one({"usuario": ADMIN_USUARIO})
    resp = client.post(f"/usuarios/{admin['_id']}/estado", data={"activo": "0"})
    assert resp.status_code == 302
    assert usuarios.find_one({"usuario": ADMIN_USUARIO})["activo"] is True


def test_no_cambiar_propio_rol(client):
    admin = usuarios.find_one({"usuario": ADMIN_USUARIO})
    resp = client.post(f"/usuarios/{admin['_id']}/editar", data={"nombre": "Admin Test", "rol": "cajero"})
    assert resp.status_code == 302
    assert usuarios.find_one({"usuario": ADMIN_USUARIO})["rol"] == "admin"


def test_cambiar_contrasena(client, client_anon):
    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    resp = client.post(f"/usuarios/{caja['_id']}/contrasena", data={"password": "nueva-clave"})
    assert resp.status_code == 302
    resp = login(client_anon, usuario=CAJA_USUARIO, password="nueva-clave")
    assert resp.status_code == 302


def test_configuracion_muestra_gestion_usuarios(client):
    resp = client.get("/configuracion")
    assert resp.status_code == 200
    assert "Usuarios" in resp.get_data(as_text=True)
    assert "Admin Test" in resp.get_data(as_text=True)


def test_idle_timeout_cierra_sesion(client):
    with client.session_transaction() as sess:
        sess["_ultima_actividad"] = time.time() - SESSION_IDLE_TIMEOUT_SECONDS - 60
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_factura_cliente_registra_usuario(client):
    resp = client.post(
        "/facturas/nueva/cliente",
        data={
            "nombre": "JULIANA PEREZ",
            "telefono": "3001234567",
            "fecha": "2026-01-01",
            "boleta[]": ["0001"],
            "monto[]": ["50000"],
            "metodo[]": ["efectivo"],
        },
    )
    assert resp.status_code == 302
    factura = facturas.find_one({"tipo": "cliente"})
    assert factura is not None
    assert factura["usuario_nombre"] == "Admin Test"
    assert factura["usuario_id"]


def test_factura_antigua_muestra_no_registrado(client):
    facturas.insert_one(
        {
            "_id": 999,
            "tipo": "cliente",
            "fecha": now_local(),
            "boletas": [],
            "detalle": [],
            "valor_total": 0,
            "cliente": {"nombre": "VIEJO", "telefono": "", "direccion": ""},
            "vendedor_id": "LOCAL",
            "vendedor_nombre": "LOCAL",
        }
    )
    resp = client.get("/facturas/999")
    assert resp.status_code == 200
    assert "No registrado" in resp.get_data(as_text=True)


def test_eliminar_usuario(client, client_anon):
    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    resp = client.post(f"/usuarios/{caja['_id']}/eliminar", data={"confirmacion": "ELIMINAR"})
    assert resp.status_code == 302
    assert usuarios.find_one({"usuario": CAJA_USUARIO}) is None


def test_no_eliminar_propio_usuario(client):
    admin = usuarios.find_one({"usuario": ADMIN_USUARIO})
    resp = client.post(f"/usuarios/{admin['_id']}/eliminar", data={"confirmacion": "ELIMINAR"})
    assert resp.status_code == 302
    assert usuarios.find_one({"usuario": ADMIN_USUARIO}) is not None


def test_eliminar_usuario_requiere_confirmacion(client):
    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    resp = client.post(f"/usuarios/{caja['_id']}/eliminar")
    assert resp.status_code == 302
    assert usuarios.find_one({"usuario": CAJA_USUARIO}) is not None


def test_usuario_eliminado_no_inicia_sesion(client, client_anon):
    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    client.post(f"/usuarios/{caja['_id']}/eliminar", data={"confirmacion": "ELIMINAR"})
    resp = login(client_anon, usuario=CAJA_USUARIO, password=CAJA_PASSWORD)
    assert resp.status_code == 401

"""Tests de los hallazgos MEDIOS de la auditoría (open redirect, rol sync,
fail-closed, egresos ajenas, sort de reservas, respaldo, throttle admin,
limpieza de batch, dedup de compradores)."""

from types import SimpleNamespace
from urllib.parse import quote

from conftest import ADMIN_PASSWORD, ADMIN_USUARIO, CAJA_USUARIO
from test_backup_restore import _assert_no_se_aplico_respaldo, _backup_valido, _restaurar

import motores.flask_integration as flask_integration
import motores.usuarios as mod_usuarios
from database import boletas, configuracion, facturas, reservas, rifas, usuarios
from motores.cache import invalidate_config_cache
from motores.constants import MOV_EGRESO, MOV_PAGO
from motores.fechas import now_local
from motores.payment_service import _limpiar_temp_batch

# ---------------------------------------------------------------------------
# 1. Open redirect en /login (?next=)
# ---------------------------------------------------------------------------


def _login_post(client, next_qs=""):
    return client.post(f"/login{next_qs}", data={"usuario": ADMIN_USUARIO, "password": ADMIN_PASSWORD})


def test_login_respeta_next_interno_seguro(client_anon):
    resp = _login_post(client_anon, "?next=/consultas")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/consultas"


def test_login_next_malicioso_no_redirige_a_otro_host(client_anon):
    for next_url in ("//evil.com", "/\\evil.com", "https://evil.com", "/\nevil"):
        resp = _login_post(client_anon, f"?next={quote(next_url, safe='')}")
        assert resp.status_code == 302
        loc = resp.headers["Location"]
        assert "evil.com" not in loc
        assert loc.startswith("/")
        assert not loc.startswith("//")
        # Cerrar sesión para poder probar el siguiente caso autenticado.
        client_anon.post("/logout")


# ---------------------------------------------------------------------------
# 2. Rol sincronizado con la DB sin re-login
# ---------------------------------------------------------------------------


def test_rol_revocado_aplica_sin_relogin(client, client_caja):
    resp = client_caja.get("/dashboard")
    assert resp.status_code == 403

    caja = usuarios.find_one({"usuario": CAJA_USUARIO})
    resp = client.post(f"/usuarios/{caja['_id']}/editar", data={"nombre": "Caja Test", "rol": "admin"})
    assert resp.status_code == 302

    resp = client_caja.get("/dashboard")
    assert resp.status_code == 200
    assert usuarios.find_one({"usuario": CAJA_USUARIO})["rol"] == "admin"


# ---------------------------------------------------------------------------
# 3. Fail-closed: fallo de DB al verificar la sesión cierra la sesión
# ---------------------------------------------------------------------------


def test_fallo_db_verificacion_cierra_sesion(client_caja, monkeypatch):
    class _Boom:
        def find_one(self, *args, **kwargs):
            raise RuntimeError("db caida")

    flask_integration.invalidate_activo_cache()
    monkeypatch.setattr(flask_integration, "usuarios", _Boom())

    resp = client_caja.get("/consultas")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    with client_caja.session_transaction() as sess:
        assert "usuario_id" not in sess


# ---------------------------------------------------------------------------
# 4-5. Egresos: boletas deben pertenecer al vendedor seleccionado
# ---------------------------------------------------------------------------


def _post_egreso(client, vendedor_id, boleta, valor="20000"):
    return client.post(
        "/facturas/egreso/nueva",
        data={
            "vendedor_id": vendedor_id,
            "fecha": "2026-07-30",
            "egreso_tipo": "comision_vendedor",
            "boleta[]": [boleta],
            "valor[]": [valor],
            "metodo[]": ["efectivo"],
            "referencia[]": [""],
            "banco[]": [""],
        },
    )


def test_egreso_rechaza_boleta_de_otro_vendedor(client):
    boletas.update_one({"_id": 1}, {"$set": {"vendedor_id": "OTRO"}})
    resp = _post_egreso(client, "VEND01", "0001")
    assert resp.status_code == 200
    assert "no asignadas al vendedor seleccionado" in resp.get_data(as_text=True)
    assert facturas.count_documents({"tipo": "egreso"}) == 0
    assert not any(m.get("tipo") == MOV_EGRESO for m in boletas.find_one({"_id": 1})["historial_movimientos"])


def test_egreso_local_acepta_boletas_local(client):
    boletas.update_one(
        {"_id": 1},
        {
            "$set": {
                "vendedor_id": "LOCAL",
                "estado": "pagada",
                "total_abonado": 70000,
                "historial_movimientos": [{"tipo": MOV_PAGO, "fecha": "2026-07-01", "valor": 70000, "metodo": "efectivo", "factura_id": 50}],
            }
        },
    )
    resp = _post_egreso(client, "LOCAL", "0001")
    assert resp.status_code == 302
    assert facturas.count_documents({"tipo": "egreso"}) == 1
    b = boletas.find_one({"_id": 1})
    assert any(m.get("tipo") == MOV_EGRESO for m in b["historial_movimientos"])


# ---------------------------------------------------------------------------
# 6. Sort de reservas mapea campos whitelistados a campos reales
# ---------------------------------------------------------------------------


def test_reservas_sort_por_comprador_y_reservada(client):
    reservas.insert_many(
        [
            {"_id": 5, "cliente": {"nombre": "ZZULMA", "telefono": "300", "direccion": ""}, "creado_en": now_local()},
            {"_id": 6, "cliente": {"nombre": "AANITA", "telefono": "301", "direccion": ""}, "creado_en": now_local()},
        ]
    )
    resp = client.get("/compradores/reservas?sort_by=comprador&sort_dir=asc")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert html.index("AANITA") < html.index("ZZULMA")

    resp = client.get("/compradores/reservas?sort_by=reservada&sort_dir=desc")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 7-9. Validación ligera de respaldos (rifas/usuarios/traslados)
# ---------------------------------------------------------------------------


def test_restore_rechaza_rol_usuario_invalido(client):
    data = _backup_valido()
    data["usuarios"] = [{"usuario": "admin2", "nombre": "X", "rol": "superuser", "password_hash": "hash"}]
    resp = _restaurar(client, data, follow=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "rol inválido" in body
    assert "La restauración fue cancelada" in body
    _assert_no_se_aplico_respaldo()


def test_restore_rechaza_traslado_con_boleta_inexistente(client):
    data = _backup_valido()
    data["traslados"] = [{"_id": 1, "valor": 5000, "boleta_origen": 1, "boleta_destino": 9999}]
    resp = _restaurar(client, data, follow=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "no existe en el respaldo" in body
    assert "La restauración fue cancelada" in body
    _assert_no_se_aplico_respaldo()


def test_restore_rechaza_dos_rifas_activas(client):
    data = _backup_valido()
    data["rifas"] = [
        {"_id": "r1", "estado": "activa", "valor_boleta": 70000, "cantidad_boletas": 500},
        {"_id": "r2", "estado": "activa", "valor_boleta": 80000, "cantidad_boletas": 500},
    ]
    resp = _restaurar(client, data, follow=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "rifas con estado" in body
    assert "La restauración fue cancelada" in body
    _assert_no_se_aplico_respaldo()


# ---------------------------------------------------------------------------
# 10. guardar_config sin rifa activa: override en configuracion
# ---------------------------------------------------------------------------


def test_guardar_config_sin_rifa_activa_escribe_override(client, monkeypatch):
    class _RifasSinActiva:
        def update_one(self, *args, **kwargs):
            return SimpleNamespace(matched_count=0, upserted_id=None)

    monkeypatch.setattr("motores.rifas.rifas", _RifasSinActiva())
    try:
        resp = client.post(
            "/configuracion",
            data={
                "action": "guardar_config",
                "valor_boleta": "12000",
                "nombre_rifa": "Rifa Nueva",
                "cantidad_boletas": "500",
                "clave_admin": ADMIN_PASSWORD,
            },
        )
        assert resp.status_code == 302

        cfg = configuracion.find_one({"_id": "rifa"})
        assert cfg["valor_boleta"] == 12000
        assert cfg["nombre_rifa"] == "Rifa Nueva"
        assert cfg["cantidad_boletas"] == 500
        # La rifa activa real no fue tocada (la escritura fue al fallback).
        assert rifas.find_one({"estado": "activa"})["valor_boleta"] == 70000
    finally:
        # No contaminar tests siguientes: _reset() también los limpia, pero aquí
        # se retira el override en cuanto termina la verificación.
        configuracion.update_one(
            {"_id": "rifa"},
            {"$unset": {"valor_boleta": "", "nombre_rifa": "", "cantidad_boletas": ""}},
        )
        invalidate_config_cache()


# ---------------------------------------------------------------------------
# 11. Throttle de ensure_initial_admin en GET /login
# ---------------------------------------------------------------------------


def test_login_no_reintenta_admin_en_cada_get(client_anon, monkeypatch):
    real = mod_usuarios.usuarios

    class _Contador:
        def __init__(self):
            self.llamadas = 0

        def __getattr__(self, nombre):
            return getattr(real, nombre)

        def update_one(self, *args, **kwargs):
            self.llamadas += 1
            return real.update_one(*args, **kwargs)

    proxy = _Contador()
    monkeypatch.setattr(mod_usuarios, "usuarios", proxy)
    monkeypatch.setitem(mod_usuarios._ADMIN_ULTIMA_VERIFICACION, "ts", 0.0)

    resp = client_anon.get("/login")
    assert resp.status_code == 200
    assert proxy.llamadas == 1

    resp = client_anon.get("/login")
    assert resp.status_code == 200
    assert proxy.llamadas == 1  # dentro del TTL: sin nuevo upsert


# ---------------------------------------------------------------------------
# 12. _limpiar_temp_batch solo remueve el marcador del propio batch
# ---------------------------------------------------------------------------


def test_temp_batch_no_borra_otro_marcador():
    boletas.update_one(
        {"_id": 10},
        {
            "$set": {
                "historial_movimientos": [
                    {"tipo": MOV_PAGO, "fecha": "2026-07-01", "valor": 70000, "metodo": "efectivo", "_temp_batch_id": "batch-A"},
                    {"tipo": MOV_PAGO, "fecha": "2026-07-02", "valor": 10000, "metodo": "efectivo", "_temp_batch_id": "batch-B"},
                    {"tipo": MOV_PAGO, "fecha": "2026-07-03", "valor": 5000, "metodo": "efectivo"},
                ]
            }
        },
    )

    _limpiar_temp_batch("batch-A")
    movs = boletas.find_one({"_id": 10})["historial_movimientos"]
    assert len(movs) == 3
    assert "_temp_batch_id" not in movs[0]
    assert movs[0]["valor"] == 70000
    assert movs[1].get("_temp_batch_id") == "batch-B"
    assert movs[2]["valor"] == 5000

    _limpiar_temp_batch("batch-B")
    movs = boletas.find_one({"_id": 10})["historial_movimientos"]
    assert all("_temp_batch_id" not in m for m in movs)
    assert [m["valor"] for m in movs] == [70000, 10000, 5000]


# ---------------------------------------------------------------------------
# 13. Compradores rápido: filas duplicadas no registran doble pago
# ---------------------------------------------------------------------------


def test_compradores_rapido_dedup(client):
    resp = client.post(
        "/compradores/rapido",
        json={
            "rows": [
                {"boleta": 5, "nombre": "ANA TORRES", "telefono": "300", "direccion": "", "pago": "10000"},
                {"boleta": 5, "nombre": "ANA DUPLICADA", "telefono": "301", "direccion": "", "pago": "10000"},
            ]
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["updated"] == 1
    assert data["pagos_registrados"] == 1
    assert any("repetida" in e for e in data["errores"])

    b = boletas.find_one({"_id": 5})
    assert b["cliente"]["nombre"] == "ANA TORRES"
    assert b["total_abonado"] == 10000
    pagos = [m for m in b["historial_movimientos"] if m.get("tipo") == MOV_PAGO]
    assert len(pagos) == 1

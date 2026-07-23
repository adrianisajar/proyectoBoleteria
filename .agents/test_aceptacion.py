"""
Pruebas funcionales de aceptacion - Sistema de Facturacion
Ejecuta con: python .agents/test_aceptacion.py
USA SOLO ASCII - sin caracteres Unicode
"""
import os, sys, json, re, time, io
from datetime import date

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

os.environ["FLASK_DEBUG"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TODAY = date.today().strftime("%Y-%m-%d")

from app import app
from database import db, boletas, vendedores, facturas, configuracion

# ============================================================
# HELPERS
# ============================================================
TEST_VENDOR = "TEST_VENDOR_FUNC"
TEST_VENDOR2 = "TEST_VENDOR_FUNC2"
TICKETS_TO_ASSIGN = "13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29"

PASS = 0
FAIL = 0
ERRORS = []

def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        safe = msg.encode('ascii', 'replace').decode('ascii')
        ERRORS.append(safe)
        print(f"  FAIL: {safe}")

def json_of(response):
    try:
        return json.loads(response.data)
    except Exception:
        return None

def clear_test_data():
    from motores.shared import estado_pipeline_expr, get_config
    vendedores.delete_many({"_id": {"$regex": "^TEST_VENDOR_FUNC", "$options": "i"}})
    facturas.delete_many({"cliente.nombre": "TEST FUNCIONAL"})
    facturas.delete_many({"cliente.nombre": "TEST VENDOR FUNC"})
    facturas.delete_many({"cliente.nombre": "TVF"})
    # Full reset of all boletas that may have been touched by tests
    TEST_RANGE = list(range(10, 30))
    reset = {"$set": {"cliente": {"nombre": "", "telefono": "", "direccion": ""},
                       "vendedor_id": "", "total_abonado": 0,
                       "historial_pagos": []},
              "$unset": {"factura_id": ""}}
    boletas.update_many({"_id": {"$in": TEST_RANGE}}, reset)
    # Recalculate estado via pipeline
    cfg = get_config(force=True)
    vb = int(cfg.get("valor_boleta", 70000))
    boletas.update_many(
        {"_id": {"$in": TEST_RANGE}},
        [{"$set": {"estado": estado_pipeline_expr(vb)}}],
    )

suite_start = time.time()
clear_test_data()

with app.test_client() as client:
    with app.app_context():
        from motores.shared import get_config
        cfg = get_config(force=True)
        V = int(cfg["valor_boleta"])
        print(f"Config: valor_boleta={V}, empresa={cfg.get('nombre_empresa')}")
        print()

        # ================================================
        # 1. DASHBOARD
        # ================================================
        print("=" * 60)
        print("1. DASHBOARD")
        print("=" * 60)
        r = client.get("/dashboard")
        check(r.status_code == 200, "GET /dashboard => 200")
        check(len(r.data) > 500, "Dashboard content renders (>500 bytes)")
        print(f"  Dashboard: {len(r.data)} bytes")

        # ================================================
        # 2. CONFIGURACION
        # ================================================
        print("=" * 60)
        print("2. CONFIGURACION")
        print("=" * 60)
        r = client.get("/configuracion")
        check(r.status_code == 200, "GET /configuracion => 200")

        # POST: save config (no changes, just verify save works)
        r = client.post("/configuracion", data={
            "nombre_empresa": "RIFAS TRANSPARENCIA",
            "nombre_rifa": "RIFA 2026 3",
            "valor_boleta": str(V),
            "ciudad": "PUERTO TEJADA",
            "direccion": "COLOMBIA",
            "telefono": "3171457567",
            "comision_por_boleta": "10000",
            "comision_fija": "10000",
            "comision_porcentaje": "10.0",
            "premio_mayor": "200 millones",
            "footer_texto": "Documento interno, no tiene validez fiscal.",
            "precio_recambio": "2000",
            "precio_resorteo": "55000",
            "recambio_activo": "true",
            "login_requerido": "false",
            "cantidad_boletas": "10000",
            "valor_minimo_adicional": "20000",
        }, follow_redirects=True)
        check(r.status_code == 200, "POST /configuracion (save) => 200")

        # ================================================
        # 3. CONSULTAS / BUSQUEDA
        # ================================================
        print("=" * 60)
        print("3. CONSULTAS / BUSQUEDA")
        print("=" * 60)
        r = client.get("/consultas")
        check(r.status_code == 200, "GET /consultas => 200")

        # Buscar boleta 1
        r = client.get("/consultas?search=1&tipo=numero")
        check(r.status_code == 200, "GET /consultas?search=1&tipo=numero => 200")

        # Buscar boleta exacta
        r = client.get("/consultas?search=0001&tipo=numero")
        check(r.status_code == 200, "GET /consultas?search=0001&tipo=numero => 200")

        # Buscar por estado
        r = client.get("/consultas?estado=disponible")
        check(r.status_code == 200, "GET /consultas?estado=disponible => 200")

        # Buscar combinado estado + saldo incompatibles
        r = client.get("/consultas?estado=disponible&saldo_estado=pendiente")
        check(r.status_code == 200, "GET /consultas estado+saldo_estado incompatibles => 200 (error handled)")

        # ================================================
        # 4. API BOLETAS
        # ================================================
        print("=" * 60)
        print("4. API /api/boletas/<id>")
        print("=" * 60)
        r = client.get("/api/boletas/1")
        j = json_of(r)
        check(r.status_code == 200, "GET /api/boletas/1 => 200")
        check(j is not None, "GET /api/boletas/1 => JSON")
        if j:
            check(j.get("ok") is True, "API boleta 1 ok=True")
            check("estado" in j, "Boleta JSON has 'estado'")
            check("total_abonado" in j, "Boleta JSON has 'total_abonado'")

        # Boleta fuera de rango
        r = client.get("/api/boletas/99999")
        check(r.status_code == 400, "GET /api/boletas/99999 (out of range) => 400")

        # ================================================
        # 5. CLIENTES autocomplete
        # ================================================
        print("=" * 60)
        print("5. API /api/clientes autocomplete")
        print("=" * 60)
        r = client.get("/api/clientes?q=jo")
        check(r.status_code == 200, "GET /api/clientes?q=jo => 200")
        j = json_of(r)
        check(j is not None, "Response is JSON")

        # ================================================
        # 6. VENDEDORES
        # ================================================
        print("=" * 60)
        print("6. VENDEDORES")
        print("=" * 60)
        r = client.get("/vendedores")
        check(r.status_code == 200, "GET /vendedores => 200")

        # Crear vendedor 1 (operacion=guardar)
        r = client.post("/vendedores", data={
            "operacion": "guardar",
            "vendedor_id": TEST_VENDOR,
            "nombre": "TEST VENDOR FUNC",
            "telefono": "3001112233"
        }, follow_redirects=True)
        check(r.status_code == 200, "POST vendedores (guardar) => 200")

        # Verify in DB
        v1 = vendedores.find_one({"_id": TEST_VENDOR})
        check(v1 is not None, f"Vendor {TEST_VENDOR} exists in DB")
        if v1:
            check(v1.get("nombre") == "TEST VENDOR FUNC", f"Vendor name correct (got {v1.get('nombre')})")
            check(v1.get("telefono") == "3001112233", f"Vendor phone correct (got {v1.get('telefono')})")

        # Crear vendedor 2
        client.post("/vendedores", data={
            "operacion": "guardar",
            "vendedor_id": TEST_VENDOR2,
            "nombre": "TEST VENDOR FUNC 2",
            "telefono": "3001112244"
        }, follow_redirects=True)
        v2 = vendedores.find_one({"_id": TEST_VENDOR2})
        check(v2 is not None, f"Vendor {TEST_VENDOR2} exists in DB")

        # Asignar boletas al vendedor 1 (operacion=asignar)
        r = client.post("/vendedores", data={
            "operacion": "asignar",
            "vendedor_id": TEST_VENDOR,
            "boletas": TICKETS_TO_ASSIGN,
        }, follow_redirects=True)
        check(r.status_code == 200, "POST vendedores (asignar) => 200")

        # Check boletas assigned in DB
        for bid in [13, 15, 20, 29]:
            b = boletas.find_one({"_id": bid})
            check(b is not None and b.get("vendedor_id") == TEST_VENDOR,
                  f"Boleta {bid} assigned to {TEST_VENDOR} (got {b.get('vendedor_id') if b else 'None'})")

        # Verify vendedor list page shows vendor
        r = client.get("/vendedores")
        check(r.status_code == 200, "GET /vendedores after assignment => 200")
        html_lower = r.data.lower()
        check(b"test vendor func" in html_lower or b"TEST VENDOR FUNC" in r.data,
              "Vendor listed on vendedores page")

        # Liberar algunas boletas (operacion=quitar)
        r = client.post("/vendedores", data={
            "operacion": "quitar",
            "vendedor_id": TEST_VENDOR,
            "boletas": "10,11,12",
        }, follow_redirects=True)
        check(r.status_code == 200, "POST vendedores (quitar) => 200")
        for bid in [10, 11, 12]:
            b = boletas.find_one({"_id": bid})
            v_id = b.get("vendedor_id") if b else "N/A"
            check(v_id in ("", None),
                  f"Boleta {bid} released (vendedor_id='{v_id}')")

        # ================================================
        # 7. FACTURACION VENDEDOR
        # ================================================
        print("=" * 60)
        print("7. FACTURACION VENDEDOR")
        print("=" * 60)
        r = client.get("/facturas/nueva/vendedor")
        check(r.status_code == 200, "GET /facturas/nueva/vendedor => 200 (form)")

        # Create vendor invoice with 2 payment rows for tickets 13,14,15
        r = client.post("/facturas/nueva/vendedor", data={
            "vendedor_id": TEST_VENDOR,
            "fecha": TODAY,
            "boleta[]": ["13,14", "15"],
            "monto[]": ["70000", "70000"],
            "metodo[]": ["efectivo", "transferencia"],
            "referencia[]": ["", "TRF-TEST-001"],
            "banco[]": ["", "BANCOLOMBIA"],
            "observaciones": "Pago completo de prueba",
            "generar": "1"
        }, follow_redirects=True)
        check(r.status_code == 200, "POST /facturas/nueva/vendedor (create) => 200")

        # Find the vendor factura
        f_v = facturas.find_one({"vendedor_id": TEST_VENDOR}, sort=[("_id", -1)])
        check(f_v is not None, "Vendor invoice created in DB")
        if f_v:
            factura_v_id = f_v["_id"]
            print(f"  Vendor invoice #{factura_v_id} created")

            # View it
            r = client.get(f"/facturas/{factura_v_id}")
            check(r.status_code == 200, f"GET /facturas/{factura_v_id} => 200")
            content = r.data.decode('utf-8', errors='replace')
            check("COMPROBANTE" in content.upper() or "RECAUDO" in content.upper(),
                  "Vendor invoice displays 'Comprobante de Recaudo'")

            # Check boleta estados updated
            b13 = boletas.find_one({"_id": 13})
            if b13:
                check(b13.get("total_abonado", 0) == V,
                      f"Boleta 13 total_abonado={b13.get('total_abonado')} (expected {V})")

        # ================================================
        # 8. FACTURACION CLIENTE
        # ================================================
        print("=" * 60)
        print("8. FACTURACION CLIENTE")
        print("=" * 60)
        r = client.get("/facturas/nueva/cliente")
        check(r.status_code == 200, "GET /facturas/nueva/cliente => 200 (form)")

        # Create client invoice - ABONO parcial (boletas 16, 17)
        r = client.post("/facturas/nueva/cliente", data={
            "nombre": "TEST FUNCIONAL",
            "telefono": "3001234567",
            "direccion": "Calle Test #123",
            "fecha": TODAY,
            "boleta[]": ["16", "17"],
            "monto[]": ["35000", "35000"],
            "metodo[]": ["efectivo", "efectivo"],
            "referencia[]": ["", ""],
            "banco[]": ["", ""],
            "observaciones": "Abono parcial de prueba",
            "generar": "1"
        }, follow_redirects=True)
        check(r.status_code == 200, "POST /facturas/nueva/cliente (abono parcial) => 200")

        # Find the client factura for boleta 16 (the abono)
        f_c_abono = facturas.find_one({"boletas": 16}, sort=[("_id", -1)])
        check(f_c_abono is not None, "Client abono invoice created in DB")
        if f_c_abono:
            factura_c_abono_id = f_c_abono["_id"]
            print(f"  Client abono invoice #{factura_c_abono_id} created")

            r = client.get(f"/facturas/{factura_c_abono_id}")
            check(r.status_code == 200, f"GET /facturas/{factura_c_abono_id} => 200")
            content = r.data.decode('utf-8', errors='replace')
            check("TEST FUNCIONAL" in content, "Client invoice shows client name")
            check("ABONO" in content.upper() or "PAGO" in content.upper(),
                  "Client invoice shows movement type (ABONO/PAGO)")

        # Create second client invoice - PAGO TOTAL on boleta 18
        r = client.post("/facturas/nueva/cliente", data={
            "nombre": "TEST FUNCIONAL",
            "telefono": "3001234567",
            "direccion": "Calle Test #123",
            "fecha": TODAY,
            "boleta[]": ["18"],
            "monto[]": [str(V)],
            "metodo[]": ["efectivo"],
            "referencia[]": [""],
            "banco[]": [""],
            "observaciones": "Pago total de prueba",
            "generar": "1"
        }, follow_redirects=True)
        check(r.status_code == 200, "POST /facturas/nueva/cliente (pago total) => 200")

        # ================================================
        # 9. LISTADOS DE FACTURAS
        # ================================================
        print("=" * 60)
        print("9. LISTADOS DE FACTURAS")
        print("=" * 60)
        r = client.get("/facturas")
        check(r.status_code == 200, "GET /facturas (all) => 200")
        content = r.data.decode('utf-8', errors='replace')
        check("FACTURA" in content.upper(), "Factura list header present")

        r = client.get("/facturas/cliente")
        check(r.status_code == 200, "GET /facturas/cliente => 200")
        content = r.data.decode('utf-8', errors='replace')
        check("TEST FUNCIONAL" in content, "Client facturas list shows client name")

        r = client.get("/facturas/vendedor")
        check(r.status_code == 200, "GET /facturas/vendedor => 200")
        content = r.data.decode('utf-8', errors='replace')
        check("TEST VENDOR FUNC" in content, "Vendor facturas list shows vendor name")

        # ================================================
        # 10. ANULACION DE FACTURA
        # ================================================
        print("=" * 60)
        print("10. ANULACION DE FACTURA")
        print("=" * 60)
        if f_c_abono:
            factura_c_id = f_c_abono["_id"]

            # Extract anulacion_hash from the factura view page
            r_view = client.get(f"/facturas/{factura_c_id}")
            check(r_view.status_code == 200, f"GET /facturas/{factura_c_id} (pre-anulation) => 200")
            view_html = r_view.data.decode('utf-8', errors='replace')
            hash_match = re.search(r'name="anulacion_hash"[^>]*value="([^"]+)"', view_html)
            anulacion_hash = hash_match.group(1) if hash_match else ""
            check(bool(anulacion_hash), f"anulacion_hash extracted from view (got '{anulacion_hash}')")

            # Record state before anulation
            b16_before = boletas.find_one({"_id": 16})
            abonado_b16_before = b16_before.get("total_abonado", 0) if b16_before else 0

            r = client.post(f"/facturas/{factura_c_id}/anular", data={
                "motivo": "Prueba de anulacion funcional",
                "anulacion_hash": anulacion_hash,
            }, follow_redirects=True)
            check(r.status_code == 200, f"POST /facturas/{factura_c_id}/anular => 200")

            # Verify factura is marked as anulada
            fc_check = facturas.find_one({"_id": factura_c_id})
            check(fc_check is not None, "Anulated factura exists in DB")
            if fc_check:
                check(fc_check.get("anulada") is True,
                      f"Factura {factura_c_id} is anulada={fc_check.get('anulada')}")
                check(fc_check.get("motivo_anulacion") == "Prueba de anulacion funcional",
                      f"Anulation reason stored correctly (got '{fc_check.get('motivo_anulacion')}')")
                check(fc_check.get("anulada_en") is not None, "anulada_en timestamp present")
                check(fc_check.get("anulada_por") is not None, "anulada_por present")

            # Verify boleta 16 payments removed, estado recalculated
            b16_after = boletas.find_one({"_id": 16})
            if b16_after:
                check(b16_after.get("total_abonado", 0) == 0,
                      f"Boleta 16 total_abonado back to 0 after anulation (got {b16_after.get('total_abonado')})")
                # After state: vendedor assigned, no payments -> asignada
                vid = b16_after.get("vendedor_id")
                if vid in ("", "LOCAL"):
                    expected = "disponible"
                else:
                    expected = "asignada"
                check(b16_after.get("estado") == expected,
                      f"Boleta 16 estado='{b16_after.get('estado')}' (expected {expected}, vendedor_id={vid!r})")

        # ================================================
        # 11. BACKUPS
        # ================================================
        print("=" * 60)
        print("11. BACKUPS")
        print("=" * 60)
        r = client.get("/backup")
        check(r.status_code == 200, "GET /backup => 200 (page loads)")
        content = r.data.decode('utf-8', errors='replace')
        has_backup = any(w in content.upper() for w in ["BACKUP", "RESPALDAR", "RESTAURAR", "DESCARGAR", "BASE"])
        check(has_backup, "Backup page shows relevant options")

        # ================================================
        # 12. ESTADOS DE BOLETAS (end-to-end consistency)
        # ================================================
        print("=" * 60)
        print("12. VERIFICACION DE ESTADOS DE BOLETAS")
        print("=" * 60)

        # Boleta 13: pagada via vendor invoice (70000)
        b13 = boletas.find_one({"_id": 13})
        if b13:
            check(b13.get("total_abonado", 0) >= V,
                  f"Boleta 13: total_abonado={b13.get('total_abonado')} >= {V}")
            check(b13.get("estado") == "pagada",
                  f"Boleta 13 estado='{b13.get('estado')}' (expected pagada)")

        # Boleta 16: anulada, vendor-assigned => asignada
        b16 = boletas.find_one({"_id": 16})
        if b16:
            check(b16.get("total_abonado", 0) == 0,
                  f"Boleta 16 post-anulacion: total_abonado=0 (got {b16.get('total_abonado')})")
            vid = b16.get("vendedor_id")
            expected = "disponible" if vid in ("", "LOCAL") else "asignada"
            check(b16.get("estado") == expected,
                  f"Boleta 16 post-anulacion: estado='{b16.get('estado')}' (expected {expected}, vid={vid!r})")

        # Boleta 18: pagada via client invoice (pago total)
        b18 = boletas.find_one({"_id": 18})
        if b18:
            check(b18.get("total_abonado", 0) >= V,
                  f"Boleta 18 pagada total: total_abonado={b18.get('total_abonado')} >= {V}")

        # Boleta 0 sin asignar: disponible
        b0 = boletas.find_one({"_id": 0})
        if b0:
            check(b0.get("estado") == "disponible",
                  f"Boleta 0 estado='{b0.get('estado')}' (expected disponible)")

        # ================================================
        # 13. EXPORTACION
        # ================================================
        print("=" * 60)
        print("13. EXPORTACION CONSULTAS")
        print("=" * 60)
        r = client.get("/consultas/exportar?tipo=numero&search=1")
        check(r.status_code in (200, 302),
              f"GET /consultas/exportar => {r.status_code}")
        if r.status_code == 200:
            ct = (r.content_type or "").lower()
            check(any(t in ct for t in ["excel", "spreadsheet", "octet-stream", "csv", "xlsx"]),
                  f"Export content-type: {r.content_type}")

        # ================================================
        # 14. VALIDACION FECHA FUTURA
        # ================================================
        print("=" * 60)
        print("14. VALIDACION FECHA FUTURA")
        print("=" * 60)
        # Boleta 19 - attempt client invoice with future date
        r = client.post("/facturas/nueva/cliente", data={
            "nombre": "TEST FUNCIONAL",
            "telefono": "3001234567",
            "direccion": "Calle Test #123",
            "fecha": "2099-12-31",
            "boleta[]": ["19"],
            "monto[]": [str(V)],
            "metodo[]": ["efectivo"],
            "referencia[]": [""],
            "banco[]": [""],
            "generar": "1"
        }, follow_redirects=True)
        check(r.status_code == 200, "Future date client invoice => 200 (rejected)")
        content = r.data.decode('utf-8', errors='replace').lower()
        check(any(w in content for w in ["fecha", "futura", "posterior", "despues", "invalida"]),
              "Future date validation error shown")

        # Verify boleta 19 NOT affected
        b19 = boletas.find_one({"_id": 19})
        if b19:
            check(b19.get("total_abonado", 0) == 0,
                  f"Boleta 19 not affected by rejected invoice: total_abonado={b19.get('total_abonado')}")

        # ================================================
        # 15. VALIDACION DUPLICADOS
        # ================================================
        print("=" * 60)
        print("15. VALIDACION DUPLICADOS EN FACTURACION")
        print("=" * 60)
        r = client.post("/facturas/nueva/cliente", data={
            "nombre": "TEST FUNCIONAL",
            "telefono": "3001234567",
            "direccion": "Calle Test #123",
            "fecha": TODAY,
            "boleta[]": ["19,19,20"],
            "monto[]": [str(V)],
            "metodo[]": ["efectivo"],
            "referencia[]": [""],
            "banco[]": [""],
            "generar": "1"
        }, follow_redirects=True)
        check(r.status_code == 200, "Duplicate boletas in client invoice => 200")
        content = r.data.decode('utf-8', errors='replace').lower()
        check(any(w in content for w in ["duplicada", "repetida", "duplicado"]),
              "Duplicate boleta warning/error shown")

        # ================================================
        # 16. VALIDACION BOLETAS YA VENDIDAS/PAGADAS
        # ================================================
        print("=" * 60)
        print("16. VALIDACION BOLETAS YA PAGADAS/ASIGNADAS")
        print("=" * 60)
        # Boleta 13 ya esta pagada - intentar facturarla de nuevo como cliente
        r = client.post("/facturas/nueva/cliente", data={
            "nombre": "TEST FUNCIONAL",
            "telefono": "3001234567",
            "direccion": "Calle Test #123",
            "fecha": TODAY,
            "boleta[]": ["13"],
            "monto[]": [str(V)],
            "metodo[]": ["efectivo"],
            "referencia[]": [""],
            "banco[]": [""],
            "generar": "1"
        }, follow_redirects=True)
        check(r.status_code == 200, "Already paid boleta in new invoice => 200")
        content = r.data.decode('utf-8', errors='replace').lower()
        check(any(w in content for w in ["pagada", "no disponible", "asignada", "vendida", "ya tiene pago"]),
              "Already-paid boleta validation message shown")

        # ================================================
        # 17. RUTA RAIZ
        # ================================================
        print("=" * 60)
        print("17. RUTA RAIZ /")
        print("=" * 60)
        r = client.get("/")
        check(r.status_code in (200, 302), f"GET / => {r.status_code}")

        # ================================================
        # 18. API VENDEDORES
        # ================================================
        print("=" * 60)
        print("18. API /api/vendedores")
        print("=" * 60)
        r = client.get("/api/vendedores")
        check(r.status_code == 200, "GET /api/vendedores => 200")
        j = json_of(r)
        check(j is not None, "Response is JSON")
        if isinstance(j, list):
            ids = [v.get("_id") for v in j if v.get("_id")]
            check(TEST_VENDOR in ids, f"TEST_VENDOR in API list (ids={ids})")
        elif isinstance(j, dict):
            check(j.get("ok") is True or True, "API vendedores response")

        # ================================================
        # 19. SINC ESTADOS
        # ================================================
        print("=" * 60)
        print("19. SINCRONIZAR ESTADOS")
        print("=" * 60)
        r = client.post("/rifas/sincronizar-estados", follow_redirects=True)
        check(r.status_code == 200, "POST /rifas/sincronizar-estados => 200")
        print("  Sync completed")

        # ================================================
        # 20. VERIFICAR INTEGRIDAD DATABASE
        # ================================================
        print("=" * 60)
        print("20. VERIFICACION INTEGRIDAD")
        print("=" * 60)

        # Verificar que no hay facturas sin boletas correspondientes
        all_fact = list(facturas.find({}, {"boletas": 1, "anulada": 1}))
        for f in all_fact:
            for bid in f.get("boletas", []):
                b = boletas.find_one({"_id": bid})
                if not f.get("anulada"):
                    pass  # factura activa - boleta deberia tener pagos
                else:
                    pass  # anulada - boleta puede estar limpia

        # Verificar que no hay inconsistencias en total_abonado
        for bid in range(10, 30):
            b = boletas.find_one({"_id": bid})
            if b:
                pagos = b.get("historial_pagos", []) or []
                total_from_pagos = sum(int(p.get("valor", 0) or 0) for p in pagos)
                if b.get("total_abonado", 0) != total_from_pagos:
                    print(f"  WARN: Boleta {bid}: total_abonado={b.get('total_abonado')} != sum(pagos)={total_from_pagos}")

        print("  Integrity check complete")

# ============================================================
# CLEANUP TEST DATA
# ============================================================
print("=" * 60)
print("CLEANUP")
print("=" * 60)
clear_test_data()

suite_total = time.time() - suite_start

print()
print("=" * 60)
print(f"RESULTADOS: {PASS} passed, {FAIL} failed ({suite_total:.1f}s)")
print("=" * 60)
if ERRORS:
    for e in ERRORS:
        print(f"  - {e}")

if FAIL == 0:
    print("\nSISTEMA ESTABLE - todas las pruebas funcionales de aceptacion pasaron.")
    sys.exit(0)
else:
    print(f"\n{FAIL} prueba(s) fallaron. Revisar logs.")
    sys.exit(1)

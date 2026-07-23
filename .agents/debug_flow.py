"""Debug script - check vendor invoice flow"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['MONGO_DB'] = 'sistema_boleteria'

from app import app
from database import boletas, vendedores, facturas

vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR'}})
facturas.delete_many({'cliente.nombre': 'TEST FUNCIONAL'})

with app.test_client() as client:
    # 1. Create vendor
    r = client.post('/vendedores', data={
        'operacion': 'guardar',
        'vendedor_id': 'TEST_VENDOR_FUNC',
        'nombre': 'TEST VENDOR FUNC',
        'telefono': '3001112233'
    }, follow_redirects=True)
    print(f'1. Crear vendedor: status={r.status_code}')
    
    v = vendedores.find_one({'_id': 'TEST_VENDOR_FUNC'})
    print(f'   Vendor in DB: {v}')
    
    # 2. Assign tickets
    r = client.post('/vendedores', data={
        'operacion': 'asignar',
        'vendedor_id': 'TEST_VENDOR_FUNC',
        'boletas': '10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29',
    }, follow_redirects=True)
    print(f'2. Asignar: status={r.status_code}')
    
    b13 = boletas.find_one({'_id': 13})
    print(f'   Boleta 13: vendedor_id={b13["vendedor_id"]}, estado={b13["estado"]}')
    
    # 3. Create vendor invoice
    r = client.post('/facturas/nueva/vendedor', data={
        'vendedor_id': 'TEST_VENDOR_FUNC',
        'metodo_pago_0': 'efectivo',
        'monto_0': '70000',
        'boletas_0': '13,14',
        'metodo_pago_1': 'transferencia',
        'monto_1': '70000',
        'boletas_1': '15',
        'referencia_1': 'TRF-TEST-001',
        'observaciones': 'Pago completo de prueba',
        'generar': '1'
    }, follow_redirects=True)
    print(f'3. Factura vendedor: status={r.status_code}, path={r.request.path}')
    
    html = r.data.decode('utf-8', errors='replace')
    # Check for flash messages
    flash_matches = re.findall(r'class="alert[^"]*(?:danger|error|warning|success|info)[^"]*"[^>]*>\s*(.*?)\s*</div>', html, re.DOTALL)
    for fm in flash_matches[:5]:
        clean = re.sub(r'<[^>]+>', '', fm).strip()
        if clean:
            print(f'   Flash: {clean}')
    
    f = facturas.find_one({'vendedor_id': 'TEST_VENDOR_FUNC'}, sort=[('_id', -1)])
    print(f'   Vendor factura in DB: {f["_id"] if f else "NONE"}')
    if f:
        b13 = boletas.find_one({'_id': 13})
        print(f'   Boleta 13 post-factura: abonado={b13.get("total_abonado")}, estado={b13.get("estado")}')
        print(f'   historial_pagos: {b13.get("historial_pagos")}')

    # 4. Create client invoice
    r = client.post('/facturas/nueva/cliente', data={
        'nombre': 'TEST FUNCIONAL',
        'telefono': '3001234567',
        'direccion': 'Calle Test #123',
        'metodo_pago_0': 'efectivo',
        'monto_0': '70000',
        'boletas_0': '18',
        'observaciones': 'Pago total de prueba',
        'generar': '1'
    }, follow_redirects=True)
    print(f'4. Factura cliente: status={r.status_code}')
    
    html = r.data.decode('utf-8', errors='replace')
    flash_matches = re.findall(r'class="alert[^"]*(?:danger|error|warning|success|info)[^"]*"[^>]*>\s*(.*?)\s*</div>', html, re.DOTALL)
    for fm in flash_matches[:5]:
        clean = re.sub(r'<[^>]+>', '', fm).strip()
        if clean:
            print(f'   Flash: {clean}')
    
    fc = facturas.find_one({'cliente.nombre': 'TEST FUNCIONAL'}, sort=[('_id', -1)])
    print(f'   Client factura in DB: {fc["_id"] if fc else "NONE"}')

# Cleanup
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR'}})
facturas.delete_many({'cliente.nombre': 'TEST FUNCIONAL'})

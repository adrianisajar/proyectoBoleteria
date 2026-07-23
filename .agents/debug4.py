"""Debug - check vendor factura list content"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['MONGO_DB'] = 'sistema_boleteria'

from app import app
from datetime import date
from database import boletas, vendedores, facturas
import re

TODAY = date.today().strftime('%Y-%m-%d')

# Clean
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'cliente.nombre': 'TEST FUNCIONAL'})
for bid in range(10, 30):
    boletas.update_one({'_id': bid}, {
        '$set': {'cliente': {'nombre': '', 'telefono': '', 'direccion': ''}, 'vendedor_id': '', 'total_abonado': 0, 'historial_pagos': [], 'estado': 'disponible'}
    })

with app.test_client() as client:
    # Create vendor
    client.post('/vendedores', data={
        'operacion': 'guardar', 'vendedor_id': 'TEST_VENDOR_FUNC',
        'nombre': 'TEST VENDOR FUNC', 'telefono': '3001112233'
    }, follow_redirects=True)
    
    v = vendedores.find_one({'_id': 'TEST_VENDOR_FUNC'})
    print('Vendor: %s' % str(v))
    
    # Assign
    client.post('/vendedores', data={
        'operacion': 'asignar', 'vendedor_id': 'TEST_VENDOR_FUNC',
        'boletas': '13,14,15',
    }, follow_redirects=True)
    
    # Create vendor invoice
    r = client.post('/facturas/nueva/vendedor', data={
        'vendedor_id': 'TEST_VENDOR_FUNC', 'fecha': TODAY,
        'boleta[]': ['13,14', '15'], 'monto[]': ['70000', '70000'],
        'metodo[]': ['efectivo', 'transferencia'],
        'referencia[]': ['', 'TRF-1'], 'banco[]': ['', 'BANCO'],
    }, follow_redirects=True)
    print('Post vendor invoice: status=%d' % r.status_code)
    
    f = facturas.find_one({'vendedor_id': 'TEST_VENDOR_FUNC'}, sort=[('_id', -1)])
    if f:
        print('Factura found: _id=%d, vendedor_nombre=%r, tipo=%r' % (f['_id'], f.get('vendedor_nombre'), f.get('tipo')))
    
    # GET the vendor facturas list
    r = client.get('/facturas/vendedor')
    print('GET /facturas/vendedor: status=%d' % r.status_code)
    content = r.data.decode('utf-8', errors='replace')
    has_vendor = 'TEST VENDOR FUNC' in content
    print('  Contains "TEST VENDOR FUNC": %s' % has_vendor)
    # Show snippet around the vendor name
    idx = content.find('TEST')
    if idx > 0:
        print('  Snippet: ...%s...' % content[max(0,idx-50):idx+100])
    else:
        print('  First 500 chars: %s' % content[:500])

# Cleanup
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
for bid in range(10, 30):
    boletas.update_one({'_id': bid}, {
        '$set': {'vendedor_id': '', 'total_abonado': 0, 'historial_pagos': [], 'estado': 'disponible'}
    })

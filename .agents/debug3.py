"""Debug script - check vendor creation and assignment"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['MONGO_DB'] = 'sistema_boleteria'

from app import app
from database import boletas, vendedores, facturas

# Clean slate
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})

with app.test_client() as client:
    # Create vendor - check response
    r = client.post('/vendedores', data={
        'operacion': 'guardar',
        'vendedor_id': 'TEST_VENDOR_FUNC',
        'nombre': 'TVF',
        'telefono': '111'
    }, follow_redirects=True)
    print('POST create vendor: status=%d' % r.status_code)
    
    vcheck = vendedores.find_one({'_id': 'TEST_VENDOR_FUNC'})
    print('Vendor after creation: %s' % str(vcheck))
    
    # Now assign
    r = client.post('/vendedores', data={
        'operacion': 'asignar',
        'vendedor_id': 'TEST_VENDOR_FUNC',
        'boletas': '13,14,15,16',
    }, follow_redirects=True)
    print('POST assign: status=%d' % r.status_code)
    
    # Check flash messages
    html = r.data.decode('utf-8', errors='replace')
    flash_matches = re.findall(r'class="alert[^"]*"[^>]*>\s*(.*?)\s*</div>', html, re.DOTALL)
    for fm in flash_matches[:5]:
        clean = re.sub(r'<[^>]+>', '', fm).strip()
        if clean:
            print('  Flash: %s' % clean)
    
    b13 = boletas.find_one({'_id': 13})
    print('Boleta 13: vendedor_id=%r, estado=%r' % (b13['vendedor_id'], b13['estado']))
    
    b16 = boletas.find_one({'_id': 16})
    print('Boleta 16: vendedor_id=%r, estado=%r' % (b16['vendedor_id'], b16['estado']))

# Cleanup
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})

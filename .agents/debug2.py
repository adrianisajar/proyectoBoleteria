"""Debug script - check anulation behavior specifically"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['MONGO_DB'] = 'sistema_boleteria'

from app import app
from datetime import date
from database import boletas, vendedores, facturas

TODAY = date.today().strftime('%Y-%m-%d')
V = 70000

# Clean slate
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'cliente.nombre': 'TEST FUNCIONAL'})
boletas.update_many({'cliente.nombre': 'TEST FUNCIONAL'}, {
    '$set': {'cliente': {'nombre': '', 'telefono': '', 'direccion': ''}, 'vendedor_id': '', 'total_abonado': 0, 'historial_pagos': []},
    '$unset': {'factura_id': ''}
})

with app.test_client() as client:
    # Create vendor
    client.post('/vendedores', data={'operacion': 'guardar', 'vendedor_id': 'TEST_VENDOR_FUNC', 'nombre': 'TVF', 'telefono': '111'}, follow_redirects=True)
    
    # Assign boletas 13-29
    client.post('/vendedores', data={'operacion': 'asignar', 'vendedor_id': 'TEST_VENDOR_FUNC', 'boletas': '13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29'}, follow_redirects=True)
    
    b16 = boletas.find_one({'_id': 16})
    print('After assignment: vendedor_id=%r, estado=%r' % (b16['vendedor_id'], b16['estado']))
    
    # Create vendor invoice for 13,14,15
    client.post('/facturas/nueva/vendedor', data={
        'vendedor_id': 'TEST_VENDOR_FUNC', 'fecha': TODAY,
        'boleta[]': ['13,14', '15'], 'monto[]': ['70000', '70000'],
        'metodo[]': ['efectivo', 'transferencia'],
        'referencia[]': ['', 'TRF-1'], 'banco[]': ['', 'BANCO'],
    }, follow_redirects=True)
    
    b16 = boletas.find_one({'_id': 16})
    print('After vendor invoice: vendedor_id=%r, estado=%r, abonado=%s' % (b16['vendedor_id'], b16['estado'], b16['total_abonado']))
    
    # Create client abono for 16,17
    r = client.post('/facturas/nueva/cliente', data={
        'nombre': 'TEST FUNCIONAL', 'telefono': '300', 'direccion': 'CALLE',
        'fecha': TODAY,
        'boleta[]': ['16', '17'], 'monto[]': ['35000', '35000'],
        'metodo[]': ['efectivo', 'efectivo'],
        'referencia[]': ['', ''], 'banco[]': ['', ''],
    }, follow_redirects=True)
    
    b16 = boletas.find_one({'_id': 16})
    print('After client invoice: vendedor_id=%r, estado=%r, abonado=%s' % (b16['vendedor_id'], b16['estado'], b16['total_abonado']))
    print('  cliente=%s' % str(b16['cliente']))
    print('  historial_pagos=%s' % str(b16['historial_pagos']))
    
    f = facturas.find_one({'boletas': 16}, sort=[('_id', -1)])
    print('Factura: id=%s' % (str(f['_id']) if f else 'NONE'))
    
    if f:
        fid = f['_id']
        # Get hash from view
        rv = client.get('/facturas/%d' % fid)
        html = rv.data.decode('utf-8', errors='replace')
        hm = re.search(r'name="anulacion_hash"[^>]*value="([^"]+)"', html)
        h = hm.group(1) if hm else ''
        
        print('  hash=%s' % h)
        
        # Anulate
        r2 = client.post('/facturas/%d/anular' % fid, data={
            'motivo': 'TEST',
            'anulacion_hash': h,
        }, follow_redirects=True)
        print('  anulation response status=%d, path=%s' % (r2.status_code, r2.request.path))
        
        b16 = boletas.find_one({'_id': 16})
        print('After anulation: vendedor_id=%r, estado=%r, abonado=%s' % (b16['vendedor_id'], b16['estado'], b16['total_abonado']))
        print('  historial_pagos=%s' % str(b16['historial_pagos']))
        
        fc = facturas.find_one({'_id': fid})
        print('  factura anulada=%s, motivo=%s' % (str(fc.get('anulada')), fc.get('motivo_anulacion')))

# Cleanup
vendedores.delete_many({'_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'vendedor_id': {'$regex': 'TEST_VENDOR', '$options': 'i'}})
facturas.delete_many({'cliente.nombre': 'TEST FUNCIONAL'})

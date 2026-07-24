"""Test premios adicionales logic"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['MONGO_DB'] = 'sistema_boleteria'
from motores.shared import calcular_premios_adicionales

V = 20000
premios = [
    {'nombre': 'Premio 1', 'fecha_juego': '2026-06-01'},
    {'nombre': 'Premio 2', 'fecha_juego': '2026-08-01'},
    {'nombre': 'Premio 3', 'fecha_juego': '2026-10-01'},
]

def test(label, pagos, expected):
    result = calcular_premios_adicionales(pagos, premios, V)
    parts = ['%s=%s' % (r['nombre'], 'SI' if r['participa'] else 'NO') for r in result]
    ok = [r['participa'] for r in result] == expected
    print('  %s %s: %s' % ('OK' if ok else 'FAIL', label, ', '.join(parts)))

print('Ejemplo 1 (pagos perfectos):')
test('P1', [{'fecha': '2026-05-01', 'valor': 20000}], [True, False, False])
test('P2', [{'fecha': '2026-05-01', 'valor': 20000}, {'fecha': '2026-07-01', 'valor': 20000}], [True, True, False])
test('P3', [{'fecha': '2026-05-01', 'valor': 20000}, {'fecha': '2026-07-01', 'valor': 20000}, {'fecha': '2026-09-01', 'valor': 20000}], [True, True, True])

print('Ejemplo 2 (pago tardio):')
test('Solo P2', [{'fecha': '2026-07-15', 'valor': 20000}], [False, True, False])

print('Ejemplo 3 ($40k antes de P1):')
test('40k', [{'fecha': '2026-05-01', 'valor': 40000}], [True, True, False])

print('Ejemplo 4 ($60k antes de P1):')
test('60k', [{'fecha': '2026-05-01', 'valor': 60000}], [True, True, True])

print('Ejemplo 5 (pagada completa):')
test('100k', [{'fecha': '2026-05-01', 'valor': 100000}], [True, True, True])

print('Pago el mismo dia del premio:')
test('mismo dia', [{'fecha': '2026-06-01', 'valor': 20000}], [True, False, False])

print('Sin pagos:')
test('sin pagos', [], [False, False, False])

print('Sin premios:')
r = calcular_premios_adicionales([{'fecha': '2026-05-01', 'valor': 20000}], [], V)
print('  OK: vacio=%s' % (r == []))

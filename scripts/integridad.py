"""Verificador de integridad del libro mayor (ledger) de la boletería.

Uso:
    python scripts/integridad.py [verificar]   # solo lectura (por defecto)
    python scripts/integridad.py reparar       # repara lo reparable y re-verifica

Verifica:
  - Boletas: ``total_abonado`` == neto de ``historial_movimientos``
    (pago + traslado_entrada - traslado_salida), ``estado`` derivado,
    ``vendedor_id`` existente y movimientos con tipo/valor válidos.
  - Vendedores: ``boletas_asignadas`` coherentes con el ``vendedor_id`` de cada
    boleta (en ambos sentidos).
  - Facturas: ``valor_total`` == suma del ``detalle``, ``factura_counter`` >=
    id máximo de factura.

``reparar`` restaura los movimientos ``pago`` faltantes desde el ``detalle``
persistido de las facturas (incidente histórico conocido) SIN modificar
``total_abonado`` ni ``estado``. Todo lo que no se pueda reparar automáticamente
queda reportado como error para revisión manual.

Exit code: 0 = sin errores (o todos reparados), 1 = quedan errores.
"""

import argparse
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from database import boletas, configuracion, facturas, vendedores  # noqa: E402
from motores.cache import invalidate_dashboard_cache  # noqa: E402
from motores.constants import (  # noqa: E402
    BOLETA_MAX,
    BOLETA_MIN,
    CONFIG_ID,
    MOV_EGRESO,
    MOV_PAGO,
    MOV_TRASLADO_ENTRADA,
    MOV_TRASLADO_SALIDA,
    MOVIMIENTOS_FIELD,
    USUARIO_SISTEMA,
    VENDEDOR_LOCAL,
)
from motores.fechas import now_local  # noqa: E402
from motores.ticket_service import estado_para_total  # noqa: E402

TIPOS_MOV = {MOV_PAGO, MOV_EGRESO, MOV_TRASLADO_ENTRADA, MOV_TRASLADO_SALIDA}
TIPOS_FACTURA = {"cliente", "vendedor", "egreso"}


def _es_numero_valido(value) -> bool:
    """Return True for real numbers (ints/floats, excluding bools)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _neto_ledger(movimientos: list) -> int:
    """Net `total_abonado` that the ledger implies (income minus traslado_salida)."""
    neto = 0
    for mov in movimientos or []:
        if not isinstance(mov, dict):
            continue
        valor = mov.get("valor")
        if not _es_numero_valido(valor):
            continue
        tipo = mov.get("tipo") or MOV_PAGO
        if tipo in (MOV_PAGO, MOV_TRASLADO_ENTRADA):
            neto += int(valor)
        elif tipo == MOV_TRASLADO_SALIDA:
            neto -= int(valor)
    return max(neto, 0)


def _movimientos_faltantes(movimientos: list, detalle_lines: list) -> list:
    """Detalle lines without a matching pago movement in the ticket ledger.

    A movement matches a detalle line when it has the same ``factura_id`` and
    ``valor``; duplicates are accounted for one-to-one.
    """
    disponibles = Counter(
        (mov.get("factura_id"), int(mov.get("valor")))
        for mov in movimientos or []
        if isinstance(mov, dict)
        and (mov.get("tipo") or MOV_PAGO) == MOV_PAGO
        and _es_numero_valido(mov.get("valor"))
        and isinstance(mov.get("factura_id"), int)
    )
    faltantes = []
    for fid, linea in detalle_lines:
        valor = linea.get("valor")
        if not _es_numero_valido(valor):
            continue
        clave = (fid, int(valor))
        if disponibles.get(clave, 0) > 0:
            disponibles[clave] -= 1
            continue
        faltantes.append((fid, dict(linea)))
    return faltantes


def verificar() -> tuple[list[str], list[str], dict[int, list]]:
    """Run all checks. Returns (errores, advertencias, reparables).

    ``reparables`` maps boleta_id -> [(factura_id, detalle_line)]: pago movements
    missing from the ledger that can be restored automatically.
    """
    errores: list[str] = []
    advertencias: list[str] = []
    reparables: dict[int, list] = defaultdict(list)

    if boletas is None or vendedores is None or configuracion is None or facturas is None:
        errores.append("No hay conexión activa a MongoDB.")
        return errores, advertencias, reparables

    config = configuracion.find_one({"_id": CONFIG_ID})
    if config is None:
        errores.append("configuracion: falta el documento '_id=rifa'.")
        return errores, advertencias, reparables
    valor_boleta = int(config.get("valor_boleta", 0) or 0)
    if valor_boleta <= 0:
        errores.append("configuracion: valor_boleta debe ser mayor que cero.")
        return errores, advertencias, reparables

    # ── Facturas: índice + validación básica ─────────────────────────
    factura_ids: set[int] = set()
    detalle_por_boleta: dict[int, list] = defaultdict(list)
    max_factura = 0
    for factura in facturas.find({}):
        fid = factura.get("_id")
        if not _es_numero_valido(fid) or fid <= 0:
            errores.append(f"facturas: _id inválido {fid!r}")
            continue
        fid = int(fid)
        factura_ids.add(fid)
        max_factura = max(max_factura, fid)
        if factura.get("tipo") not in TIPOS_FACTURA:
            errores.append(f"factura #{fid}: tipo inválido {factura.get('tipo')!r}")
        detalle = factura.get("detalle")
        if not isinstance(detalle, list):
            errores.append(f"factura #{fid}: detalle no es una lista")
            detalle = []
        suma = 0
        for linea in detalle:
            if not isinstance(linea, dict):
                continue
            valor = linea.get("valor")
            if not _es_numero_valido(valor):
                errores.append(f"factura #{fid}: línea de detalle sin valor válido")
                continue
            suma += int(valor)
            bnum = linea.get("boleta")
            if _es_numero_valido(bnum) and BOLETA_MIN <= int(bnum) <= BOLETA_MAX:
                detalle_por_boleta[int(bnum)].append((fid, linea))
        vtotal = factura.get("valor_total")
        if not _es_numero_valido(vtotal):
            errores.append(f"factura #{fid}: valor_total inválido")
        elif not factura.get("es_general") and int(vtotal) != suma:
            errores.append(f"factura #{fid}: valor_total ({int(vtotal)}) != suma del detalle ({suma})")

    counter = config.get("factura_counter")
    if _es_numero_valido(counter) and int(counter) < max_factura:
        errores.append(f"configuracion: factura_counter ({int(counter)}) es menor que el id máximo de factura ({max_factura})")

    # ── Vendedores: índice de ids y asignaciones ─────────────────────
    vendedor_ids: set[str] = set()
    asignaciones: dict[str, set[int]] = {}
    for v in vendedores.find({}):
        vendedor_ids.add(v["_id"])
        asignadas = v.get("boletas_asignadas")
        if not isinstance(asignadas, list):
            errores.append(f"vendedor {v['_id']}: boletas_asignadas no es una lista")
            asignaciones[v["_id"]] = set()
            continue
        asignaciones[v["_id"]] = {int(n) for n in asignadas if _es_numero_valido(n) and BOLETA_MIN <= int(n) <= BOLETA_MAX}

    # ── Boletas (streaming cursor — no full list in RAM) ──────────────
    boleta_vendedor: dict[int, str] = {}
    _boleta_proj = {"_id": 1, "total_abonado": 1, "estado": 1, "vendedor_id": 1, "cliente": 1, MOVIMIENTOS_FIELD: 1}
    for doc in boletas.find({}, _boleta_proj).batch_size(500):
        bid = doc.get("_id")
        if not _es_numero_valido(bid) or not (BOLETA_MIN <= bid <= BOLETA_MAX):
            errores.append(f"boletas: _id inválido {bid!r}")
            continue
        bid = int(bid)
        movimientos = doc.get(MOVIMIENTOS_FIELD)
        if movimientos is None:
            movimientos = []
        elif not isinstance(movimientos, list):
            errores.append(f"boleta #{bid:04d}: historial_movimientos no es una lista")
            movimientos = []

        neto = _neto_ledger(movimientos)
        total = doc.get("total_abonado")
        if not _es_numero_valido(total) or int(total) < 0:
            errores.append(f"boleta #{bid:04d}: total_abonado inválido")
            continue
        total = int(total)
        if total != neto:
            if total > neto:
                candidatos = _movimientos_faltantes(movimientos, detalle_por_boleta.get(bid, []))
                if candidatos and sum(int(c[1].get("valor", 0) or 0) for c in candidatos) == total - neto:
                    reparables[bid] = candidatos
                    advertencias.append(f"boleta #{bid:04d}: faltaban {len(candidatos)} movimiento(s) de pago (recuperables desde el detalle de facturas).")
                else:
                    errores.append(
                        f"boleta #{bid:04d}: total_abonado ({total}) != neto del ledger ({neto}). No se puede reparar automáticamente; revisar a mano."
                    )
            else:
                errores.append(f"boleta #{bid:04d}: total_abonado ({total}) < neto del ledger ({neto}). Revisar a mano.")

        vendedor_id = doc.get("vendedor_id") or ""
        boleta_vendedor[bid] = vendedor_id
        if vendedor_id not in ("", VENDEDOR_LOCAL) and vendedor_id not in vendedor_ids:
            errores.append(f"boleta #{bid:04d}: vendedor_id {vendedor_id!r} no existe en la colección vendedores.")

        esperado = estado_para_total(
            total,
            valor_boleta,
            vendedor_id=vendedor_id or None,
            cliente_nombre=(doc.get("cliente") or {}).get("nombre", ""),
        )
        estado = doc.get("estado")
        if estado is not None and estado != esperado:
            errores.append(f"boleta #{bid:04d}: estado {estado!r} != esperado {esperado!r}.")

        for mov in movimientos:
            if not isinstance(mov, dict):
                errores.append(f"boleta #{bid:04d}: movimiento no es un dict")
                continue
            tipo = mov.get("tipo")
            if tipo is not None and tipo not in TIPOS_MOV:
                errores.append(f"boleta #{bid:04d}: tipo de movimiento inválido {tipo!r}")
            if not _es_numero_valido(mov.get("valor")) or int(mov.get("valor")) <= 0:
                errores.append(f"boleta #{bid:04d}: movimiento con valor inválido")
            fid = mov.get("factura_id")
            if fid is not None and _es_numero_valido(fid) and int(fid) not in factura_ids:
                advertencias.append(f"boleta #{bid:04d}: movimiento referencia a factura #{int(fid)} inexistente.")

        if vendedor_id and vendedor_id != VENDEDOR_LOCAL and bid not in asignaciones.get(vendedor_id, set()):
            errores.append(f"boleta #{bid:04d}: asignada a {vendedor_id} pero no está en sus boletas_asignadas.")

    # ── Vendedores: asignaciones → boletas existentes y coincidencia ─
    for vendedor_id, ids in asignaciones.items():
        for numero in sorted(ids):
            vendedor_boleta = boleta_vendedor.get(numero)
            if vendedor_boleta is None:
                errores.append(f"vendedor {vendedor_id}: boleta #{numero:04d} asignada no existe.")
            elif vendedor_boleta != vendedor_id and vendedor_boleta and vendedor_boleta != VENDEDOR_LOCAL:
                errores.append(f"vendedor {vendedor_id}: boleta #{numero:04d} asignada pertenece a {vendedor_boleta!r}.")

    return errores, advertencias, reparables


def reparar() -> tuple[int, list[str]]:
    """Restore missing pago movements from factura detalle (no totals/estado touched).

    Uses ``$push`` with ``$each`` for atomic appends and deduplicates against
    existing ``(factura_id, valor)`` pairs before writing.

    Returns (reinsertados, errores_restantes_despues_de_reparar).
    """
    errores, _advertencias, reparables = verificar()
    if boletas is None:
        print("[ERROR] No hay conexión activa a MongoDB.", file=sys.stderr)
        return 0, ["sin conexión"]
    reinsertados = 0
    for bid, lineas in sorted(reparables.items()):
        doc = boletas.find_one(
            {"_id": bid},
            {MOVIMIENTOS_FIELD: 1, "total_abonado": 1},
        )
        if doc is None:
            continue
        existentes = doc.get(MOVIMIENTOS_FIELD) or []
        vistas: set[tuple[int, int]] = set()
        for mov in existentes:
            if isinstance(mov, dict) and _es_numero_valido(mov.get("factura_id")) and _es_numero_valido(mov.get("valor")):
                vistas.add((int(mov["factura_id"]), int(mov["valor"])))
        nuevos = []
        for fid, linea in lineas:
            clave = (int(fid), int(linea.get("valor", 0) or 0))
            if clave in vistas:
                continue
            vistas.add(clave)
            mov = {
                "tipo": MOV_PAGO,
                "fecha": str(linea.get("fecha", "")),
                "valor": int(linea.get("valor", 0) or 0),
                "metodo": linea.get("metodo", ""),
                "registrado_en": now_local(),
                "usuario": USUARIO_SISTEMA,
                "factura_id": int(fid),
            }
            if linea.get("referencia"):
                mov["referencia"] = linea["referencia"]
            if linea.get("banco"):
                mov["banco"] = linea["banco"]
            nuevos.append(mov)
            reinsertados += 1
            print(f"[REPARAR] boleta #{bid:04d}: reinsertado pago ${mov['valor']:,} (factura #{fid}).")
        if nuevos:
            boletas.update_one(
                {"_id": bid},
                {"$push": {MOVIMIENTOS_FIELD: {"$each": nuevos}}},
            )
    if reinsertados:
        invalidate_dashboard_cache()
    _, _, reparables_restantes = verificar()
    if reparables_restantes:
        errores.append(f"quedan {sum(len(v) for v in reparables_restantes.values())} boleta(s) por revisar a mano.")
    return reinsertados, errores


def _reporte(errores: list[str], advertencias: list[str], reparables: dict[int, list]) -> None:
    total_reparables = sum(len(v) for v in reparables.values())
    if errores:
        print(f"ERRORES ({len(errores)}):")
        for e in errores[:60]:
            print(f"  [E] {e}")
        if len(errores) > 60:
            print(f"  ... y {len(errores) - 60} más.")
    else:
        print("ERRORES: ninguno.")
    if advertencias:
        print(f"ADVERTENCIAS ({len(advertencias)}):")
        for a in advertencias[:40]:
            print(f"  [W] {a}")
        if len(advertencias) > 40:
            print(f"  ... y {len(advertencias) - 40} más.")
    if total_reparables:
        print(f"REPARABLES ({total_reparables} movimiento(s) en {len(reparables)} boleta(s)): ejecuta `python scripts/integridad.py reparar`.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica/repara la integridad del libro mayor.")
    parser.add_argument("accion", nargs="?", default="verificar", choices=["verificar", "reparar"])
    args = parser.parse_args()

    if args.accion == "reparar":
        reinsertados, errores = reparar()
        print()
        print(f"[RESUMEN] movimientos reinsertados: {reinsertados}.")
        if errores:
            print(f"[RESUMEN] errores restantes ({len(errores)}):")
            for e in errores[:40]:
                print(f"  [E] {e}")
            return 1
        print("[RESUMEN] Sin errores después de la reparación.")
        return 0

    errores, advertencias, reparables = verificar()
    print("=== Integridad del libro mayor ===")
    _reporte(errores, advertencias, reparables)
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())

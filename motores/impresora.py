"""ESC/POS thermal printer driver — sends receipts directly via TCP socket.

Configure in .env::

    PRINTER_HOST=192.168.1.100
    PRINTER_PORT=9100

If ``PRINTER_HOST`` is empty the printer is disabled and all operations are no-ops.
"""

import os
import socket

from flask import current_app

# ── ESC/POS constants ──────────────────────────────────────────────
ESC = b"\x1b"
GS = b"\x1d"

INIT = ESC + b"@"  # Initialize printer
BOLD_ON = ESC + b"E" + b"\x01"
BOLD_OFF = ESC + b"E" + b"\x00"
ALIGN_LEFT = ESC + b"a" + b"\x00"
ALIGN_CENTER = ESC + b"a" + b"\x01"
ALIGN_RIGHT = ESC + b"a" + b"\x02"
FEED_LINE = b"\n"


def feed_lines(n: int) -> bytes:
    return ESC + b"d" + bytes([n])


CUT = GS + b"V" + b"\x00"  # Full cut
LINE_WIDTH = 48  # characters for 80mm printer

PRINTER_HOST = os.getenv("PRINTER_HOST") or ""
PRINTER_PORT = int(os.getenv("PRINTER_PORT") or "9100")
PRINTER_TIMEOUT = 3  # seconds


def is_configured() -> bool:
    return bool(PRINTER_HOST)


def _send(data: bytes) -> None:
    """Open a TCP connection to the printer and send raw bytes."""
    if not PRINTER_HOST:
        return
    with socket.create_connection((PRINTER_HOST, PRINTER_PORT), timeout=PRINTER_TIMEOUT) as sock:
        sock.sendall(data)


def _center(text: str) -> bytes:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= LINE_WIDTH:
        return ALIGN_CENTER + encoded + FEED_LINE
    # Wrap long centered text
    buf = bytearray()
    for chunk in _wrap(text):
        buf += ALIGN_CENTER + chunk.encode("utf-8", errors="replace") + FEED_LINE
    return bytes(buf)


def _left(text: str) -> bytes:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= LINE_WIDTH:
        return ALIGN_LEFT + encoded + FEED_LINE
    buf = bytearray()
    for chunk in _wrap(text):
        buf += ALIGN_LEFT + chunk.encode("utf-8", errors="replace") + FEED_LINE
    return bytes(buf)


def _right(text: str) -> bytes:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= LINE_WIDTH:
        return ALIGN_RIGHT + encoded + FEED_LINE
    buf = bytearray()
    for chunk in _wrap(text):
        buf += ALIGN_RIGHT + chunk.encode("utf-8", errors="replace") + FEED_LINE
    return bytes(buf)


def _wrap(text: str) -> list[str]:
    """Word-wrap text to fit within LINE_WIDTH bytes."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = (current + " " + word).strip() if current else word
        if len(test.encode("utf-8", errors="replace")) <= LINE_WIDTH:
            current = test
        else:
            if current:
                lines.append(current)
            # If single word exceeds width, split by bytes
            if len(word.encode("utf-8", errors="replace")) > LINE_WIDTH:
                chunk = ""
                for ch in word:
                    if len((chunk + ch).encode("utf-8", errors="replace")) > LINE_WIDTH:
                        lines.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                current = chunk
            else:
                current = word
    if current:
        lines.append(current)
    return lines or [""]


def _line(left: str, right: str) -> bytes:
    """Print left-aligned and right-aligned on the same line.
    If combined text exceeds printer width, falls back to two lines."""
    left_enc = left.encode("utf-8", errors="replace")
    right_enc = right.encode("utf-8", errors="replace")
    combined_len = len(left_enc) + len(right_enc)
    if combined_len <= LINE_WIDTH - 1:
        pad = LINE_WIDTH - len(left_enc) - len(right_enc)
        if pad < 1:
            pad = 1
        return ALIGN_LEFT + left_enc + (b" " * pad) + right_enc + FEED_LINE
    # Too long — print on two separate lines
    buf = bytearray()
    buf += _left(left)
    buf += _right(right)
    return bytes(buf)


def _separator() -> bytes:
    return ALIGN_LEFT + (b"-" * LINE_WIDTH) + FEED_LINE


def _double_separator() -> bytes:
    return ALIGN_LEFT + (b"=" * LINE_WIDTH) + FEED_LINE


def _wrap_boletas(boletas: list[int]) -> list[str]:
    """Wrap boleta numbers across multiple lines to fit printer width."""
    lines: list[str] = []
    current = ""
    for b in boletas:
        token = f"{b:04d}"
        test = (current + " " + token).strip() if current else token
        if len(test.encode("utf-8", errors="replace")) <= LINE_WIDTH:
            current = test
        else:
            if current:
                lines.append(current)
            current = token
    if current:
        lines.append(current)
    return lines


def _centered_box(text: str) -> bytes:
    """Center text with surrounding dashes."""
    pad = (LINE_WIDTH - len(text) - 2) // 2
    if pad < 0:
        pad = 0
    line = "-" * pad + " " + text + " " + "-" * (LINE_WIDTH - len(text) - 2 - pad)
    return ALIGN_LEFT + line.encode("utf-8") + FEED_LINE


# ── Build receipt bytes ────────────────────────────────────────────


def build_cliente_receipt(factura: dict, config: dict, boletas_info: dict) -> bytes:
    """Build ESC/POS bytes for a customer invoice."""
    buf = bytearray()
    buf += INIT
    buf += BOLD_ON
    buf += _center(config.get("nombre_empresa") or config.get("nombre_rifa") or "BOLETERIA")
    buf += BOLD_OFF
    buf += _center("RECIBO DE PAGO / ABONO")
    empresa = config.get("direccion", "")
    if config.get("ciudad"):
        empresa += (", " if empresa else "") + config["ciudad"]
    if config.get("telefono"):
        empresa += " - " + config["telefono"]
    if empresa:
        buf += _center(empresa)
    buf += FEED_LINE
    buf += _separator()

    buf += _line("FACTURA", f"FC-{factura['_id']:05d}")
    fecha = factura.get("fecha_display", "")
    if fecha:
        buf += _line("FECHA", fecha)
    buf += _left("RIFA")
    buf += BOLD_ON + _left(config.get("nombre_rifa", "Rifa")) + BOLD_OFF
    buf += _separator()

    buf += BOLD_ON + _left("CLIENTE") + BOLD_OFF
    if factura.get("cliente", {}).get("nombre"):
        buf += _left(factura["cliente"]["nombre"])
    if factura.get("cliente", {}).get("telefono"):
        buf += _left(f"TEL {factura['cliente']['telefono']}")
    if factura.get("cliente", {}).get("direccion"):
        buf += _left(f"DIR {factura['cliente']['direccion']}")
    if factura.get("vendedor_id") and factura["vendedor_id"] != "LOCAL":
        buf += BOLD_ON + _left("VENDEDOR") + BOLD_OFF
        buf += _left(factura.get("vendedor_nombre", factura["vendedor_id"]))
    buf += _separator()

    valor_boleta = config.get("valor_boleta", 10000)
    total = 0
    for bid in factura.get("boletas", []):
        info = boletas_info.get(bid, {})
        boleta_pagos = [d for d in factura.get("detalle", []) if d.get("boleta") == bid]
        invoice_pago = sum(d.get("valor", 0) for d in boleta_pagos)
        total += invoice_pago

        buf += BOLD_ON
        buf += _left(f"#{bid:04d}")
        buf += BOLD_OFF
        buf += feed_lines(1)
        if info.get("vendedor_id") and info["vendedor_id"] != "LOCAL":
            buf += _left(f"  Vendedor: {info.get('vendedor_nombre', info['vendedor_id'])}")
        buf += _left(f"  Valor de la boleta: ${info.get('valor_boleta', valor_boleta):,}")
        buf += _left(f"  Pagado: ${invoice_pago:,}")
        for p in boleta_pagos:
            metodo = {"transferencia": "Transferencia", "pago_a_delio": "Pago a Delio"}.get(p.get("metodo", ""), "Efectivo")
            buf += _left(f"  Metodo: {metodo}")
            if p.get("metodo") == "transferencia":
                if p.get("banco"):
                    buf += _left(f"  Banco: {p['banco']}")
                if p.get("referencia"):
                    buf += _left(f"  Ref: {p['referencia']}")
        buf += feed_lines(1)
        buf += _separator()

    buf += BOLD_ON + _line("TOTAL", f"${total:,}") + BOLD_OFF
    buf += feed_lines(2)
    buf += _center("--- EMITIDO POR ---")
    buf += _center(factura.get("usuario_nombre") or "No registrado")
    buf += feed_lines(2)
    buf += _separator()
    buf += _center("Gracias por confiar en nosotros.")
    buf += _center("Conserve este comprobante.")
    buf += feed_lines(5)
    buf += CUT
    return bytes(buf)


def build_vendedor_receipt(factura: dict, config: dict) -> bytes:
    """Build ESC/POS bytes for a vendor invoice, grouped by amount."""
    buf = bytearray()
    buf += INIT
    buf += BOLD_ON
    buf += _center(config.get("nombre_empresa") or config.get("nombre_rifa") or "BOLETERIA")
    buf += BOLD_OFF
    buf += _center("COMPROBANTE DE RECAUDO")
    buf += FEED_LINE
    empresa = config.get("direccion", "")
    if config.get("ciudad"):
        empresa += (", " if empresa else "") + config["ciudad"]
    if config.get("telefono"):
        empresa += " - " + config["telefono"]
    if empresa:
        buf += _center(empresa)
    buf += feed_lines(2)
    buf += _separator()

    buf += _line("FACTURA", f"FV-{factura['_id']:05d}")
    fecha = factura.get("fecha_display", "")
    if fecha:
        buf += _line("FECHA", fecha)
    buf += _left("RIFA")
    buf += BOLD_ON + _left(config.get("nombre_rifa", "Rifa")) + BOLD_OFF
    buf += feed_lines(1)
    buf += _separator()

    buf += BOLD_ON + _left("VENDEDOR") + BOLD_OFF
    buf += _left(factura.get("vendedor_nombre", factura.get("vendedor_id", "")))
    if factura.get("vendedor_telefono"):
        buf += _left(f"TEL {factura['vendedor_telefono']}")
    buf += feed_lines(1)
    buf += _separator()

    valor_boleta = config.get("valor_boleta", 10000)
    detalle = factura.get("detalle") or []
    has_multiple_methods = len(set(d.get("metodo") for d in detalle)) > 1

    def _es_pago(valor: int) -> bool:
        return valor >= valor_boleta

    def _grupo_label(cant: int, es_pago_flag: bool) -> str:
        if cant > 1:
            return "Pagos" if es_pago_flag else "Abonos"
        return "Pago" if es_pago_flag else "Abono"

    def _center_text(text: str) -> bytes:
        encoded = text.encode("utf-8", errors="replace")
        pad = max(0, LINE_WIDTH - len(encoded))
        left_pad = pad // 2
        return ALIGN_LEFT + (b" " * left_pad) + encoded + FEED_LINE

    def _add_grouped_section(items: list, label: str) -> None:
        nonlocal buf
        if not items:
            return
        buf += BOLD_ON + _left(label) + BOLD_OFF
        buf += feed_lines(1)
        groups: dict[str, list] = {}
        for d in items:
            key = d.get("grupo_pago", str(d.get("valor", 0)))
            groups.setdefault(key, []).append(d)
        n_groups = len(groups)
        for grp_items in groups.values():
            valor = int(grp_items[0].get("valor", 0) or 0)
            cant = len(grp_items)
            txt_label = _grupo_label(cant, _es_pago(valor))
            boletas_ids = [d["boleta"] for d in grp_items]
            subtotal = sum(int(d.get("valor", 0) or 0) for d in grp_items)
            buf += _left(f"{txt_label} de ${valor:,}")
            buf += _left("Boletas:")
            buf += feed_lines(1)
            for bl in _wrap_boletas(boletas_ids):
                buf += _left(bl)
                buf += FEED_LINE
            buf += feed_lines(1)
            if n_groups > 1:
                buf += _center_text(f"Subtotal: ${subtotal:,}")
                buf += feed_lines(1)
            buf += _separator()
        total = sum(int(d.get("valor", 0) or 0) for d in items)
        if has_multiple_methods:
            buf += _center_text(f"TOTAL {label}: ${total:,}")
        buf += _separator()
        buf += feed_lines(1)

    # ── Efectivo ──
    efectivo = [d for d in detalle if d.get("metodo") != "transferencia" and d.get("metodo") != "pago_a_delio"]
    _add_grouped_section(efectivo, "EFECTIVO")

    # ── Transferencias ──
    transf = [d for d in detalle if d.get("metodo") == "transferencia"]
    if transf:
        buf += BOLD_ON + _left("TRANSFERENCIAS") + BOLD_OFF
        buf += feed_lines(1)
        tx_groups: dict[str, list] = {}
        for d in transf:
            key = d.get("grupo_transferencia", f"{d.get('banco', '')}|{d.get('referencia', '')}")
            tx_groups.setdefault(key, []).append(d)
        for tx_items in tx_groups.values():
            banco = tx_items[0].get("banco", "")
            ref = tx_items[0].get("referencia", "")
            if banco:
                buf += _left(f"Banco: {banco}")
            if ref:
                buf += _left(f"Ref: {ref}")
            val_groups: dict[str, list] = {}
            for d in tx_items:
                vkey = d.get("grupo_pago", str(d.get("valor", 0)))
                val_groups.setdefault(vkey, []).append(d)
            n_tx_groups = len(val_groups)
            for v_items in val_groups.values():
                valor = int(v_items[0].get("valor", 0) or 0)
                cant = len(v_items)
                txt_label = _grupo_label(cant, _es_pago(valor))
                boletas_ids = [d["boleta"] for d in v_items]
                subtotal = sum(int(d.get("valor", 0) or 0) for d in v_items)
                buf += _left(f"{txt_label} de ${valor:,}")
                buf += _left("Boletas:")
                buf += feed_lines(1)
                for bl in _wrap_boletas(boletas_ids):
                    buf += _left(bl)
                    buf += FEED_LINE
                buf += feed_lines(1)
                if n_tx_groups > 1:
                    buf += _center_text(f"Subtotal: ${subtotal:,}")
                    buf += feed_lines(1)
                buf += _separator()
        total_t = sum(int(d.get("valor", 0) or 0) for d in transf)
        if has_multiple_methods:
            buf += _center_text(f"TOTAL TRANSFERENCIAS: ${total_t:,}")
        buf += _separator()
        buf += feed_lines(1)

    # ── Pago a Delio ──
    delio = [d for d in detalle if d.get("metodo") == "pago_a_delio"]
    _add_grouped_section(delio, "PAGO A DELIO")

    if has_multiple_methods:
        buf += _double_separator()
    buf += BOLD_ON + _center("TOTAL") + BOLD_OFF
    buf += BOLD_ON + _center(f"${factura['valor_total']:,}") + BOLD_OFF
    if has_multiple_methods:
        buf += _double_separator()
    buf += feed_lines(3)
    buf += _center("--- EMITIDO POR ---")
    buf += _center(factura.get("usuario_nombre") or "No registrado")
    buf += feed_lines(5)
    buf += CUT
    return bytes(buf)


def build_egreso_receipt(factura: dict, config: dict) -> bytes:
    """Build ESC/POS bytes for an egreso comprobante."""
    buf = bytearray()
    buf += INIT
    buf += BOLD_ON
    buf += _center(config.get("nombre_empresa") or config.get("nombre_rifa") or "BOLETERIA")
    buf += BOLD_OFF
    buf += _center("COMPROBANTE DE EGRESO")
    empresa = config.get("direccion", "")
    if config.get("ciudad"):
        empresa += (", " if empresa else "") + config["ciudad"]
    if config.get("telefono"):
        empresa += " - " + config["telefono"]
    if empresa:
        buf += _center(empresa)
    buf += FEED_LINE
    buf += _separator()

    buf += _line("DOCUMENTO", f"E-{factura['_id']:05d}")
    buf += _line("FECHA", factura.get("fecha_display", ""))
    if factura.get("es_general"):
        buf += _line("TIPO", "EGRESO GENERAL")
        buf += _separator()
        buf += BOLD_ON + _left("DESCRIPCION") + BOLD_OFF
        for linea in str(factura.get("descripcion", "")).split("\n"):
            buf += _left(f"  {linea.strip()}"[:48])
        buf += _separator()
    else:
        buf += _line("TIPO", factura.get("egreso_tipo", ""))
        buf += _separator()

        buf += BOLD_ON + _left("VENDEDOR") + BOLD_OFF
        buf += _left(factura.get("vendedor_nombre", factura.get("vendedor_id", "")))
        if factura.get("vendedor_telefono"):
            buf += _left(f"TEL {factura['vendedor_telefono']}")
        buf += _separator()

        buf += BOLD_ON + _left("DETALLE") + BOLD_OFF
        for d in factura.get("detalle", []):
            buf += _line(f"  #{d['boleta']:04d}", f"${d['valor']:,}")
            metodo = d.get("metodo", "").capitalize()
            if d.get("metodo") == "transferencia":
                parts = [metodo]
                if d.get("banco"):
                    parts.append(d["banco"])
                if d.get("referencia"):
                    parts.append(d["referencia"])
                buf += _left(f"    {' / '.join(parts)}")
            else:
                buf += _left(f"    {metodo}")
        buf += _separator()

    buf += BOLD_ON + _line("TOTAL EGRESO", f"${factura['valor_total']:,}") + BOLD_OFF

    if factura.get("observaciones"):
        buf += FEED_LINE
        buf += _left("Observaciones:")
        for line in factura["observaciones"].split("\n"):
            buf += _left(f"  {line}")

    buf += feed_lines(2)
    buf += _center("--- EMITIDO POR ---")
    buf += _center(factura.get("usuario_nombre") or "No registrado")
    buf += feed_lines(2)
    buf += _separator()
    buf += _center("Documento interno, no tiene validez fiscal.")
    buf += feed_lines(5)
    buf += CUT
    return bytes(buf)


def build_traslado_receipt(traslado: dict, config: dict, origen: dict, destino: dict) -> bytes:
    """Build ESC/POS bytes for a traslado comprobante."""
    buf = bytearray()
    buf += INIT
    buf += BOLD_ON
    buf += _center(config.get("nombre_empresa") or config.get("nombre_rifa") or "BOLETERIA")
    buf += BOLD_OFF
    buf += _center("COMPROBANTE DE TRASLADO DE SALDO")
    empresa = config.get("direccion", "")
    if config.get("ciudad"):
        empresa += (", " if empresa else "") + config["ciudad"]
    if config.get("telefono"):
        empresa += " - " + config["telefono"]
    if empresa:
        buf += _center(empresa)
    buf += FEED_LINE
    buf += _separator()

    buf += _line("DOCUMENTO", f"T-{traslado['_id']:05d}")
    buf += _line("FECHA", str(traslado.get("fecha", "")))
    buf += _left("RIFA")
    buf += BOLD_ON + _left(config.get("nombre_rifa", "Rifa")) + BOLD_OFF
    buf += _separator()

    buf += BOLD_ON + _left("VENDEDOR") + BOLD_OFF
    buf += _left(traslado.get("vendedor_nombre", traslado.get("vendedor_id", "")))
    buf += _separator()

    buf += BOLD_ON + _left("MOVIMIENTO") + BOLD_OFF
    buf += _line("  DE (origen)", f"#{traslado['boleta_origen']:04d}")
    buf += _line("  A (destino)", f"#{traslado['boleta_destino']:04d}")
    buf += _separator()
    buf += BOLD_ON + _line("  VALOR TRASLADADO", f"${traslado['valor']:,}") + BOLD_OFF
    buf += FEED_LINE
    buf += _line("  Saldo origen", f"${origen.get('total_abonado', 0):,}")
    buf += _line("  Saldo destino", f"${destino.get('total_abonado', 0):,}")

    if traslado.get("observaciones"):
        buf += FEED_LINE
        buf += _left("Observaciones:")
        for line in traslado["observaciones"].split("\n"):
            buf += _left(f"  {line}")

    buf += feed_lines(2)
    buf += _center("--- EMITIDO POR ---")
    buf += _center(traslado.get("usuario_nombre") or "No registrado")
    buf += feed_lines(2)
    buf += _separator()
    buf += _center("Documento interno, no tiene validez fiscal.")
    buf += feed_lines(5)
    buf += CUT
    return bytes(buf)


def imprimir(data: bytes) -> tuple[bool, str]:
    """Send ESC/POS bytes to the configured printer. Returns (ok, message)."""
    if not PRINTER_HOST:
        return False, "Impresora no configurada. Define PRINTER_HOST en .env."
    try:
        _send(data)
        return True, "Enviado a la impresora."
    except OSError as exc:
        current_app.logger.error("Error de impresion: %s", exc)
        return False, f"No se pudo conectar con la impresora: {exc}"

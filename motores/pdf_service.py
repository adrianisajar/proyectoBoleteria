"""Server-side PDF generation using pdfkit (wkhtmltopdf)."""

import os

import pdfkit
from flask import render_template

from database import boletas, vendedores
from motores.constants import METODO_TRANSFERENCIA, VENDEDOR_LOCAL


def _get_wkhtmltopdf_path() -> str | None:
    """Return path to wkhtmltopdf executable, or None to use system PATH."""
    # Common Windows installation paths
    candidates = [
        r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
        r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
        "/usr/local/bin/wkhtmltopdf",
        "/usr/bin/wkhtmltopdf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None  # rely on system PATH


def _render_html_to_pdf(html_string: str) -> bytes:
    """Render HTML string to PDF using pdfkit/wkhtmltopdf."""
    options = {
        "encoding": "UTF-8",
        "quiet": "",
        "enable-local-file-access": "",
        "page-size": "A6",  # will be overridden by @page CSS if present
        "margin-top": "3mm",
        "margin-right": "4mm",
        "margin-bottom": "1mm",
        "margin-left": "4mm",
        "disable-smart-shrinking": "",
    }
    config = pdfkit.configuration(wkhtmltopdf=_get_wkhtmltopdf_path())
    pdf_bytes = pdfkit.from_string(html_string, False, options=options, configuration=config)
    return pdf_bytes


def generar_pdf_factura(factura: dict, config: dict) -> bytes:
    """Generate PDF for a factura (cliente or vendedor)."""
    template_map = {"cliente": "factura_cliente.html", "vendedor": "factura_vendedor.html"}
    template = template_map.get(factura.get("tipo", ""), "factura_cliente.html")

    # Build context same as ver_factura
    ctx = {"factura": factura, "config": config}

    # Prepare fecha_display
    fecha_f = factura["fecha"]
    if hasattr(fecha_f, "strftime"):
        if fecha_f.hour == 0 and fecha_f.minute == 0 and fecha_f.second == 0:
            factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y")
        else:
            factura["fecha_display"] = fecha_f.strftime("%d/%m/%Y %I:%M %p")

    if factura.get("tipo") == "cliente":
        boletas_ids = factura.get("boletas", [])
        docs = list(boletas.find({"_id": {"$in": boletas_ids}}))
        config_obj = config
        valor_boleta = int(config_obj.get("valor_boleta", 10000))
        vendedores_vistos = {doc.get("vendedor_id") for doc in docs if doc.get("vendedor_id") and doc.get("vendedor_id") != VENDEDOR_LOCAL}
        vid_cache = {}
        if vendedores_vistos:
            for v in vendedores.find({"_id": {"$in": list(vendedores_vistos)}}, {"nombre": 1}):
                vid_cache[v["_id"]] = v.get("nombre", v["_id"])
        boletas_info = {}
        for doc in docs:
            bid = doc["_id"]
            historial_completo = doc.get("historial_pagos") or []
            fecha_factura_str = factura["fecha"].strftime("%Y-%m-%d") if hasattr(factura["fecha"], "strftime") else str(factura["fecha"])[:10]
            historial_hasta_factura = [
                p
                for p in historial_completo
                if (p.get("factura_id") is None or p.get("factura_id", 0) <= factura["_id"]) and str(p.get("fecha", ""))[:10] <= fecha_factura_str
            ]
            historial_esta_factura = [p for p in historial_hasta_factura if p.get("factura_id") == factura["_id"]]
            total_hasta_factura = sum(int(p.get("valor", 0) or 0) for p in historial_hasta_factura)
            saldo_hasta_factura = max(valor_boleta - total_hasta_factura, 0)
            if total_hasta_factura >= valor_boleta:
                estado_historico = "pagada"
            elif total_hasta_factura > 0:
                estado_historico = "abonando"
            elif doc.get("vendedor_id") == VENDEDOR_LOCAL:
                estado_historico = "separada"
            elif doc.get("vendedor_id"):
                estado_historico = "asignada"
            else:
                estado_historico = "disponible"

            boletas_info[bid] = {
                "total_abonado": total_hasta_factura,
                "saldo_pendiente": saldo_hasta_factura,
                "estado": estado_historico,
                "valor_boleta": valor_boleta,
                "vendedor_id": doc.get("vendedor_id", "LOCAL"),
                "vendedor_nombre": vid_cache.get(doc.get("vendedor_id", "LOCAL"), "LOCAL"),
                "historial_pagos": historial_hasta_factura,
                "pagos_factura": historial_esta_factura,
            }
        ctx["boletas_info"] = boletas_info

        for d in factura.get("detalle") or []:
            d["grupo_pago"] = str(d.get("valor", 0))
            if d.get("metodo") == "transferencia":
                d["grupo_transferencia"] = f"{d.get('banco', '')}|{d.get('referencia', '')}"

    elif factura.get("tipo") == "vendedor":
        total_efectivo = 0
        total_transferencia = 0
        for d in factura.get("detalle") or []:
            valor = int(d.get("valor", 0) or 0)
            d["grupo_pago"] = str(valor)
            if d.get("metodo") == METODO_TRANSFERENCIA:
                total_transferencia += valor
                d["grupo_transferencia"] = f"{d.get('banco', '')}|{d.get('referencia', '')}"
            else:
                total_efectivo += valor
        ctx["total_efectivo"] = total_efectivo
        ctx["total_transferencia"] = total_transferencia

    # Render HTML
    html_string = render_template(template, **ctx)
    return _render_html_to_pdf(html_string)


def generar_pdf_liquidacion(liqui: dict, config: dict) -> bytes:
    """Generate PDF for a liquidación comprobante."""
    html_string = render_template("comprobante_liquidacion.html", liqui=liqui, config=config)
    return _render_html_to_pdf(html_string)

"""Server-side PDF generation using pdfkit (wkhtmltopdf) + PyMuPDF crop."""

import os

import fitz  # PyMuPDF
import pdfkit
from flask import render_template

from database import boletas, vendedores
from motores.constants import METODO_TRANSFERENCIA, VENDEDOR_LOCAL


def _get_wkhtmltopdf_path() -> str | None:
    """Return path to wkhtmltopdf executable, or None to use system PATH."""
    candidates = [
        r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
        r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
        "/usr/local/bin/wkhtmltopdf",
        "/usr/bin/wkhtmltopdf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _render_html_to_pdf_a4(html_string: str) -> bytes:
    """Render HTML to A4 PDF using wkhtmltopdf with print media type."""
    options = {
        "encoding": "UTF-8",
        "quiet": "",
        "enable-local-file-access": "",
        "print-media-type": "",
        "disable-smart-shrinking": "",
        "no-pdf-compression": "",
    }
    config = pdfkit.configuration(wkhtmltopdf=_get_wkhtmltopdf_path())
    return pdfkit.from_string(html_string, False, options=options, configuration=config)


def _crop_pdf_to_thermal(pdf_bytes: bytes, page_width_mm: float = 80.0, margin_mm: float = 3.0) -> bytes:
    """
    Crop PDF to thermal paper width (80mm) and trim height to content bounds.
    """
    doc = fitz.open("pdf", pdf_bytes)
    page_width_pt = page_width_mm * 72 / 25.4  # 80mm = ~226.77 points
    margin_pt = margin_mm * 72 / 25.4

    # Stitch all pages vertically into one tall page
    a4_width = doc[0].rect.width
    a4_height = doc[0].rect.height
    gap_pt = margin_mm * 72 / 25.4
    total_height = len(doc) * a4_height + (len(doc) - 1) * gap_pt + 2 * margin_pt

    new_doc = fitz.open()
    new_page = new_doc.new_page(width=page_width_pt, height=total_height)

    # Draw each original page onto the new tall page, centered horizontally
    x_offset = (page_width_pt - a4_width) / 2
    if x_offset < 0:
        x_offset = 0

    for i in range(len(doc)):
        new_page.show_pdf_page(
            fitz.Rect(x_offset, i * (a4_height + gap_pt) + margin_pt, x_offset + a4_width, (i + 1) * a4_height + margin_pt + i * gap_pt), doc, i
        )

    # Find content bounds on the stitched page
    new_page = new_doc[0]

    all_rects = []
    for text in [
        "FACTURA",
        "BOLETER",
        "TOTAL",
        "RECIBO",
        "FECHA",
        "PAGO",
        "VENDEDOR",
        "BOLETA",
        "EFECTIVO",
        "TRANSFER",
        "OBSERV",
        "FIRMA",
        "DOCUMENTO",
        "INTERNO",
        "VALIDEZ",
        "FISCAL",
    ]:
        found = new_page.search_for(text)
        if found:
            for r in found:
                all_rects.append(r)
    for drawing in new_page.get_drawings():
        all_rects.append(drawing["rect"])
    for img in new_page.get_images(full=True):
        try:
            bbox = new_page.get_image_bbox(img)
            if bbox:
                all_rects.append(bbox)
        except Exception:
            pass

    if not all_rects:
        return pdf_bytes

    content_rect = fitz.Rect()
    for r in all_rects:
        content_rect |= r

    margin_pt = 3 * 72 / 25.4
    content_rect.x0 = max(0, content_rect.x0 - margin_pt)
    content_rect.y0 = max(0, content_rect.y0 - margin_pt)
    content_rect.x1 = min(new_page.rect.width, content_rect.x1 + margin_pt)
    content_rect.y1 = min(new_page.rect.height, content_rect.y1 + margin_pt)

    # Crop to 80mm width, centered - ensure within MediaBox
    page_width_pt = 80 * 72 / 25.4
    page_center_x = new_page.rect.width / 2
    new_x0 = page_center_x - (80 * 72 / 25.4) / 2
    new_x1 = new_x0 + (80 * 72 / 25.4)

    # Clamp to MediaBox
    if new_x0 < 0:
        new_x0 = 0
        new_x1 = 80 * 72 / 25.4
    if new_x1 > new_page.rect.width:
        new_x1 = new_page.rect.width
        new_x0 = new_page.rect.width - (80 * 72 / 25.4)

    # Ensure crop rect is within MediaBox
    crop_y0 = max(0, content_rect.y0)
    crop_y1 = min(new_page.rect.height, content_rect.y1)
    crop_x0 = max(0, min(new_x0, new_page.rect.width - (80 * 72 / 25.4)))
    crop_x1 = min(new_page.rect.width, crop_x0 + (80 * 72 / 25.4))

    crop_rect = fitz.Rect(crop_x0, crop_y0, crop_x1, crop_y1)

    # Verify crop rect is within MediaBox
    if (
        crop_rect.x0 >= 0
        and crop_rect.y0 >= 0
        and crop_rect.x1 <= new_page.rect.width
        and crop_rect.y1 <= new_page.rect.height
        and crop_rect.x1 > crop_rect.x0
        and crop_rect.y1 > crop_rect.y0
    ):
        new_page.set_cropbox(crop_rect)
        new_page.set_mediabox(crop_rect)
    else:
        # Fallback: use full page width, crop to content height only
        fallback_rect = fitz.Rect(0, max(0, content_rect.y0), new_page.rect.width, min(new_page.rect.height, content_rect.y1 + margin_pt))
        new_page.set_cropbox(fallback_rect)
        new_page.set_mediabox(fallback_rect)

    output = new_doc.tobytes()
    new_doc.close()
    return output


def generar_pdf_factura(factura: dict, config: dict) -> bytes:
    """Generate PDF for a factura (cliente or vendedor)."""
    template_map = {"cliente": "factura_cliente.html", "vendedor": "factura_vendedor.html"}
    template = template_map.get(factura.get("tipo", ""), "factura_cliente.html")

    ctx = {"factura": factura, "config": config}

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

    html_string = render_template(template, **ctx)
    pdf_a4 = _render_html_to_pdf_a4(html_string)
    return _crop_pdf_to_thermal(pdf_a4)


def generar_pdf_liquidacion(liqui: dict, config: dict) -> bytes:
    """Generate PDF for a liquidación comprobante."""
    html_string = render_template("comprobante_liquidacion.html", liqui=liqui, config=config)
    pdf_a4 = _render_html_to_pdf_a4(html_string)
    return _crop_pdf_to_thermal(pdf_a4)

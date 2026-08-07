import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from typing import Any
from unicodedata import normalize as unicode_normalize

from pymongo import UpdateOne

from database import boletas, vendedores
from motores.cache import invalidate_dashboard_cache
from motores.config_service import get_config, require_collections
from motores.constants import (
    METODO_TRANSFERENCIA,
    MODELO_RIFA_HEADERS,
    MOV_PAGO,
    MOVIMIENTOS_FIELD,
    VENDEDOR_LOCAL,
    XLSX_NS,
    XLSX_REL_NS,
)
from motores.excel_import import clean_excel_text, col_to_index, parse_excel_boleta
from motores.ticket_service import estado_pipeline_expr, sync_ticket_statuses
from motores.vendor_service import vendedor_label


def compact_model_payments(payments: list, slots: int) -> list:
    """Collapse extra payments into a single 'VARIOS' row when exceeding the slot count."""
    payments = [payment for payment in payments if int(payment.get("valor", 0) or 0) > 0]
    if len(payments) <= slots:
        return payments

    head = payments[: slots - 1]
    tail = payments[slots - 1 :]
    head.append(
        {
            "fecha": tail[-1].get("fecha", ""),
            "facturero": "VARIOS",
            "valor": sum(int(payment.get("valor", 0) or 0) for payment in tail),
            "metodo": tail[-1].get("metodo", ""),
            "referencia": "VARIOS",
        }
    )
    return head


def append_model_payment_slots(row: list, payments: list, slots: int) -> None:
    """Extend a report row with fecha/facturero/valor triplets for each payment slot."""
    compacted = compact_model_payments(payments, slots)
    for index in range(slots):
        if index < len(compacted):
            payment = compacted[index]
            row.extend([payment.get("fecha", ""), payment.get("facturero", ""), int(payment.get("valor", 0) or 0)])
        else:
            row.extend(["", "", ""])


def modelo_rifa_report_rows() -> tuple[list, list]:
    """Build (headers, rows) for the modelo-rifa Excel export from all tickets."""
    nombres_vendedores = {doc["_id"]: doc.get("nombre", "") for doc in vendedores.find({}, {"nombre": 1})}
    rows = []
    for doc in boletas.find({}).sort("_id", 1):
        cliente = doc.get("cliente") or {}
        historial = [mov for mov in (doc.get(MOVIMIENTOS_FIELD) or []) if (mov.get("tipo") or MOV_PAGO) == MOV_PAGO]
        efectivo = [payment for payment in historial if payment.get("metodo") != METODO_TRANSFERENCIA]
        transferencias = [payment for payment in historial if payment.get("metodo") == METODO_TRANSFERENCIA]
        total_efectivo = sum(int(payment.get("valor", 0) or 0) for payment in efectivo)
        total_transferencias = sum(int(payment.get("valor", 0) or 0) for payment in transferencias)
        total_abonado = int(doc.get("total_abonado", 0) or 0)

        row = [
            f"{doc['_id']:04d}",
            total_abonado,
            doc.get("fecha_adquisicion", ""),
            vendedor_label(doc.get("vendedor_id", VENDEDOR_LOCAL), nombres_vendedores),
            cliente.get("nombre", ""),
            cliente.get("direccion", ""),
            cliente.get("telefono", ""),
        ]
        append_model_payment_slots(row, efectivo, 7)
        row.extend([total_efectivo, ""])
        append_model_payment_slots(row, transferencias, 5)
        row.extend([total_transferencias, total_abonado])
        rows.append(row)
    return MODELO_RIFA_HEADERS, rows


def vendor_from_excel(value: Any) -> tuple[str, str]:
    """Derive (vendedor_id, display_nombre) from an Excel vendor cell value."""
    raw = re.sub(r"\s+", " ", clean_excel_text(value)).strip()
    if not raw:
        return VENDEDOR_LOCAL, VENDEDOR_LOCAL
    nombre = re.sub(r"^VEND\.?\s*", "", raw, flags=re.IGNORECASE).strip() or raw

    ascii_name = unicode_normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii")
    vendedor_id = re.sub(r"[^A-Z0-9]+", "_", ascii_name.upper()).strip("_")
    vendedor_id = vendedor_id[:32].strip("_") or VENDEDOR_LOCAL
    return vendedor_id, nombre.upper()


def is_assignable_vendor_cell(value: Any) -> bool:
    """Return True if a vendor cell is an assignable vendor (not LOCAL/CAMION/PAQUETE)."""
    raw = re.sub(r"\s+", " ", clean_excel_text(value)).strip()
    if not raw:
        return False
    nombre = re.sub(r"^VEND\.?\s*", "", raw, flags=re.IGNORECASE).strip()
    if not nombre:
        return False
    upper = nombre.upper()
    if upper == VENDEDOR_LOCAL:
        return False
    return not upper.startswith(("CAMION", "CAMI\u00d3N", "PAQUETE"))


def read_xlsx_first_sheet_rows(file_obj: Any) -> list[list]:
    """Parse the first worksheet of an .xlsx into rows of cell values (no openpyxl)."""
    data = file_obj.read()
    with zipfile.ZipFile(io.BytesIO(data)) as workbook_zip:
        shared_strings = []
        if "xl/sharedStrings.xml" in workbook_zip.namelist():
            shared_root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("main:si", XLSX_NS):
                shared_strings.append("".join(node.text or "" for node in item.iterfind(".//main:t", XLSX_NS)))

        workbook_root = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
        rels_root = ET.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
        rels = {rel.attrib["Id"]: rel.attrib["Target"].replace("/xl/", "") for rel in rels_root.findall("rel:Relationship", XLSX_REL_NS)}
        first_sheet = workbook_root.find("main:sheets/main:sheet", XLSX_NS)
        if first_sheet is None:
            raise ValueError("El archivo no contiene hojas.")

        rel_id = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        sheet_path = "xl/" + rels[rel_id].lstrip("/")
        sheet_root = ET.fromstring(workbook_zip.read(sheet_path))

        rows = []
        for row in sheet_root.findall("main:sheetData/main:row", XLSX_NS):
            values = []
            for cell in row.findall("main:c", XLSX_NS):
                ref = cell.attrib.get("r", "")
                match = re.match(r"([A-Z]+)", ref)
                if not match:
                    continue
                index = col_to_index(match.group(1))
                while len(values) <= index:
                    values.append("")

                if cell.attrib.get("t") == "inlineStr":
                    values[index] = "".join(node.text or "" for node in cell.iterfind(".//main:t", XLSX_NS))
                    continue

                value_node = cell.find("main:v", XLSX_NS)
                value = "" if value_node is None or value_node.text is None else value_node.text
                if cell.attrib.get("t") == "s" and value != "":
                    value = shared_strings[int(value)]
                values[index] = value
            rows.append(values)
        return rows


def row_value(row: list, index: int) -> str:
    """Return the cell at index or '' when out of bounds."""
    return row[index] if index < len(row) else ""


def parse_asignaciones_vendedores_xlsx(file_obj: Any) -> tuple[dict, dict, dict]:
    """Parse vendor assignments from an xlsx and return (assignments, names, summary)."""
    rows = read_xlsx_first_sheet_rows(file_obj)
    if not rows:
        raise ValueError("El archivo está vacío.")

    headers = [clean_excel_text(value).upper() for value in rows[0]]
    normalized_headers = {header.strip() for header in headers}
    missing = [header for header in ("NUMERO DE BOLETA", "VENDEDOR (A)") if header not in normalized_headers]
    if missing:
        raise ValueError("El archivo no parece ser el modelo esperado. Faltan columnas: " + ", ".join(missing))

    vendor_assignments = defaultdict(list)
    vendor_names = {}
    invalid_rows = []
    ignored_local = 0
    ignored_camion = 0
    ignored_paquete = 0
    empty_vendor = 0

    for excel_row_number, row in enumerate(rows[1:], start=2):
        numero = parse_excel_boleta(row_value(row, 0))
        if numero is None:
            if any(clean_excel_text(value) for value in row):
                invalid_rows.append(excel_row_number)
            continue

        vendedor_cell = row_value(row, 3)
        if not clean_excel_text(vendedor_cell):
            empty_vendor += 1
            continue
        if not is_assignable_vendor_cell(vendedor_cell):
            raw_v = re.sub(r"\s+", " ", clean_excel_text(vendedor_cell)).strip()
            nombre_v = re.sub(r"^VEND\.?\s*", "", raw_v, flags=re.IGNORECASE).strip().upper()
            if nombre_v.startswith("PAQUETE"):
                ignored_paquete += 1
            elif nombre_v.startswith(("CAMION", "CAMI\u00d3N")):
                ignored_camion += 1
            else:
                ignored_local += 1
            continue

        vendedor_id, vendedor_nombre = vendor_from_excel(vendedor_cell)
        vendor_assignments[vendedor_id].append(numero)
        vendor_names[vendedor_id] = vendedor_nombre

    boleta_to_vendors = defaultdict(set)
    for v_id, ids_list in vendor_assignments.items():
        for num in ids_list:
            boleta_to_vendors[num].add(v_id)
    duplicates = {num: sorted(v_ids) for num, v_ids in boleta_to_vendors.items() if len(v_ids) > 1}
    if duplicates:
        dup_msgs = [f"#{num:04d}: {', '.join(v_ids)}" for num, v_ids in sorted(duplicates.items())[:5]]
        raise ValueError(f"Boletas asignadas a m\u00faltiples vendedores: {'; '.join(dup_msgs)}")

    return (
        vendor_assignments,
        vendor_names,
        {
            "boletas_asignadas": sum(len(set(ids)) for ids in vendor_assignments.values()),
            "vendedores": len(vendor_assignments),
            "local_ignoradas": ignored_local,
            "camion_ignoradas": ignored_camion,
            "paquete_ignoradas": ignored_paquete,
            "sin_vendedor": empty_vendor,
            "invalid_rows": invalid_rows[:20],
        },
    )


def importar_modelo_rifa(file_obj: Any) -> dict:
    """Apply vendor assignments from an xlsx to tickets/vendors and return a summary."""
    require_collections()
    config = get_config()
    valor_boleta = int(config.get("valor_boleta", 10000) or 10000)
    vendor_assignments, vendor_names, summary = parse_asignaciones_vendedores_xlsx(file_obj)

    assigned_ids = sorted({number for ids in vendor_assignments.values() for number in ids})
    for vendedor_id, ids in vendor_assignments.items():
        unique_ids = sorted(set(ids))
        if unique_ids:
            boletas.update_many(
                {"_id": {"$in": unique_ids}},
                {"$set": {"vendedor_id": vendedor_id}},
            )
            boletas.update_many(
                {"_id": {"$in": unique_ids}},
                [{"$set": {"estado": estado_pipeline_expr(valor_boleta)}}],
            )

    vendor_ops = []
    for vendedor_id, assigned in vendor_assignments.items():
        vendor_ops.append(
            UpdateOne(
                {"_id": vendedor_id},
                {
                    "$set": {
                        "nombre": vendor_names.get(vendedor_id, vendedor_id),
                    },
                    "$addToSet": {"boletas_asignadas": {"$each": sorted(set(assigned))}},
                    "$setOnInsert": {"telefono": ""},
                },
                upsert=True,
            )
        )
    if vendor_ops:
        vendedores.bulk_write(vendor_ops, ordered=False)

    summary["boletas_actualizadas"] = len(assigned_ids)
    summary["boletas_locales_omitidas"] = 0

    sync_ticket_statuses(valor_boleta)
    invalidate_dashboard_cache()

    return summary

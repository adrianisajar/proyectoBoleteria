import re
from datetime import datetime, timedelta
from typing import Any

from motores.constants import BOLETA_MAX, BOLETA_MIN


def col_to_index(column: str) -> int:
    """Convert Excel column letter ('A', 'BC') to 0-based index."""
    index = 0
    for char in column:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def clean_excel_text(value: Any) -> str:
    """Normalize a cell value to stripped text; drop trailing '.0' for whole floats."""
    if value is None:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return text.strip()


def parse_excel_number(value: Any) -> int:
    """Parse a cell to int, tolerating currency symbols and thousand separators."""
    text = clean_excel_text(value).replace("$", "").replace(",", "")
    if text == "":
        return 0
    try:
        return int(float(text))
    except ValueError:
        digits = re.sub(r"[^\d-]", "", text)
        return int(digits) if digits not in {"", "-"} else 0


def parse_excel_boleta(value: Any) -> int | None:
    """Parse a ticket number from a cell; return None if invalid or out of 0000-9999."""
    text = clean_excel_text(value).strip().lstrip("'\"")
    if text == "":
        return None
    try:
        number = int(float(text))
    except ValueError:
        digits = re.sub(r"\D", "", text)
        number = int(digits) if digits else None
    if number is None or number < BOLETA_MIN or number > BOLETA_MAX:
        return None
    return number


def parse_excel_date(value: Any) -> str:
    """Parse a cell date (Excel serial or common text formats) to ISO yyyy-mm-dd."""
    text = clean_excel_text(value)
    if not text:
        return ""
    try:
        serial = float(text)
        if 0 < serial < 100000:
            return (datetime(1899, 12, 30) + timedelta(days=int(serial))).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text

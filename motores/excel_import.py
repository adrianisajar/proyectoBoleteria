import re
from datetime import date, datetime, timedelta

from motores.constants import BOLETA_MIN, BOLETA_MAX


def col_to_index(column):
    index = 0
    for char in column:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def clean_excel_text(value):
    if value is None:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return text.strip()


def parse_excel_number(value):
    text = clean_excel_text(value).replace("$", "").replace(",", "")
    if text == "":
        return 0
    try:
        return int(float(text))
    except ValueError:
        digits = re.sub(r"[^\d-]", "", text)
        return int(digits) if digits not in {"", "-"} else 0


def parse_excel_boleta(value):
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


def parse_excel_date(value):
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

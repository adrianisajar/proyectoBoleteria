import re
from collections import Counter

from motores.constants import BOLETA_MAX, BOLETA_MIN


def parse_int_filter(value: str, field_name: str, errors: list, min_value: int | None = None, max_value: int | None = None) -> int | None:
    """Parse an integer filter value, appending validation errors as needed."""
    if value == "":
        return None

    if not value.isdigit():
        errors.append(f"{field_name} debe ser num\u00e9rico.")
        return None

    number = int(value)
    if min_value is not None and number < min_value:
        errors.append(f"{field_name} debe ser mayor o igual a {min_value}.")
    if max_value is not None and number > max_value:
        errors.append(f"{field_name} debe ser menor o igual a {max_value}.")
    return number


def ticket_number_query(value: str, errors: list) -> tuple[int | dict, bool]:
    """Build a ticket query from a 1-4 digit input: exact id or $in prefix matches."""
    raw_value = (value or "").strip()
    if raw_value == "":
        return None, False

    if not raw_value.isdigit():
        errors.append("El n\u00famero de boleta debe contener solo d\u00edgitos.")
        return None, False

    if len(raw_value) > 4:
        errors.append("El n\u00famero de boleta debe tener m\u00e1ximo 4 d\u00edgitos.")
        return None, False

    if len(raw_value) == 4:
        number = int(raw_value)
        if number < BOLETA_MIN or number > BOLETA_MAX:
            errors.append("El n\u00famero debe estar entre 0000 y 9999.")
            return None, False
        return number, True

    MAX_MATCHES = 200
    matches = [number for number in range(BOLETA_MIN, BOLETA_MAX + 1) if raw_value in f"{number:04d}"]
    if not matches:
        errors.append("No hay boletas que contengan esos d\u00edgitos.")
        return None, False
    if len(matches) > MAX_MATCHES:
        errors.append(f"Demasiados resultados ({len(matches)}). Se usan los primeros {MAX_MATCHES}.")
        matches = matches[:MAX_MATCHES]

    return {"$in": matches}, False


def parse_money(value: str | int | None) -> int:
    """Extract integer amount from text, ignoring currency symbols and separators."""
    cleaned = re.sub(r"[^\d]", "", value or "")
    return int(cleaned) if cleaned else 0


def parse_boletas_detailed(raw_numbers: str) -> tuple[list[int], list[str], list[str], list[int]]:
    """Parse a raw ticket list into (unique_ids, invalid, out_of_range, duplicates)."""
    parts = [part for part in re.split(r"[\s,;]+", (raw_numbers or "").strip()) if part]
    invalid = [part for part in parts if not part.isdigit()]
    numbers = []
    out_of_range = []

    for part in parts:
        if not part.isdigit():
            continue
        number = int(part)
        if number < BOLETA_MIN or number > BOLETA_MAX:
            out_of_range.append(part)
            continue
        numbers.append(number)

    counts = Counter(numbers)
    duplicates = sorted(number for number, count in counts.items() if count > 1)
    unique_numbers = list(dict.fromkeys(numbers))

    return unique_numbers, invalid, out_of_range, duplicates


def parse_boletas(raw_numbers: str) -> tuple[list[int], list[str], list[str]]:
    """Parse a raw ticket list into (unique_ids, invalid, out_of_range)."""
    boleta_ids, invalid, out_of_range, _duplicates = parse_boletas_detailed(raw_numbers)
    return boleta_ids, invalid, out_of_range

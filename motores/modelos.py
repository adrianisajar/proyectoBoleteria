def crear_boleta_base(numero: int, rifa_id: object | None = None) -> dict:
    """Return a fresh ticket document in 'disponible' state."""
    doc = {
        "_id": numero,
        "vendedor_id": "",
        "cliente": {"nombre": "", "telefono": "", "direccion": ""},
        "estado": "disponible",
        "total_abonado": 0,
        "historial_movimientos": [],
        "fecha_adquisicion": None,
    }
    if rifa_id is not None:
        doc["rifa_id"] = rifa_id
    return doc

def crear_boleta_base(numero: int) -> dict:
    """Return a fresh ticket document in 'disponible' state."""
    return {
        "_id": numero,
        "vendedor_id": "",
        "cliente": {"nombre": "", "telefono": "", "direccion": ""},
        "estado": "disponible",
        "total_abonado": 0,
        "historial_pagos": [],
    }

from init_db import crear_configuracion_base, crear_indices
from database import boletas


if __name__ == "__main__":
    if boletas is None:
        raise RuntimeError("No hay conexión activa a MongoDB.")

    crear_configuracion_base()
    crear_indices()
    print("Configuración e índices creados o verificados correctamente.")

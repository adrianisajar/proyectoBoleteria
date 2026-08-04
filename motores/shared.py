import os
import sys

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from motores.auth import role_required, current_user
from motores.vendor_service import (  # re-export
    normalize_vendedor_id,
    calc_comision_por_boleta,
    get_vendedor_options,
    existing_boleta_ids,
    get_vendedores_snapshot,
    safe_vendedores_snapshot,
    vendedor_label,
)
from motores.dashboard_service import (  # re-export
    get_alertas,
    get_dashboard_counts,
    first_aggregate,
    get_dashboard_stats,
)

from motores.cache import (
    CONFIG_CACHE,
    CONFIG_CACHE_SECONDS,
    RIFA_CACHE,
    RIFA_CACHE_SECONDS,
    DASHBOARD_CACHE,
    DASHBOARD_CACHE_SECONDS,
    invalidate_rifa_cache,
    invalidate_dashboard_cache,
    invalidate_config_cache,
)
from motores.ticket_service import estado_para_total, sync_ticket_statuses, estado_pipeline_expr
from motores.config_service import get_config, require_collections
from motores.payment_service import (
    buscar_transferencia_duplicada,
    build_factura_detalle,
    validar_form_abono,
    build_abono_preview,
    registrar_abono_lote,
    rollback_pagos_por_factura,
    next_factura_id,
)

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from database import boletas, configuracion, facturas, rifas, vendedores, liquidaciones
from motores.constants import (
    BOLETA_MIN,
    BOLETA_MAX,
    METODOS_PAGO,
    OPERACIONES_VENDEDOR,
    ESTADOS_BOLETA,
    CONSULTA_LIMIT_DEFAULT,
    CONSULTA_LIMIT_MAX,
    CONFIG_ID,
    COMISION_DEFAULT_TIERS,
    DEFAULT_RIFA,
    DEFAULT_CONFIG,
    VENDEDOR_LOCAL,
    VENDEDOR_SIN_ASIGNAR,
    METODO_EFECTIVO,
    METODO_TRANSFERENCIA,
    REFERENCIA_N_A,
    USUARIO_SISTEMA,
    MODELO_RIFA_HEADERS,
    XLSX_NS,
    XLSX_REL_NS,
)
from motores.validacion import parse_int_filter, ticket_number_query, parse_money, parse_boletas_detailed, parse_boletas
from motores.consulta_service import (  # re-export
    build_consulta_context,
    build_page_url,
)
from motores.excel_service import (  # re-export
    compact_model_payments,
    append_model_payment_slots,
    modelo_rifa_report_rows,
    vendor_from_excel,
    is_assignable_vendor_cell,
    read_xlsx_first_sheet_rows,
    row_value,
    parse_asignaciones_vendedores_xlsx,
    importar_modelo_rifa,
)
from motores.rifa_lifecycle import (  # re-export
    crear_indices_boletas,
    crear_nueva_rifa,
)
from motores.liquidacion_service import (  # re-export
    generar_liquidacion,
    get_liquidacion,
    get_liquidacion_detalle,
    get_liquidaciones_resumen,
    registrar_abono_liquidacion,
    next_liquidacion_id,
)
from motores.flask_integration import (  # re-export
    register_template_filters,
    register_before_request,
    register_context_processor,
)

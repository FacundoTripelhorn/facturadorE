"""Acceso a datos, un módulo por entidad.

Convenciones: ids uuid4 hex; montos como TEXT (str de Decimal, nunca float);
fechas de comprobante AAAAMMDD; timestamps ISO UTC.
"""

from .clients import (
    CLIENT_FIELDS,
    create_client,
    get_client,
    get_default_client,
    list_clients,
    update_client,
)
from .emisores import (
    EMISOR_FIELDS,
    create_emisor,
    delete_emisor,
    get_emisor,
    get_emisor_por_ambiente,
    list_emisores,
    update_emisor,
    upsert_emisor,
)
from .invoices import (
    INVOICE_FIELDS,
    UPDATABLE_INVOICE_FIELDS,
    count_invoices_by_status,
    create_invoice,
    delete_draft,
    get_invoice,
    get_invoice_items,
    list_invoices,
    max_arca_id,
    max_authorized_cbte_nro,
    try_transition_to_submitting,
    update_invoice,
)
from .params import get_params, replace_params
from .settings import get_settings, save_settings

__all__ = [
    "CLIENT_FIELDS",
    "count_invoices_by_status",
    "create_emisor",
    "EMISOR_FIELDS",
    "INVOICE_FIELDS",
    "UPDATABLE_INVOICE_FIELDS",
    "create_client",
    "create_invoice",
    "delete_draft",
    "delete_emisor",
    "get_client",
    "get_emisor",
    "get_default_client",
    "get_emisor_por_ambiente",
    "get_invoice",
    "get_invoice_items",
    "get_params",
    "get_settings",
    "list_clients",
    "list_emisores",
    "list_invoices",
    "max_arca_id",
    "max_authorized_cbte_nro",
    "replace_params",
    "save_settings",
    "try_transition_to_submitting",
    "update_client",
    "update_emisor",
    "update_invoice",
    "upsert_emisor",
]

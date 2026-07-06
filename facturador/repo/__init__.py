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

__all__ = [
    "CLIENT_FIELDS",
    "count_invoices_by_status",
    "INVOICE_FIELDS",
    "UPDATABLE_INVOICE_FIELDS",
    "create_client",
    "create_invoice",
    "delete_draft",
    "get_client",
    "get_default_client",
    "get_invoice",
    "get_invoice_items",
    "get_params",
    "list_clients",
    "list_invoices",
    "max_arca_id",
    "max_authorized_cbte_nro",
    "replace_params",
    "try_transition_to_submitting",
    "update_client",
    "update_invoice",
]

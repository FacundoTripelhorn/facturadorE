"""Generación del PDF del comprobante con QR RG 4892 (fase 5).

FAC-53: despacho versionado + cache local descartable bajo ``pdf_dir``.
"""

from .registry import known_pdf_render_versions
from .render import (
    get_or_render_invoice_pdf,
    invoice_pdf_cache_path,
    invoice_pdf_filename,
    render_invoice_html,
    render_invoice_pdf,
)

# Importar render registra el renderer v1 en el registry.
__all__ = [
    "get_or_render_invoice_pdf",
    "invoice_pdf_cache_path",
    "invoice_pdf_filename",
    "known_pdf_render_versions",
    "render_invoice_html",
    "render_invoice_pdf",
]

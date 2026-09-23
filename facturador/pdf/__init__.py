"""Generación del PDF del comprobante con QR RG 4892 (fase 5).

FAC-53: despacho versionado + cache local descartable bajo ``pdf_dir``.
FAC-88: motor fpdf2 (Python puro, sin browser); una sola página A4.
"""

from .registry import known_pdf_render_versions
from .render import (
    PdfLayoutError,
    get_or_render_invoice_pdf,
    invoice_pdf_cache_path,
    invoice_pdf_filename,
    render_invoice_pdf,
)

# Importar render registra el renderer v1 en el registry.
__all__ = [
    "PdfLayoutError",
    "get_or_render_invoice_pdf",
    "invoice_pdf_cache_path",
    "invoice_pdf_filename",
    "known_pdf_render_versions",
    "render_invoice_pdf",
]

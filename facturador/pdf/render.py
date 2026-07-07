"""Render del comprobante: fila SQLite → HTML (Jinja2) → PDF (weasyprint).

El layout replica el comprobante real de Comprobantes en Línea analizado en
design.md §0.1. Los datos del emisor (leyenda IVA, IIBB, inicio de
actividades) salen de la tabla settings de la DB; el resto es el snapshot
inmutable de la factura autorizada. Autoescape SIEMPRE activo: razón social,
domicilio y descripciones (incluidos los datos del emisor, que también son
texto libre) son hostiles (checklist §2.1.1 punto 4 aplica al HTML igual que
al XML).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ..constants import (
    MONEDA_DISPLAY,
    TIPO_EXPO_BIENES,
    TIPO_EXPO_OTROS,
    TIPO_EXPO_SERVICIOS,
    UMED_UNIDADES,
)
from ..settings import Emisor
from .qr import build_qr_payload, qr_png_data_uri, qr_url

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=True,
)
_env.globals["UMED_UNIDADES"] = UMED_UNIDADES


def _fecha_larga(aaaammdd: str) -> str:
    return f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"


_env.filters["fecha"] = _fecha_larga
# DOL → USD para el lector; el código ARCA viaja solo en el XML.
_env.filters["moneda"] = lambda code: MONEDA_DISPLAY.get(code, code)


def invoice_pdf_filename(inv: sqlite3.Row) -> str:
    return (
        f"factura-E-{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}"
        f"-{inv['environment']}.pdf"
    )


_TIPO_EXPO_DS = {
    TIPO_EXPO_BIENES: "Exportación de bienes",
    TIPO_EXPO_SERVICIOS: "Exportación de servicios",
    TIPO_EXPO_OTROS: "Otros",
}


def render_invoice_html(
    inv: sqlite3.Row,
    items: list[sqlite3.Row],
    emisor: Emisor,
    cuit_emisor: int,
    pais_ds: str = "",
) -> str:
    payload = build_qr_payload(inv, cuit_emisor)
    template = _env.get_template("invoice.html")
    return template.render(
        inv=inv,
        items=items,
        emisor=emisor,
        cuit_emisor=cuit_emisor,
        # El ambiente del comprobante es el snapshot de la fila, no la
        # config actual: un PDF de homologación se marca siempre como tal.
        es_homo=inv["environment"] == "homo",
        pais_ds=pais_ds or str(inv["dst_cmp"]),
        tipo_expo_ds=_TIPO_EXPO_DS.get(inv["tipo_expo"], str(inv["tipo_expo"])),
        qr_data_uri=qr_png_data_uri(qr_url(payload)),
        nro_completo=f"{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}",
    )


def render_invoice_pdf(
    inv: sqlite3.Row,
    items: list[sqlite3.Row],
    emisor: Emisor,
    cuit_emisor: int,
    pais_ds: str = "",
) -> bytes:
    # Import perezoso: weasyprint necesita Pango/GTK del sistema, que solo
    # está garantizado dentro de la imagen Docker (design.md §2.5). Así el
    # resto de la app (y el desarrollo en Windows) no depende de esas libs.
    from weasyprint import HTML

    html = render_invoice_html(inv, items, emisor, cuit_emisor, pais_ds)
    pdf = HTML(string=html).write_pdf()
    if pdf is None:  # write_pdf sin target siempre devuelve bytes
        raise RuntimeError("weasyprint no devolvió bytes del PDF")
    return pdf

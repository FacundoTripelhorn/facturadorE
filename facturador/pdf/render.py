"""Render del comprobante: fila SQLite → HTML (Jinja2) → PDF (weasyprint).

El layout replica el comprobante real de Comprobantes en Línea analizado en
spike.md §0.1. Los datos del emisor (leyenda IVA, IIBB, inicio de
actividades) salen de la config local; el resto es el snapshot inmutable de
la factura autorizada. Autoescape SIEMPRE activo: razón social, domicilio y
descripciones son texto libre hostil (checklist §2.1.1 punto 4 aplica al
HTML igual que al XML).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from ..config import Config
from ..constants import (
    TIPO_EXPO_BIENES,
    TIPO_EXPO_OTROS,
    TIPO_EXPO_SERVICIOS,
    UMED_UNIDADES,
)
from .qr import build_qr_payload, qr_png_data_uri, qr_url

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=True,
)
_env.globals["UMED_UNIDADES"] = UMED_UNIDADES


def _fecha_larga(aaaammdd: str) -> str:
    return f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"


_env.filters["fecha"] = _fecha_larga


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
    config: Config,
    cuit_emisor: int,
    pais_ds: str = "",
) -> str:
    payload = build_qr_payload(inv, cuit_emisor)
    template = _env.get_template("invoice.html")
    return template.render(
        inv=inv,
        items=items,
        emisor=config.emisor,
        cuit_emisor=cuit_emisor,
        es_homo=config.env == "homo",
        pais_ds=pais_ds or str(inv["dst_cmp"]),
        tipo_expo_ds=_TIPO_EXPO_DS.get(inv["tipo_expo"], str(inv["tipo_expo"])),
        qr_data_uri=qr_png_data_uri(qr_url(payload)),
        nro_completo=f"{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}",
    )


def render_invoice_pdf(
    inv: sqlite3.Row,
    items: list[sqlite3.Row],
    config: Config,
    cuit_emisor: int,
    pais_ds: str = "",
) -> bytes:
    html = render_invoice_html(inv, items, config, cuit_emisor, pais_ds)
    pdf = HTML(string=html).write_pdf()
    assert pdf is not None  # write_pdf sin target devuelve bytes
    return pdf

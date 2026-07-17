"""Render del comprobante: fila SQLite → HTML (Jinja2) → PDF (weasyprint).

El layout replica el comprobante real de Comprobantes en Línea analizado en
design.md §0.1. Todos los valores impresos salen del snapshot inmutable de
la factura e ítems (FAC-10 / FAC-52): emisor, CUIT fiscal, receptor,
descripciones de params y ``pdf_render_version``. No se relee emisores,
clients, settings ni arca_params. Autoescape SIEMPRE activo: razón social,
domicilio y descripciones (incluidos los del emisor, texto libre) son
hostiles (checklist §2.1.1 punto 4 aplica al HTML igual que al XML).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ..constants import MONEDA_DISPLAY, PDF_RENDER_VERSION
from ..settings import Emisor, emisor_from_invoice_snapshot
from .qr import build_qr_payload, qr_png_data_uri, qr_url

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=True,
)


def _fecha_larga(aaaammdd: str) -> str:
    return f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"


def _num(valor: str | Decimal, decimales: int) -> str:
    """Formato numérico del comprobante real: coma decimal, sin separador
    de miles (cantidades y precios unitarios van con 6 decimales; importes,
    con 2)."""
    return f"{Decimal(str(valor)):.{decimales}f}".replace(".", ",")


_env.filters["fecha"] = _fecha_larga
# DOL → USD para el lector; el código ARCA viaja solo en el XML.
# Alias atado a PDF_RENDER_VERSION (FAC-52): cambiarlo exige bump de versión.
_env.filters["moneda"] = lambda code: MONEDA_DISPLAY.get(code, code)
_env.filters["num"] = _num
# La cotización es el único número que el comprobante real imprime con
# punto decimal (6 decimales).
_env.filters["ctz"] = lambda valor: f"{Decimal(str(valor)):.6f}"


def invoice_pdf_filename(inv: sqlite3.Row) -> str:
    return (
        f"factura-E-{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}"
        f"-{inv['environment']}.pdf"
    )


def _require_cuit_emisor(inv: sqlite3.Row) -> int:
    raw = inv["cuit_emisor"] if "cuit_emisor" in inv.keys() else None
    if raw is None or str(raw).strip() == "":
        raise ValueError(
            f"La factura {inv['id']} no tiene CUIT fiscal snapshot; "
            "no se puede regenerar el PDF."
        )
    return int(raw)


def _umed_label(item: sqlite3.Row) -> str:
    """Etiqueta de U. Medida del ítem (FAC-52); fallback al código."""
    keys = item.keys()
    if "pro_umed_ds" in keys and item["pro_umed_ds"]:
        return str(item["pro_umed_ds"])
    return str(item["pro_umed"])


def render_invoice_html(inv: sqlite3.Row, items: list[sqlite3.Row]) -> str:
    """HTML del comprobante solo desde datos inmutables de la factura.

    El despacho por ``pdf_render_version`` llega en FAC-53; hoy se valida
    que la fila declare la versión del contrato actual.
    """
    version = (
        inv["pdf_render_version"]
        if "pdf_render_version" in inv.keys()
        else None
    )
    if version is None:
        raise ValueError(
            f"La factura {inv['id']} no tiene pdf_render_version; "
            "crear un borrador nuevo con el esquema actual."
        )
    if int(version) != PDF_RENDER_VERSION:
        # Hasta FAC-53 no hay registry de renderers; fallar en claro.
        raise ValueError(
            f"La factura {inv['id']} pide pdf_render_version={version}, "
            f"pero este proceso solo conoce la versión {PDF_RENDER_VERSION}."
        )

    emisor: Emisor = emisor_from_invoice_snapshot(inv)
    if not emisor.completo:
        raise ValueError(
            "La factura no tiene snapshot completo del emisor; "
            "no se puede generar el PDF."
        )
    cuit_emisor = _require_cuit_emisor(inv)
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
        pais_ds=inv["dst_cmp_ds"] or str(inv["dst_cmp"]),
        cuit_pais_ds=inv["cuit_pais_cliente_ds"] or "",
        moneda_ds=inv["moneda_ds"] or "",
        umed_label=_umed_label,
        qr_data_uri=qr_png_data_uri(qr_url(payload)),
        nro_completo=f"{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}",
    )


def render_invoice_pdf(inv: sqlite3.Row, items: list[sqlite3.Row]) -> bytes:
    # Import perezoso: weasyprint necesita Pango/GTK del sistema, que solo
    # está garantizado dentro de la imagen Docker (design.md §2.5). Así el
    # resto de la app (y el desarrollo en Windows) no depende de esas libs.
    from weasyprint import HTML

    html = render_invoice_html(inv, items)
    pdf = HTML(string=html).write_pdf()
    if pdf is None:  # write_pdf sin target siempre devuelve bytes
        raise RuntimeError("weasyprint no devolvió bytes del PDF")
    return pdf

"""QR de la RG 4892: JSON estándar en base64 dentro de la URL de ARCA.

El payload replica el esquema publicado en https://www.afip.gob.ar/fe/qr/
(especificación v1). Para Factura E el receptor es del exterior: como
documento del receptor viaja el CUIT país (tipo 80), el mismo
``Cuit_pais_cliente`` que se informó en FEXAuthorize.
"""

from __future__ import annotations

import base64
import io
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

import qrcode

QR_BASE_URL = "https://www.afip.gob.ar/fe/qr/"

TIPO_DOC_CUIT = 80  # tabla de tipos de documento de ARCA


def _monto(value: str) -> float:
    """TEXT decimal de la DB → número JSON con 2 decimales (spec RG 4892)."""
    return float(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build_qr_payload(inv: sqlite3.Row, cuit_emisor: int) -> dict:
    """Payload RG 4892 para una factura AUTORIZADA (requiere CAE)."""
    if not inv["cae"]:
        raise ValueError("El QR RG 4892 requiere una factura autorizada con CAE")
    fecha = inv["fecha_cbte"]  # AAAAMMDD → YYYY-MM-DD
    return {
        "ver": 1,
        "fecha": f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}",
        "cuit": cuit_emisor,
        "ptoVta": inv["punto_venta"],
        "tipoCmp": inv["cbte_tipo"],
        "nroCmp": inv["cbte_nro"],
        "importe": _monto(inv["imp_total"]),
        "moneda": inv["moneda_id"],
        "ctz": float(Decimal(inv["moneda_ctz"])),
        "tipoDocRec": TIPO_DOC_CUIT,
        "nroDocRec": inv["cuit_pais_cliente"],
        "tipoCodAut": "E",  # CAE (A sería CAEA)
        "codAut": int(inv["cae"]),
    }


def qr_url(payload: dict) -> str:
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("ascii")
    ).decode("ascii")
    return f"{QR_BASE_URL}?p={encoded}"


def qr_png_data_uri(url: str) -> str:
    """PNG del QR como data URI, para incrustar en el HTML del PDF."""
    image = qrcode.make(url, box_size=6, border=2)
    buffer = io.BytesIO()
    image.save(buffer)  # PilImage emite PNG por defecto
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"

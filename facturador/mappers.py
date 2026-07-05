"""Conversiones entre filas SQLite, raw_request (JSON) y el Invoice de WSFEX.

raw_request es el contrato de idempotencia (spike.md §2.3 regla 5): el
reintento reconstruye el request EXACTO persistido, no la fila actual.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from .arca.wsfex import Invoice, InvoiceItem


def dec(value: Decimal) -> str:
    """Decimal → str sin notación científica (formato que espera ARCA)."""
    return format(value, "f")


def row_to_wsfex_invoice(
    inv: sqlite3.Row, arca_id: int, cbte_nro: int, items: list[sqlite3.Row]
) -> Invoice:
    return Invoice(
        arca_id=arca_id,
        fecha_cbte=inv["fecha_cbte"],
        punto_vta=inv["punto_venta"],
        cbte_nro=cbte_nro,
        dst_cmp=inv["dst_cmp"],
        cliente=inv["cliente"],
        cuit_pais_cliente=inv["cuit_pais_cliente"],
        domicilio_cliente=inv["domicilio_cliente"],
        id_impositivo=inv["id_impositivo"],
        moneda_ctz=Decimal(inv["moneda_ctz"]),
        imp_total=Decimal(inv["imp_total"]),
        fecha_pago=inv["fecha_pago"],
        forma_pago=inv["forma_pago"],
        cbte_tipo=inv["cbte_tipo"],
        tipo_expo=inv["tipo_expo"],
        permiso_existente=inv["permiso_existente"],
        moneda_id=inv["moneda_id"],
        incoterms=inv["incoterms"],
        idioma_cbte=inv["idioma_cbte"],
        obs=inv["obs"],
        items=[
            InvoiceItem(
                pro_ds=i["pro_ds"],
                pro_precio_uni=Decimal(i["pro_precio_uni"]),
                pro_codigo=i["pro_codigo"],
                pro_qty=Decimal(i["pro_qty"]),
                pro_umed=i["pro_umed"],
            )
            for i in items
        ],
    )


def wsfex_invoice_to_raw(invoice: Invoice) -> dict:
    raw = {
        k: (format(v, "f") if isinstance(v, Decimal) else v)
        for k, v in vars(invoice).items()
        if k != "items"
    }
    raw["items"] = [
        {
            "pro_ds": i.pro_ds,
            "pro_precio_uni": format(i.pro_precio_uni, "f"),
            "pro_codigo": i.pro_codigo,
            "pro_qty": format(i.pro_qty, "f"),
            "pro_umed": i.pro_umed,
            "pro_bonificacion": format(i.pro_bonificacion, "f"),
        }
        for i in invoice.items
    ]
    return raw


def raw_to_wsfex_invoice(raw: dict) -> Invoice:
    datos = dict(raw)
    items = [
        InvoiceItem(
            pro_ds=i["pro_ds"],
            pro_precio_uni=Decimal(i["pro_precio_uni"]),
            pro_codigo=i["pro_codigo"],
            pro_qty=Decimal(i["pro_qty"]),
            pro_umed=i["pro_umed"],
            # Default para raw_requests persistidos antes de guardarse este
            # campo: eran siempre facturas sin bonificación.
            pro_bonificacion=Decimal(i.get("pro_bonificacion", "0")),
        )
        for i in datos.pop("items")
    ]
    for campo in ("moneda_ctz", "imp_total"):
        datos[campo] = Decimal(datos[campo])
    return Invoice(items=items, **datos)

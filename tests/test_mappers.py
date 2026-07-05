"""raw_request es el contrato de idempotencia (§2.3 regla 5): el round-trip
Invoice → raw → Invoice debe preservar TODOS los campos, y los raw viejos
persistidos en la DB tienen que seguir cargando."""

from decimal import Decimal

from facturador.arca.wsfex import Invoice, InvoiceItem
from facturador.mappers import raw_to_wsfex_invoice, wsfex_invoice_to_raw


def _factura(**overrides) -> Invoice:
    campos = dict(
        arca_id=7,
        fecha_cbte="20260705",
        punto_vta=1,
        cbte_nro=3,
        dst_cmp=225,
        cliente="CLIENTE URUGUAY S.A.",
        cuit_pais_cliente=55000002002,
        domicilio_cliente="Av. Siempreviva 123",
        id_impositivo="RUT 219999830019",
        moneda_ctz=Decimal("1145.5690"),
        imp_total=Decimal("1450.00"),
        fecha_pago="20260706",
        forma_pago="WIRE TRANSFER",
        items=[
            InvoiceItem(
                pro_ds="Servicios",
                pro_precio_uni=Decimal("1500.00"),
                pro_bonificacion=Decimal("50.00"),
            )
        ],
    )
    campos.update(overrides)
    return Invoice(**campos)


def test_round_trip_preserva_todos_los_campos_incluida_bonificacion():
    original = _factura()
    reconstruida = raw_to_wsfex_invoice(wsfex_invoice_to_raw(original))
    assert reconstruida == original
    assert reconstruida.items[0].pro_bonificacion == Decimal("50.00")
    assert reconstruida.items[0].pro_total_item == Decimal("1450.00")


def test_raw_viejo_sin_bonificacion_sigue_cargando():
    """Compat: los raw_request ya persistidos no traen pro_bonificacion."""
    raw = wsfex_invoice_to_raw(
        _factura(
            imp_total=Decimal("1500.00"),
            items=[InvoiceItem(pro_ds="Servicios", pro_precio_uni=Decimal("1500.00"))],
        )
    )
    for item in raw["items"]:
        del item["pro_bonificacion"]  # como quedó guardado antes de este campo
    reconstruida = raw_to_wsfex_invoice(raw)
    assert reconstruida.items[0].pro_bonificacion == Decimal(0)

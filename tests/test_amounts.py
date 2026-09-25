"""Formato de importes de WSFEX: validación antes de armar FEXAuthorize.

Límites del manual WSFEX V3.1.1 (ver ``facturador/arca/amounts.py``). Los
valores patológicos (NaN, Infinity, exponentes enormes) se rechazan con 422
y mensaje en castellano, sin que ``format`` llegue a materializarlos.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from facturador import repo
from facturador.arca.amounts import (
    IMP_TOTAL,
    MONEDA_CTZ,
    PRO_BONIFICACION,
    PRO_PRECIO_UNI,
    PRO_QTY,
    PRO_TOTAL_ITEM,
    ImporteFueraDeFormato,
    validar_importe,
)
from facturador.arca.wsfex import Invoice, InvoiceItem, _dec, build_cmp_element
from facturador.schemas import InvoiceCreate, ItemIn
from tests.conftest import with_csrf

PATOLOGICOS = ["1e1000000000", "1e300", "NaN", "Infinity", "-Infinity", "-1", "0"]

# (formato, máximo aceptado, un entero de más, un decimal de más)
LIMITES = [
    (IMP_TOTAL, "9999999999999.99", "10000000000000", "0.001"),
    (MONEDA_CTZ, "9999.999999", "10000", "0.0000001"),
    (PRO_QTY, "999999999999.999999", "1000000000000", "0.0000001"),
    (PRO_PRECIO_UNI, "999999999999.999999", "1000000000000", "0.0000001"),
    (PRO_BONIFICACION, "999999999999.999999", "1000000000000", "0.0000001"),
    (PRO_TOTAL_ITEM, "9999999999999.99", "10000000000000", "0.001"),
]


# --- helper ---


@pytest.mark.parametrize(("formato", "maximo", "_e", "_d"), LIMITES)
def test_acepta_el_maximo_de_cada_campo(formato, maximo, _e, _d):
    assert validar_importe(Decimal(maximo), formato) == Decimal(maximo)


@pytest.mark.parametrize(("formato", "_m", "entero_de_mas", "decimal_de_mas"), LIMITES)
def test_rechaza_un_digito_de_mas(formato, _m, entero_de_mas, decimal_de_mas):
    for valor in (entero_de_mas, decimal_de_mas):
        with pytest.raises(ImporteFueraDeFormato, match="excede el formato de ARCA"):
            validar_importe(Decimal(valor), formato)


@pytest.mark.parametrize("valor", PATOLOGICOS)
def test_rechaza_valores_patologicos(valor):
    with pytest.raises(ImporteFueraDeFormato):
        validar_importe(Decimal(valor), IMP_TOTAL)


def test_mensajes_en_castellano():
    casos = {
        "NaN": "debe ser un número finito",
        "Infinity": "debe ser un número finito",
        "-1": "no puede ser negativo",
        "0": "debe ser mayor a cero",
        "1e1000000000": "hasta 13 enteros y 2 decimales",
    }
    for valor, mensaje in casos.items():
        with pytest.raises(ImporteFueraDeFormato, match=mensaje):
            validar_importe(Decimal(valor), IMP_TOTAL)


def test_cero_solo_donde_se_permite():
    assert validar_importe(Decimal("0.00"), PRO_BONIFICACION, permite_cero=True) == 0
    with pytest.raises(ImporteFueraDeFormato):
        validar_importe(Decimal("-0.01"), PRO_BONIFICACION, permite_cero=True)


def test_ceros_de_mas_a_la_derecha_no_cuentan_y_se_recortan():
    valor = validar_importe(Decimal("100.000"), IMP_TOTAL)
    assert format(valor, "f") == "100.00"
    # Los que entran en el formato quedan como vienen.
    assert format(validar_importe(Decimal("1.50"), IMP_TOTAL), "f") == "1.50"
    # Exponente positivo chico: 1E+2 es 100.
    assert format(validar_importe(Decimal("1E+2"), IMP_TOTAL), "f") == "100"


# --- esquemas de entrada ---


@pytest.mark.parametrize("valor", PATOLOGICOS)
def test_invoice_create_rechaza_importe_y_cotizacion(valor):
    with pytest.raises(ValidationError):
        InvoiceCreate(imp_total=valor)
    with pytest.raises(ValidationError):
        InvoiceCreate(imp_total="100", moneda_ctz=valor)


@pytest.mark.parametrize("valor", PATOLOGICOS)
def test_item_rechaza_precio_y_cantidad(valor):
    with pytest.raises(ValidationError):
        ItemIn(pro_ds="x", pro_precio_uni=valor)
    with pytest.raises(ValidationError):
        ItemIn(pro_ds="x", pro_precio_uni="1", pro_qty=valor)


def test_esquemas_aceptan_los_maximos():
    InvoiceCreate(imp_total="9999999999999.99", moneda_ctz="9999.999999")
    ItemIn(
        pro_ds="x",
        pro_precio_uni="999999999999.999999",
        pro_qty="999999999999.999999",
    )


# --- API JSON ---


CLIENTE = {
    "razon_social": "CLIENTE URUGUAY S.A.",
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "RUT 219999830019",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": True,
}


@pytest.fixture
def api_con_cliente(api):
    r = api.post("/clients", json=CLIENTE)
    assert r.status_code == 201, r.text
    return api


def _detalle(r) -> str:
    return str(r.json()["detail"])


@pytest.mark.parametrize("valor", PATOLOGICOS)
def test_api_rechaza_importe_patologico(api_con_cliente, valor):
    r = api_con_cliente.post("/invoices", json={"imp_total": valor})
    assert r.status_code == 422
    assert "El importe total" in _detalle(r)


@pytest.mark.parametrize("valor", PATOLOGICOS)
def test_api_rechaza_cotizacion_patologica(api_con_cliente, valor):
    r = api_con_cliente.post(
        "/invoices", json={"imp_total": "100.00", "moneda_ctz": valor}
    )
    assert r.status_code == 422
    assert "La cotización" in _detalle(r)


@pytest.mark.parametrize(
    ("campo", "valor", "etiqueta"),
    [
        ("pro_precio_uni", "1e1000000000", "El precio unitario"),
        ("pro_precio_uni", "0.0000001", "El precio unitario"),
        ("pro_qty", "NaN", "La cantidad"),
        ("pro_qty", "1000000000000", "La cantidad"),
    ],
)
def test_api_rechaza_items_fuera_de_formato(api_con_cliente, campo, valor, etiqueta):
    item = {"pro_ds": "Servicio", "pro_precio_uni": "100.00", campo: valor}
    r = api_con_cliente.post(
        "/invoices", json={"imp_total": "100.00", "items": [item]}
    )
    assert r.status_code == 422
    assert etiqueta in _detalle(r)


@pytest.mark.parametrize(
    ("campo", "entero_de_mas", "decimal_de_mas"),
    [
        ("imp_total", "10000000000000", "100.001"),
        ("moneda_ctz", "10000", "1.0000001"),
    ],
)
def test_api_rechaza_un_digito_de_mas(
    api_con_cliente, campo, entero_de_mas, decimal_de_mas
):
    for valor in (entero_de_mas, decimal_de_mas):
        body = {"imp_total": "100.00", campo: valor}
        r = api_con_cliente.post("/invoices", json=body)
        assert r.status_code == 422, valor
        assert "excede el formato de ARCA" in _detalle(r)


def test_api_acepta_los_maximos(api_con_cliente):
    r = api_con_cliente.post(
        "/invoices",
        json={"imp_total": "9999999999999.99", "moneda_ctz": "9999.999999"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["imp_total"] == "9999999999999.99"


def test_total_del_item_con_mas_de_dos_decimales_es_422(api_con_cliente):
    # 3 × 0.333333 = 0.999999: ARCA recibe Pro_total_item con 2 decimales.
    item = {"pro_ds": "Servicio", "pro_precio_uni": "0.333333", "pro_qty": "3"}
    r = api_con_cliente.post(
        "/invoices", json={"imp_total": "1.00", "items": [item]}
    )
    assert r.status_code == 422
    assert "Ítem 1 (cantidad × precio)" in _detalle(r)
    assert "El total del ítem excede el formato de ARCA" in _detalle(r)


def test_total_del_item_con_demasiados_enteros_es_422(api_con_cliente):
    item = {
        "pro_ds": "Servicio",
        "pro_precio_uni": "999999999999",
        "pro_qty": "100",
    }
    # 999999999999 × 100 tiene 14 enteros; imp_total en sí es válido.
    r = api_con_cliente.post(
        "/invoices", json={"imp_total": "1.00", "items": [item]}
    )
    assert r.status_code == 422
    assert "Ítem 1 (cantidad × precio)" in _detalle(r)
    assert "hasta 13 enteros y 2 decimales" in _detalle(r)


def test_cotizacion_de_arca_fuera_de_formato_es_422(api_con_cliente, arca):
    arca.ctz = "12345.6"  # 5 enteros: Moneda_ctz admite 4
    r = api_con_cliente.post("/invoices", json={"imp_total": "100.00"})
    assert r.status_code == 422
    assert "La cotización excede el formato de ARCA" in _detalle(r)


# --- formulario ---


FACTURA_FORM = {
    "imp_total": "1500,00",
    "fecha_pago": "2026-07-05",
    "descripcion": "Servicios de desarrollo de software",
    "obs": "",
    "client_id": "",
}


@pytest.mark.parametrize(
    ("valor", "mensaje"),
    [
        ("0", "El importe total debe ser mayor a cero"),
        ("100,001", "hasta 13 enteros y 2 decimales"),
        ("12.345.678.901.234,00", "hasta 13 enteros y 2 decimales"),
        # El form acepta solo coma decimal: ni exponentes ni NaN llegan a
        # Decimal.
        ("1e1000000000", "no es un importe válido"),
        ("NaN", "no es un importe válido"),
        ("1500.50", "usá coma para los decimales"),
    ],
)
def test_form_muestra_el_error_como_los_demas(api_con_cliente, valor, mensaje):
    r = api_con_cliente.post(
        "/ui/facturas",
        data=with_csrf(api_con_cliente, {**FACTURA_FORM, "imp_total": valor}),
    )
    assert r.status_code == 422
    assert "Datos inválidos" in r.text
    assert mensaje in r.text
    assert "Value error" not in r.text


# --- guardas antes de enviar ---


def test_borrador_fuera_de_formato_no_llega_a_arca(api_con_cliente, arca):
    """Una fila que no pasó por la validación de entrada (datos viejos o
    editados a mano) se frena antes de FEXAuthorize y vuelve a borrador."""
    api = api_con_cliente
    r = api.post("/invoices", json={"imp_total": "100.00"})
    assert r.status_code == 201, r.text
    invoice_id = r.json()["id"]
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET imp_total = '100.001' WHERE id = ?", (invoice_id,)
        )
        api.conn.execute(
            "UPDATE invoice_items SET pro_precio_uni = '100.001', "
            "pro_total_item = '100.001' WHERE invoice_id = ?",
            (invoice_id,),
        )

    r = api.post(f"/invoices/{invoice_id}/authorize")

    assert r.status_code == 409
    assert "excede el formato de ARCA" in _detalle(r)
    assert arca.calls["FEXAuthorize"] == 0
    inv = repo.get_invoice(api.conn, invoice_id)
    assert inv["status"] == "draft"
    assert inv["raw_request"] is None


def _invoice(**overrides) -> Invoice:
    datos = dict(
        arca_id=1,
        fecha_cbte="20260705",
        punto_vta=1,
        cbte_nro=1,
        dst_cmp=225,
        cliente="CLIENTE",
        cuit_pais_cliente=55000002002,
        domicilio_cliente="Dom",
        id_impositivo="RUT",
        moneda_ctz=Decimal("1145.569"),
        imp_total=Decimal("100.00"),
        fecha_pago="20260705",
        forma_pago="WIRE",
        items=[InvoiceItem(pro_ds="Servicio", pro_precio_uni=Decimal("100.00"))],
    )
    datos.update(overrides)
    return Invoice(**datos)


def test_serializador_no_arma_importes_fuera_de_formato():
    build_cmp_element(_invoice())  # control: el caso normal arma el XML
    with pytest.raises(ValueError, match="La cotización"):
        build_cmp_element(_invoice(moneda_ctz=Decimal("1e1000000000")))
    with pytest.raises(ValueError, match="El importe total"):
        build_cmp_element(_invoice(imp_total=Decimal("NaN")))
    item = InvoiceItem(pro_ds="x", pro_precio_uni=Decimal("1e300"))
    with pytest.raises(ValueError, match="El precio unitario"):
        build_cmp_element(_invoice(items=[item]))


def test_dec_valida_antes_de_formatear():
    assert _dec(Decimal("100.000"), IMP_TOTAL) == "100.00"
    with pytest.raises(ValueError):
        _dec(Decimal("1e1000000000"), IMP_TOTAL)

"""Importes en la UI: coma decimal y punto de miles (formato argentino)."""

from decimal import Decimal

import pytest

from facturador.web.numeros import formato_importe, parse_importe


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1500", "1500"),
        ("1500,5", "1500.5"),
        ("1500,50", "1500.50"),
        ("1.500", "1500"),
        ("1.500,50", "1500.50"),
        ("12.345.678,9", "12345678.9"),
        (" 1.500,50 ", "1500.50"),
        ("0,01", "0.01"),
        ("-5", "-5"),  # el signo lo rechaza la validación de importes
    ],
)
def test_parse_acepta_coma_decimal_y_punto_de_miles(texto, esperado):
    assert parse_importe(texto) == Decimal(esperado)
    assert str(parse_importe(texto)) == esperado


@pytest.mark.parametrize("texto", ["1500.50", "1.5", "0.01"])
def test_parse_rechaza_punto_decimal(texto):
    with pytest.raises(ValueError, match="usá coma para los decimales"):
        parse_importe(texto)


@pytest.mark.parametrize(
    "texto",
    ["", "abc", "1,500,00", "1.50,00", "15.00.00", "1e300", "NaN", "Infinity", "1,"],
)
def test_parse_rechaza_lo_que_no_es_un_importe(texto):
    with pytest.raises(ValueError, match="no es un importe válido"):
        parse_importe(texto)


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("1500.00", "1.500,00"),
        (Decimal("1234567.5"), "1.234.567,5"),
        ("999", "999"),
        ("1000", "1.000"),
        ("0.01", "0,01"),
        ("1.000000", "1,000000"),  # conserva los decimales guardados
        ("1145.5690", "1.145,5690"),
        ("-1500.00", "-1.500,00"),
        ("9999999999999.99", "9.999.999.999.999,99"),
    ],
)
def test_formato_para_mostrar(valor, esperado):
    assert formato_importe(valor) == esperado


@pytest.mark.parametrize("valor", [None, "", "abc", "NaN"])
def test_formato_deja_pasar_lo_que_no_es_un_numero(valor):
    assert formato_importe(valor) == ("" if valor in (None, "") else valor)


def test_parse_y_formato_son_inversos():
    for texto in ("1.500,00", "0,01", "9.999.999.999.999,99", "12,5"):
        assert formato_importe(parse_importe(texto)) == texto

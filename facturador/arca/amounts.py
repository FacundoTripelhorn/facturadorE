"""Formato de los importes que viajan a WSFEX.

Límites del manual del desarrollador WSFEX V3.1.1 (FEXAuthorize):

============================  ========  =========  =====================
Campo                         Enteros   Decimales  Valor (ARCA)
============================  ========  =========  =====================
``Imp_total``                 13        2          >= 0 (err. 1610)
``Moneda_ctz``                4         6          > 0 (err. 1600)
``Pro_qty``                   12        6          > 0 (err. 1780/1813)
``Pro_precio_uni``            12        6          >= 0 (err. 1800/1814)
``Pro_bonificacion``          12        6          >= 0 (err. 1811/1817)
``Pro_total_item``            13        2          >= 0 (err. 1810/1816)
============================  ========  =========  =====================

La app es más estricta que ARCA donde el producto lo pide (importe,
cantidad y precio > 0): eso lo decide quien llama con ``permite_cero``.

Todo se inspecciona con ``Decimal.as_tuple()``, sin aritmética ni
``normalize()``: con exponentes enormes (``Decimal("1e1000000000")``)
``normalize()`` lanza ``Overflow`` y ``format(x, "f")`` materializa un
string de ~1 GB. Un valor que pasa este chequeo es chico y se puede
serializar sin riesgo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FormatoImporte:
    campo: str
    etiqueta: str
    enteros: int
    decimales: int


IMP_TOTAL = FormatoImporte("Imp_total", "El importe total", 13, 2)
MONEDA_CTZ = FormatoImporte("Moneda_ctz", "La cotización", 4, 6)
PRO_QTY = FormatoImporte("Pro_qty", "La cantidad", 12, 6)
PRO_PRECIO_UNI = FormatoImporte("Pro_precio_uni", "El precio unitario", 12, 6)
PRO_BONIFICACION = FormatoImporte("Pro_bonificacion", "La bonificación", 12, 6)
PRO_TOTAL_ITEM = FormatoImporte("Pro_total_item", "El total del ítem", 13, 2)


class ImporteFueraDeFormato(ValueError):
    """El importe no respeta el formato de ARCA para su campo."""


def validar_importe(
    valor: Decimal, formato: FormatoImporte, *, permite_cero: bool = False
) -> Decimal:
    """Valida ``valor`` contra ``formato`` y lo devuelve listo para serializar.

    Rechaza NaN/Infinity, negativos, cero (salvo ``permite_cero``) y más
    enteros o decimales de los que admite el campo. Los ceros a la derecha
    que sobran no cuentan (``100.000`` en un campo de 2 decimales es
    ``100.00``); el valor devuelto ya no los tiene, sin redondear.
    """
    etiqueta = formato.etiqueta
    if not valor.is_finite():
        raise ImporteFueraDeFormato(f"{etiqueta} debe ser un número finito.")
    if valor.is_signed() and not valor.is_zero():
        raise ImporteFueraDeFormato(f"{etiqueta} no puede ser negativo.")
    if valor.is_zero():
        if not permite_cero:
            raise ImporteFueraDeFormato(f"{etiqueta} debe ser mayor a cero.")
        return Decimal(0)

    tup = valor.as_tuple()
    digitos = list(tup.digits)
    exponente = tup.exponent
    assert isinstance(exponente, int)  # finito: el exponente es un int
    # Ceros a la derecha de la coma más allá de los decimales del campo: no
    # son significativos. Los que entran en el formato se dejan como vienen.
    while exponente < -formato.decimales and digitos and digitos[-1] == 0:
        digitos.pop()
        exponente += 1
    decimales = -exponente if exponente < 0 else 0
    enteros = max(len(digitos) + exponente, 0)
    if enteros > formato.enteros or decimales > formato.decimales:
        raise ImporteFueraDeFormato(
            f"{etiqueta} excede el formato de ARCA: hasta {formato.enteros} "
            f"enteros y {formato.decimales} decimales."
        )
    return Decimal((0, tuple(digitos), exponente))

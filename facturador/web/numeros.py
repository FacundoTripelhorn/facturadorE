"""Importes en la UI con formato argentino: coma decimal, punto de miles.

Solo la interfaz HTML usa este formato. La API JSON, la DB y ARCA siguen
con punto decimal y sin separador de miles.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# 1500 · 1500,5 · 1.500 · 1.500,50 · 12.345.678,9 (signo opcional: el rechazo
# de negativos queda para la validación de importes, con su mensaje).
_SIN_MILES = re.compile(r"-?\d+(,\d+)?")
_CON_MILES = re.compile(r"-?\d{1,3}(\.\d{3})+(,\d+)?")
# 1500.50: punto como separador decimal, el formato que ya no se acepta.
_PUNTO_DECIMAL = re.compile(r"-?\d+\.\d+")


def parse_importe(texto: str) -> Decimal:
    """Texto del formulario → Decimal. Lanza ValueError con mensaje en
    castellano si no es un importe con coma decimal.

    ``1.500`` es mil quinientos (punto de miles). ``1500.50`` se rechaza en
    lugar de adivinar: en una factura, leer mal el separador multiplica el
    importe por 100 o por 1000.
    """
    limpio = texto.strip().replace(" ", "")
    if _SIN_MILES.fullmatch(limpio) or _CON_MILES.fullmatch(limpio):
        return Decimal(limpio.replace(".", "").replace(",", "."))
    if _PUNTO_DECIMAL.fullmatch(limpio):
        raise ValueError(
            f"{texto.strip()!r}: usá coma para los decimales (p.ej. 1.500,50)."
        )
    raise ValueError(f"{texto.strip()!r} no es un importe válido (p.ej. 1.500,50).")


def formato_importe(valor: object) -> str:
    """Importe guardado ("1500.00", Decimal) → "1.500,00" para mostrar.

    Conserva los decimales tal como están guardados (la cantidad
    ``1.000000`` se ve ``1,000000``). Si el valor no es un número, lo
    devuelve sin tocar.
    """
    if valor is None or valor == "":
        return ""
    try:
        numero = Decimal(str(valor))
    except InvalidOperation:
        return str(valor)
    if not numero.is_finite():
        return str(valor)
    signo = "-" if numero < 0 else ""
    texto = format(numero.copy_abs(), "f")
    entero, _, decimales = texto.partition(".")
    grupos: list[str] = []
    while len(entero) > 3:
        grupos.insert(0, entero[-3:])
        entero = entero[:-3]
    grupos.insert(0, entero)
    resultado = signo + ".".join(grupos)
    return f"{resultado},{decimales}" if decimales else resultado

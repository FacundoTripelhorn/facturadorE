"""Geometría del gráfico de barras por mes del resumen de facturación.

El SVG se arma en la plantilla con estas coordenadas: sin librerías de JS y
sin colores acá; las clases de ``app.css`` le dan los tokens de cada tema.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..resumen import MESES, Total

ANCHO = 480
ALTO = 170
_MARGEN_X = 8
_MARGEN_ARRIBA = 26  # espacio para la etiqueta del máximo
_MARGEN_ABAJO = 22  # espacio para el nombre corto del mes
_PROPORCION_BARRA = 0.62  # ancho de la barra dentro de su franja


@dataclass(frozen=True)
class Barra:
    mes: str
    abrev: str
    total: Total
    x: float
    y: float
    ancho: float
    alto: float
    centro: float


@dataclass(frozen=True)
class Grafico:
    ancho: int
    alto: int
    barras: list[Barra]
    base: float  # y del eje
    tope: float  # y de la línea del máximo
    maximo: Decimal


def grafico_por_mes(por_mes: list[Total]) -> Grafico:
    """Una barra por mes, proporcional al mes más alto. Importes de una sola
    moneda: el llamador arma un gráfico por moneda."""
    alto_util = ALTO - _MARGEN_ARRIBA - _MARGEN_ABAJO
    base = ALTO - _MARGEN_ABAJO
    franja = (ANCHO - 2 * _MARGEN_X) / len(MESES)
    ancho_barra = franja * _PROPORCION_BARRA
    maximo = max((t.importe for t in por_mes), default=Decimal(0))
    barras = []
    for i, (mes, total) in enumerate(zip(MESES, por_mes, strict=True)):
        alto = float(total.importe / maximo) * alto_util if maximo > 0 else 0.0
        centro = _MARGEN_X + franja * i + franja / 2
        barras.append(
            Barra(
                mes=mes,
                abrev=mes[:3],
                total=total,
                x=round(centro - ancho_barra / 2, 2),
                y=round(base - alto, 2),
                ancho=round(ancho_barra, 2),
                alto=round(alto, 2),
                centro=round(centro, 2),
            )
        )
    return Grafico(
        ancho=ANCHO,
        alto=ALTO,
        barras=barras,
        base=base,
        tope=_MARGEN_ARRIBA,
        maximo=maximo,
    )

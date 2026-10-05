"""Dibuja los SVG de la marca (``web/static/brand``) con Pillow.

Un solo renderer para el ícono del .exe (``packaging/windows/make_icon.py``)
y para el ícono y el wordmark de las ventanas del launcher. Interpreta los
paths del SVG, los dibuja con la regla par-impar (los huecos de las letras
quedan vacíos) y, para que los bordes no salgan dentados, dibuja más grande
y reduce.

Solo se aceptan los comandos de path que usan los SVG de la marca
(``M L H V Q A Z``, en absoluto); cualquier otro corta con error en vez de
dibujar mal.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

BRAND_DIR = Path(__file__).resolve().parent / "web" / "static" / "brand"
ICONO_SVG = BRAND_DIR / "icon.svg"
WORDMARK_SVG = BRAND_DIR / "wordmark.svg"

# Segmentos por curva al aplanar: a 1024 px sobra para que no se noten.
PASOS_CURVA = 24
# Factor de sobremuestreo de ``rasterizar``: dibuja N veces más grande y
# reduce, así los bordes quedan suavizados.
SOBREMUESTREO = 4

Punto = tuple[float, float]
RGBA = tuple[int, int, int, int]

_SVG_NS = "{http://www.w3.org/2000/svg}"
_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARGUMENTOS = {"M": 2, "L": 2, "H": 1, "V": 1, "Q": 4, "A": 7, "Z": 0}


def _arco(
    inicio: Punto,
    rx: float,
    ry: float,
    rotacion: float,
    grande: bool,
    barrido: bool,
    fin: Punto,
) -> list[Punto]:
    """Puntos de un arco elíptico de SVG (parametrización por extremos)."""
    x1, y1 = inicio
    x2, y2 = fin
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [fin]
    phi = math.radians(rotacion)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    xp = cos_p * dx + sin_p * dy
    yp = -sin_p * dx + cos_p * dy
    # Radios demasiado chicos para unir los extremos: se agrandan (spec SVG).
    lam = (xp / rx) ** 2 + (yp / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx**2 * ry**2 - rx**2 * yp**2 - ry**2 * xp**2
    den = rx**2 * yp**2 + ry**2 * xp**2
    coef = math.sqrt(max(0.0, num / den))
    if grande == barrido:
        coef = -coef
    cxp, cyp = coef * rx * yp / ry, -coef * ry * xp / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2

    def angulo(ux: float, uy: float) -> float:
        return math.atan2(uy, ux)

    t1 = angulo((xp - cxp) / rx, (yp - cyp) / ry)
    t2 = angulo((-xp - cxp) / rx, (-yp - cyp) / ry)
    delta = t2 - t1
    if barrido and delta < 0:
        delta += 2 * math.pi
    elif not barrido and delta > 0:
        delta -= 2 * math.pi
    puntos = []
    for i in range(1, PASOS_CURVA + 1):
        t = t1 + delta * i / PASOS_CURVA
        x = rx * math.cos(t)
        y = ry * math.sin(t)
        puntos.append((cos_p * x - sin_p * y + cx, sin_p * x + cos_p * y + cy))
    return puntos


def subpaths(d: str) -> list[list[Punto]]:
    """Convierte el atributo ``d`` en polígonos (uno por subpath)."""
    tokens = _TOKEN.findall(d)
    resultado: list[list[Punto]] = []
    actual: list[Punto] = []
    pos: Punto = (0.0, 0.0)
    i = 0
    comando = ""
    while i < len(tokens):
        if tokens[i].isalpha():
            comando = tokens[i]
            i += 1
            if comando not in _ARGUMENTOS:
                raise ValueError(f"comando de path no soportado: {comando!r}")
            if comando == "Z":
                if actual:
                    resultado.append(actual)
                    pos = actual[0]
                actual = []
                continue
        elif not comando or comando == "Z":
            raise ValueError(f"número sin comando en el path: {tokens[i]!r}")
        n = _ARGUMENTOS[comando]
        args = [float(t) for t in tokens[i : i + n]]
        if len(args) != n:
            raise ValueError(f"faltan argumentos para {comando!r}")
        i += n
        if comando == "M":
            if actual:
                resultado.append(actual)
            pos = (args[0], args[1])
            actual = [pos]
            comando = "L"  # pares siguientes a un M son líneas (spec SVG)
            continue
        if comando == "L":
            nuevo = (args[0], args[1])
            actual.append(nuevo)
        elif comando == "H":
            nuevo = (args[0], pos[1])
            actual.append(nuevo)
        elif comando == "V":
            nuevo = (pos[0], args[0])
            actual.append(nuevo)
        elif comando == "Q":
            (cx, cy), nuevo = (args[0], args[1]), (args[2], args[3])
            for k in range(1, PASOS_CURVA + 1):
                t = k / PASOS_CURVA
                u = 1 - t
                actual.append(
                    (
                        u * u * pos[0] + 2 * u * t * cx + t * t * nuevo[0],
                        u * u * pos[1] + 2 * u * t * cy + t * t * nuevo[1],
                    )
                )
        else:  # A
            rx, ry, rot, grande, barrido, x, y = args
            nuevo = (x, y)
            actual.extend(
                _arco(pos, rx, ry, rot, bool(grande), bool(barrido), nuevo)
            )
        pos = nuevo
    if actual:
        resultado.append(actual)
    return resultado


def color_hex(valor: str) -> RGBA:
    """``#RRGGBB`` → RGBA opaco."""
    h = valor.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"se espera un color #RRGGBB: {valor!r}")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def view_box(svg: Path) -> tuple[float, float, float, float]:
    """``viewBox`` del SVG: x, y, ancho, alto."""
    raiz = ET.parse(svg).getroot()
    vx, vy, vw, vh = (float(v) for v in raiz.attrib["viewBox"].split())
    return vx, vy, vw, vh


def _relleno(
    elemento: ET.Element,
    colores: Mapping[str, RGBA],
    color_actual: RGBA | None,
) -> RGBA:
    """Color de un path: por clase, ``currentColor`` o el hex del SVG."""
    for clase in elemento.attrib.get("class", "").split():
        if clase in colores:
            return colores[clase]
    fill = elemento.attrib.get("fill", "")
    if fill == "currentColor":
        if color_actual is None:
            raise ValueError("el SVG usa currentColor: falta color_actual")
        return color_actual
    return color_hex(fill)


def render_svg(
    svg: Path,
    ancho: int,
    alto: int,
    *,
    colores: Mapping[str, RGBA] | None = None,
    color_actual: RGBA | None = None,
) -> Image.Image:
    """Dibuja el SVG en ``ancho`` × ``alto`` px, sin suavizado y con fondo
    transparente.

    ``colores`` pisa el relleno de los paths por clase (p. ej. la E del
    wordmark) y ``color_actual`` resuelve ``fill="currentColor"``.
    """
    raiz = ET.parse(svg).getroot()
    vx, vy, vw, vh = (float(v) for v in raiz.attrib["viewBox"].split())
    escala_x, escala_y = ancho / vw, alto / vh
    img = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    for elemento in raiz.iter(f"{_SVG_NS}path"):
        # Regla par-impar: los huecos de las letras quedan vacíos.
        mascara = Image.new("1", (ancho, alto), 0)
        for poligono in subpaths(elemento.attrib["d"]):
            capa = Image.new("1", (ancho, alto), 0)
            ImageDraw.Draw(capa).polygon(
                [((x - vx) * escala_x, (y - vy) * escala_y) for x, y in poligono],
                fill=1,
            )
            mascara = ImageChops.logical_xor(mascara, capa)
        color = _relleno(elemento, colores or {}, color_actual)
        img.paste(Image.new("RGBA", (ancho, alto), color), (0, 0), mascara)
    return img


def rasterizar(
    svg: Path,
    alto: int,
    *,
    fondo: RGBA,
    colores: Mapping[str, RGBA] | None = None,
    color_actual: RGBA | None = None,
) -> Image.Image:
    """El SVG a ``alto`` px (el ancho sale del ``viewBox``), suavizado y
    apoyado sobre ``fondo``: Tk no siempre respeta la transparencia, así que
    la imagen sale opaca, del color de lo que tiene detrás."""
    _, _, vw, vh = view_box(svg)
    ancho = max(1, round(alto * vw / vh))
    grande = render_svg(
        svg,
        ancho * SOBREMUESTREO,
        alto * SOBREMUESTREO,
        colores=colores,
        color_actual=color_actual,
    )
    chico = grande.resize((ancho, alto), Image.Resampling.LANCZOS)
    base = Image.new("RGBA", (ancho, alto), fondo)
    return Image.alpha_composite(base, chico)

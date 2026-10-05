"""Genera el ícono de FacturadorE (``facturadore.ico``) con Pillow.

El diseño vive en ``facturador/web/static/brand/icon.svg`` (todo en curvas,
colores fijos). Este script interpreta los paths del SVG, los dibuja a
1024 px y los reduce a los tamaños del .ico. El tamaño de 16 px no sale del
SVG: a esa escala "fe" se vuelve borroso, así que se arma desde el mapa de
píxeles dibujado a mano en ``icono_16.txt``. El binario no vive en el repo:
lo arma el build.

Solo se aceptan los comandos de path que usa el SVG (``M L H V Q A Z``, en
absoluto); cualquier otro corta con error en vez de dibujar mal.

Uso: ``uv run python packaging/windows/make_icon.py <salida.ico>``
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / "facturador" / "web" / "static" / "brand" / "icon.svg"
MAPA_16 = Path(__file__).resolve().parent / "icono_16.txt"

LADO = 1024
TAMANIOS = [16, 24, 32, 48, 64, 128, 256]
# Segmentos por curva al aplanar: a 1024 px sobra para que no se noten.
PASOS_CURVA = 24

COLORES_16 = {
    "V": (0x5B, 0x44, 0xBE, 255),
    "L": (0xB6, 0xA8, 0xF2, 255),
    "W": (255, 255, 255, 255),
    ".": (0, 0, 0, 0),
}

Punto = tuple[float, float]

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


def _color(hex_: str) -> tuple[int, int, int, int]:
    h = hex_.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"se espera un color #RRGGBB: {hex_!r}")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def render_svg(svg: Path = SVG, lado: int = LADO) -> Image.Image:
    """Dibuja el SVG del ícono en un cuadrado de ``lado`` px."""
    raiz = ET.parse(svg).getroot()
    vx, vy, vw, vh = (float(v) for v in raiz.attrib["viewBox"].split())
    escala_x, escala_y = lado / vw, lado / vh
    img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    for elemento in raiz.iter("{http://www.w3.org/2000/svg}path"):
        # Regla par-impar: los huecos de las letras (la "e") quedan vacíos.
        mascara = Image.new("1", (lado, lado), 0)
        for poligono in subpaths(elemento.attrib["d"]):
            capa = Image.new("1", (lado, lado), 0)
            ImageDraw.Draw(capa).polygon(
                [((x - vx) * escala_x, (y - vy) * escala_y) for x, y in poligono],
                fill=1,
            )
            mascara = ImageChops.logical_xor(mascara, capa)
        relleno = Image.new("RGBA", (lado, lado), _color(elemento.attrib["fill"]))
        img.paste(relleno, (0, 0), mascara)
    return img


def icono_16(mapa: Path = MAPA_16) -> Image.Image:
    """El 16 px dibujado a mano: una fila por línea, ``#`` comenta."""
    filas = [
        linea.strip()
        for linea in mapa.read_text(encoding="utf-8").splitlines()
        if linea.strip() and not linea.lstrip().startswith("#")
    ]
    if len(filas) != 16 or any(len(f) != 16 for f in filas):
        raise ValueError(f"{mapa.name}: se esperan 16 filas de 16 píxeles")
    img = Image.new("RGBA", (16, 16))
    for y, fila in enumerate(filas):
        for x, letra in enumerate(fila):
            img.putpixel((x, y), COLORES_16[letra])
    return img


def frames() -> dict[int, Image.Image]:
    """Una imagen por tamaño del .ico, ya en su tamaño final."""
    grande = render_svg()
    imagenes = {t: grande.resize((t, t), Image.Resampling.LANCZOS) for t in TAMANIOS}
    imagenes[16] = icono_16()
    return imagenes


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    salida = Path(argv[0])
    salida.parent.mkdir(parents=True, exist_ok=True)
    imagenes = frames()
    # Cada tamaño va tal cual (sin que Pillow lo reescale desde el de 256).
    imagenes[256].save(
        salida,
        format="ICO",
        sizes=[(t, t) for t in TAMANIOS],
        append_images=[imagenes[t] for t in TAMANIOS if t != 256],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

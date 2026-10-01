"""Genera el ícono de FacturadorE (``facturadore.ico``) con Pillow.

Diseño propio y mínimo: una hoja de factura blanca con la esquina doblada y
una "E" azul, sobre un cuadrado redondeado con el azul marino de la app.
Se dibuja a 1024 px y se reduce a los tamaños del .ico, así que el archivo
binario no vive en el repo: lo arma el build.

Uso: ``uv run python packaging/windows/make_icon.py <salida.ico>``
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "facturador" / "pdf" / "fonts" / "LiberationSans-Bold.ttf"

MARINO = (16, 28, 61)
MARINO_2 = (27, 43, 94)
ACENTO = (36, 82, 224)
HOJA = (255, 255, 255)
DOBLEZ = (199, 212, 248)
LINEAS = (199, 212, 248)

LADO = 1024
TAMANIOS = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def dibujar() -> Image.Image:
    img = Image.new("RGBA", (LADO, LADO), (0, 0, 0, 0))

    # Fondo: degradé vertical marino, recortado a un cuadrado redondeado.
    fondo = Image.new("RGBA", (LADO, LADO))
    trazo = ImageDraw.Draw(fondo)
    for y in range(LADO):
        t = y / (LADO - 1)
        color = tuple(
            round(a + (b - a) * t) for a, b in zip(MARINO_2, MARINO, strict=True)
        )
        trazo.line([(0, y), (LADO, y)], fill=color)
    mascara = Image.new("L", (LADO, LADO), 0)
    ImageDraw.Draw(mascara).rounded_rectangle(
        [0, 0, LADO - 1, LADO - 1], radius=220, fill=255
    )
    img.paste(fondo, (0, 0), mascara)

    d = ImageDraw.Draw(img)
    # Hoja con la esquina superior derecha doblada.
    x0, y0, x1, y1 = 250, 150, 774, 874
    doblez = 150
    d.polygon(
        [(x0, y0), (x1 - doblez, y0), (x1, y0 + doblez), (x1, y1), (x0, y1)],
        fill=HOJA,
    )
    d.polygon(
        [(x1 - doblez, y0), (x1 - doblez, y0 + doblez), (x1, y0 + doblez)],
        fill=DOBLEZ,
    )
    # Renglones de la factura al pie de la hoja.
    for i, ancho in enumerate((380, 300)):
        y = 720 + i * 62
        d.rounded_rectangle(
            [x0 + 72, y, x0 + 72 + ancho, y + 26], radius=13, fill=LINEAS
        )

    # La "E" (Factura E), centrada en la parte alta de la hoja.
    fuente = ImageFont.truetype(str(FONT), 420)
    caja = d.textbbox((0, 0), "E", font=fuente)
    ancho_e = caja[2] - caja[0]
    cx = x0 + (x1 - x0) / 2 - 50
    d.text(
        (cx - ancho_e / 2 - caja[0], 250 - caja[1]),
        "E",
        font=fuente,
        fill=ACENTO,
    )
    return img


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    salida = Path(argv[0])
    salida.parent.mkdir(parents=True, exist_ok=True)
    dibujar().save(salida, format="ICO", sizes=TAMANIOS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

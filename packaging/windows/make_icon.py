"""Genera el ícono de FacturadorE (``facturadore.ico``) con Pillow.

El diseño vive en ``facturador/web/static/brand/icon.svg`` (todo en curvas,
colores fijos). El SVG lo dibuja ``facturador.marca`` (el mismo renderer que
usa el launcher) a 1024 px y acá se reduce a los tamaños del .ico. El tamaño
de 16 px no sale del SVG: a esa escala "fe" se vuelve borroso, así que se
arma desde el mapa de píxeles dibujado a mano en ``icono_16.txt``. El
binario no vive en el repo: lo arma el build.

Uso: ``uv run python packaging/windows/make_icon.py <salida.ico>``
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from facturador.marca import ICONO_SVG, render_svg, subpaths

SVG = ICONO_SVG
MAPA_16 = Path(__file__).resolve().parent / "icono_16.txt"

LADO = 1024
TAMANIOS = [16, 24, 32, 48, 64, 128, 256]

COLORES_16 = {
    "V": (0x5B, 0x44, 0xBE, 255),
    "L": (0xB6, 0xA8, 0xF2, 255),
    "W": (255, 255, 255, 255),
    ".": (0, 0, 0, 0),
}

# El intérprete de paths vive en facturador.marca; se reexporta para los tests.
__all__ = ["COLORES_16", "MAPA_16", "TAMANIOS", "frames", "main", "subpaths"]


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
    grande = render_svg(SVG, LADO, LADO)
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

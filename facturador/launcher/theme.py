"""Tema de las ventanas del launcher: los colores de la app, en Tk.

Los colores no viven acá: se leen del bloque de tokens de
``web/static/app.css`` (claro en ``:root``, oscuro en el bloque
``prefers-color-scheme: dark``), así el launcher y la app no se separan. El
tema sigue al de Windows; si no se puede leer, o fuera de Windows, claro.

Tk no dibuja esquinas redondeadas ni bordes suavizados, así que tarjetas,
botones, píldoras y paneles se dibujan como imágenes con Pillow, más grandes
y reducidas, sobre el color de lo que tienen detrás.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ..marca import RGBA, color_hex

APP_CSS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.css"

# Los tokens de app.css que usa el launcher. Un test verifica que cada uno
# exista en los dos temas.
TOKENS: tuple[str, ...] = (
    "fondo",
    "superficie",
    "superficie-hover",
    "neutro-fondo",
    "borde",
    "borde-fuerte",
    "borde-control",
    "tinta",
    "tinta-suave",
    "acento",
    "acento-hover",
    "sobre-acento",
    "acento-tinte",
    "ok",
    "aviso",
    "aviso-fondo",
    "aviso-borde",
    "aviso-tinta",
    "error",
    "error-fondo",
    "error-borde",
    "error-tinta",
    "prod-fondo",
    "prod-texto",
)

_INICIO = "/* tokens:inicio */"
_FIN = "/* tokens:fin */"
_OSCURO = "@media (prefers-color-scheme: dark)"
_DECLARACION = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})\s*;")

# Aire alrededor de cada control para el anillo de foco (2 px + 1 de
# separación). Los textos se corren lo mismo para quedar alineados con el
# borde visible de los controles.
MARGEN = 3

# Sobremuestreo de las formas: se dibujan N veces más grandes y se reducen.
_SS = 4

# Registro de Windows: 0 = las apps usan el tema oscuro.
_CLAVE_TEMA = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_VALOR_TEMA = "AppsUseLightTheme"
# DwmSetWindowAttribute: barra de título oscura (Windows 10 20H1+ y 11).
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20

FUENTES_TEXTO = ("Segoe UI Variable Text", "Segoe UI")
FUENTES_MONO = ("Cascadia Mono", "Consolas")


class TemaNoDisponible(RuntimeError):
    """No se pudieron leer los colores de ``app.css``."""


def leer_tokens(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """Colores ``#RRGGBB`` del bloque de tokens: (claro, oscuro)."""
    if css.count(_INICIO) != 1 or css.count(_FIN) != 1:
        raise TemaNoDisponible("app.css sin el bloque de tokens")
    bloque = css[css.index(_INICIO) : css.index(_FIN)]
    if _OSCURO not in bloque:
        raise TemaNoDisponible("app.css sin el tema oscuro")
    claro, oscuro = bloque.split(_OSCURO, 1)
    return dict(_DECLARACION.findall(claro)), dict(_DECLARACION.findall(oscuro))


@dataclass(frozen=True)
class Paleta:
    """Los colores de un tema, por nombre de token (sin el ``--``)."""

    oscuro: bool
    colores: dict[str, RGBA]

    def pil(self, token: str) -> RGBA:
        """El color para Pillow."""
        return self.colores[token]

    def tk(self, token: str) -> str:
        """El color como lo entiende Tk."""
        r, g, b, _ = self.colores[token]
        return f"#{r:02x}{g:02x}{b:02x}"


def cargar_paleta(oscuro: bool, css: Path = APP_CSS) -> Paleta:
    """La paleta del tema pedido, con los tokens de ``TOKENS``."""
    try:
        texto = css.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemaNoDisponible(f"no se pudo leer {css.name}: {exc}") from exc
    claro, noche = leer_tokens(texto)
    fuente = noche if oscuro else claro
    faltan = [t for t in TOKENS if t not in fuente]
    if faltan:
        raise TemaNoDisponible(f"faltan tokens en app.css: {', '.join(faltan)}")
    return Paleta(oscuro=oscuro, colores={t: color_hex(fuente[t]) for t in TOKENS})


def sistema_en_oscuro() -> bool:
    """True si Windows tiene las apps en tema oscuro."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CLAVE_TEMA) as clave:
            valor, _ = winreg.QueryValueEx(clave, _VALOR_TEMA)
    except Exception:
        return False
    return valor == 0


def barra_de_titulo_oscura(root: Any) -> None:
    """Pide a Windows la barra de título oscura; si falla, se ignora."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        valor = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(valor),
            ctypes.sizeof(valor),
        )
    except Exception:
        return


def elegir_familia(disponibles: Iterable[str], preferidas: Iterable[str]) -> str | None:
    """La primera familia preferida que está instalada."""
    instaladas = {f.lower(): f for f in disponibles}
    for familia in preferidas:
        if familia.lower() in instaladas:
            return instaladas[familia.lower()]
    return None


# ---- formas dibujadas con Pillow ----


def _caja_ss(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    ancho: float,
    alto: float,
    radio: float,
    color: RGBA,
) -> None:
    """Rectángulo redondeado en coordenadas ya sobremuestreadas."""
    if ancho <= 0 or alto <= 0:
        return
    radio = max(0.0, min(radio, ancho / 2, alto / 2))
    draw.rounded_rectangle(
        (x, y, x + ancho - 1, y + alto - 1), radius=radio, fill=color
    )


def caja(
    ancho: int,
    alto: int,
    *,
    fondo: RGBA,
    relleno: RGBA | None,
    radio: float,
    borde: RGBA | None = None,
    grosor: int = 1,
    margen: int = 0,
    anillo: RGBA | None = None,
    sombra: RGBA | None = None,
) -> Image.Image:
    """Una caja redondeada de ``ancho`` × ``alto`` dentro de ``margen`` px de
    ``fondo`` por lado.

    ``anillo`` es el foco: 2 px, separado 1 px de la caja (necesita
    ``margen`` ≥ 3). ``sombra`` es el anillo de hover: 2 px pegado a la caja.
    ``relleno=None`` deja ver el fondo (botón de texto).
    """
    total_w, total_h = ancho + 2 * margen, alto + 2 * margen
    img = Image.new("RGBA", (total_w * _SS, total_h * _SS), fondo)
    draw = ImageDraw.Draw(img)
    x = y = margen * _SS
    w, h, r = ancho * _SS, alto * _SS, radio * _SS
    if anillo is not None:
        d = 3 * _SS
        _caja_ss(draw, x - d, y - d, w + 2 * d, h + 2 * d, r + d, anillo)
        d = 1 * _SS
        _caja_ss(draw, x - d, y - d, w + 2 * d, h + 2 * d, r + d, fondo)
    if sombra is not None:
        d = 2 * _SS
        _caja_ss(draw, x - d, y - d, w + 2 * d, h + 2 * d, r + d, sombra)
    if relleno is not None:
        if borde is not None:
            _caja_ss(draw, x, y, w, h, r, borde)
            g = grosor * _SS
            _caja_ss(draw, x + g, y + g, w - 2 * g, h - 2 * g, max(0, r - g), relleno)
        else:
            _caja_ss(draw, x, y, w, h, r, relleno)
    return img.resize((total_w, total_h), Image.Resampling.LANCZOS)


def circulo(img: Image.Image, x: int, y: int, diametro: int, color: RGBA) -> None:
    """Pega un círculo suavizado con la esquina superior izquierda en (x, y)."""
    grande = Image.new("L", (diametro * _SS, diametro * _SS), 0)
    ImageDraw.Draw(grande).ellipse(
        (0, 0, diametro * _SS - 1, diametro * _SS - 1), fill=255
    )
    mascara = grande.resize((diametro, diametro), Image.Resampling.LANCZOS)
    img.paste(Image.new("RGBA", (diametro, diametro), color), (x, y), mascara)


def icono_error(lado: int, *, color: RGBA, fondo: RGBA) -> Image.Image:
    """Círculo con signo de exclamación (trazo, como los íconos de la app)."""
    s = lado * _SS / 24  # el ícono se diseña en una grilla de 24
    img = Image.new("RGBA", (lado * _SS, lado * _SS), fondo)
    draw = ImageDraw.Draw(img)
    trazo = max(1, round(2 * s))
    draw.ellipse((2 * s, 2 * s, 22 * s, 22 * s), outline=color, width=trazo)
    draw.line((12 * s, 8 * s, 12 * s, 12.5 * s), fill=color, width=trazo)
    r = trazo / 2 + 0.2 * s
    draw.ellipse((12 * s - r, 16 * s - r, 12 * s + r, 16 * s + r), fill=color)
    return img.resize((lado, lado), Image.Resampling.LANCZOS)


def icono_check(lado: int, *, color: RGBA, fondo: RGBA) -> Image.Image:
    """Tilde de paso cumplido (trazo, como los íconos de la app)."""
    s = lado * _SS / 24  # grilla de 24
    img = Image.new("RGBA", (lado * _SS, lado * _SS), fondo)
    draw = ImageDraw.Draw(img)
    trazo = max(1, round(3 * s))
    puntos = [(4 * s, 12 * s), (9 * s, 17 * s), (20 * s, 6 * s)]
    draw.line(puntos, fill=color, width=trazo, joint="curve")
    r = trazo / 2
    for x, y in (puntos[0], puntos[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
    return img.resize((lado, lado), Image.Resampling.LANCZOS)

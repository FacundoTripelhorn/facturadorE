"""Ícono de la app: ``packaging/windows/make_icon.py`` arma el .ico desde
``static/brand/icon.svg`` y el 16 px desde el mapa dibujado a mano."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "make_icon", ROOT / "packaging" / "windows" / "make_icon.py"
)
assert _spec and _spec.loader
make_icon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_icon)

VIOLETA = (0x5B, 0x44, 0xBE, 255)
LILA = (0xB6, 0xA8, 0xF2, 255)
BLANCO = (255, 255, 255, 255)


@pytest.fixture(scope="module")
def ico(tmp_path_factory) -> Image.Image:
    salida = tmp_path_factory.mktemp("icono") / "facturadore.ico"
    assert make_icon.main([str(salida)]) == 0
    return Image.open(salida)


def _frame(ico: Image.Image, lado: int) -> Image.Image:
    return ico.ico.getimage((lado, lado)).convert("RGBA")


def test_ico_trae_todos_los_tamanios(ico):
    assert sorted(ico.info["sizes"]) == [(t, t) for t in make_icon.TAMANIOS]


def test_16_px_coincide_pixel_a_pixel_con_el_mapa(ico):
    filas = [
        linea.strip()
        for linea in make_icon.MAPA_16.read_text(encoding="utf-8").splitlines()
        if linea.strip() and not linea.startswith("#")
    ]
    frame = _frame(ico, 16)
    for y, fila in enumerate(filas):
        for x, letra in enumerate(fila):
            assert frame.getpixel((x, y)) == make_icon.COLORES_16[letra], (x, y)


@pytest.mark.parametrize(
    ("punto_svg", "esperado"),
    [
        ((8, 40), VIOLETA),  # la hoja
        ((48, 10), LILA),  # la esquina doblada
        ((28, 40), BLANCO),  # el trazo izquierdo de la "e"
        ((37, 36), VIOLETA),  # el hueco de la "e" (regla par-impar)
        ((19, 44), BLANCO),  # el asta de la "f"
    ],
)
def test_256_px_sale_del_svg(ico, punto_svg, esperado):
    # El viewBox es de 64 unidades: a 256 px, 4 px por unidad.
    x, y = punto_svg
    assert _frame(ico, 256).getpixel((x * 4, y * 4)) == esperado


def test_esquinas_redondeadas_transparentes(ico):
    frame = _frame(ico, 256)
    for punto in ((1, 1), (1, 254), (254, 254)):
        assert frame.getpixel(punto)[3] == 0, punto


def test_path_rechaza_comandos_no_soportados():
    with pytest.raises(ValueError, match="no soportado"):
        make_icon.subpaths("M0 0 C1 1 2 2 3 3 Z")
    with pytest.raises(ValueError, match="no soportado"):
        make_icon.subpaths("m0 0 l1 1 z")


def test_path_aplana_arcos_y_cierra_subpaths():
    (cuadrado,) = make_icon.subpaths("M0 0 H10 V10 H0 Z")
    assert cuadrado == [(0, 0), (10, 0), (10, 10), (0, 10)]
    (arco,) = make_icon.subpaths("M0 10 A10 10 0 0 1 10 0 Z")
    # Cuarto de círculo de radio 10 centrado en (10, 10).
    for x, y in arco[1:]:
        assert abs((x - 10) ** 2 + (y - 10) ** 2 - 100) < 1e-6
    assert arco[-1] == pytest.approx((10, 0))

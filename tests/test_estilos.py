"""Guardia de estilos: todo el CSS vive en ``static/app.css``.

Las plantillas no traen ``<style>`` ni colores en atributos ``style=``, y en
``app.css`` los colores (hex, ``rgb()``/``rgba()``, ``hsl()``) solo aparecen
en el bloque de tokens, delimitado por ``/* tokens:inicio */`` y
``/* tokens:fin */``. El resto del archivo usa ``var(--…)``: un color nuevo
pasa primero por un token con nombre.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from facturador.web.routes import STATIC_DIR, TEMPLATES_DIR

APP_CSS = STATIC_DIR / "app.css"
MARCA_INICIO = "/* tokens:inicio */"
MARCA_FIN = "/* tokens:fin */"

_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(", re.IGNORECASE)
_STYLE_TAG = re.compile(r"<style\b", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_PROPIEDAD_COLOR = re.compile(
    r"\b(?:color|background|border|fill|stroke)", re.IGNORECASE
)


def _templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def _sin_comentarios(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _colores_en_css(css: str) -> list[str]:
    """Colores en las declaraciones (lo que va entre llaves), no en los
    selectores: ``#cae`` puede ser un id además de un hex."""
    bloques = re.findall(r"\{([^{}]*)\}", _sin_comentarios(css))
    return [m for bloque in bloques for m in _COLOR.findall(bloque)]


def test_hay_templates_y_app_css():
    # Si el glob o la ruta dejan de encontrar archivos, el resto pasaría vacío.
    assert _templates()
    assert APP_CSS.is_file()


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_template_sin_style(template: Path):
    texto = template.read_text(encoding="utf-8")
    assert not _STYLE_TAG.search(texto), f"{template.name}: mover el <style> a app.css"
    for _, valor in _STYLE_ATTR.findall(texto):
        assert not _COLOR.search(valor) and not _PROPIEDAD_COLOR.search(valor), (
            f"{template.name}: color en style={valor!r}; usar una clase de app.css"
        )


def _partes_de_app_css() -> tuple[str, str]:
    """(bloque de tokens, resto del archivo)."""
    css = APP_CSS.read_text(encoding="utf-8")
    assert css.count(MARCA_INICIO) == 1 and css.count(MARCA_FIN) == 1
    inicio = css.index(MARCA_INICIO)
    fin = css.index(MARCA_FIN)
    assert inicio < fin
    tokens = css[inicio + len(MARCA_INICIO) : fin]
    resto = css[:inicio] + css[fin + len(MARCA_FIN) :]
    return tokens, resto


def test_app_css_sin_colores_fuera_de_tokens():
    _, resto = _partes_de_app_css()
    sueltos = _colores_en_css(resto)
    assert not sueltos, f"colores fuera del bloque de tokens: {sueltos}"


def test_bloque_de_tokens_solo_declara_variables():
    tokens, _ = _partes_de_app_css()
    cuerpo = _sin_comentarios(tokens).strip()
    assert cuerpo.startswith(":root {") and cuerpo.endswith("}")
    declaraciones = [
        d.strip() for d in cuerpo[len(":root {") : -1].split(";") if d.strip()
    ]
    assert declaraciones
    no_variables = [d for d in declaraciones if not d.startswith("--")]
    assert not no_variables, f"en los tokens solo van variables: {no_variables}"


def test_tokens_usados_estan_definidos():
    tokens, resto = _partes_de_app_css()
    definidos = set(re.findall(r"(--[\w-]+)\s*:", tokens))
    usados = set(re.findall(r"var\((--[\w-]+)", resto + tokens))
    assert usados <= definidos, f"tokens sin definir: {sorted(usados - definidos)}"


def test_tokens_definidos_se_usan():
    tokens, resto = _partes_de_app_css()
    definidos = set(re.findall(r"(--[\w-]+)\s*:", tokens))
    usados = set(re.findall(r"var\((--[\w-]+)", resto + tokens))
    assert definidos <= usados, f"tokens sin uso: {sorted(definidos - usados)}"


def test_detector_de_colores():
    # El detector tiene que ver los colores sin confundir selectores de id.
    assert _colores_en_css("a { color: #fff; }") == ["#fff"]
    assert _colores_en_css("@media (x) { a { background: rgba(0, 0, 0, .5); } }")
    assert _colores_en_css("a { box-shadow: 0 1px 2px HSL(0 0% 0%); }")
    assert not _colores_en_css("#fecha_cbte, #cae { width: auto; }")
    assert not _colores_en_css("/* #fff */ a { color: var(--tinta); }")


def test_base_enlaza_app_css(api):
    r = api.get("/clientes")
    assert '<link rel="stylesheet" href="/static/app.css">' in r.text
    css = api.get("/static/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

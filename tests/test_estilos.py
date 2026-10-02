"""Guardia de estilos: todo el CSS vive en ``static/app.css``.

Las plantillas no traen ``<style>`` ni colores en atributos ``style=``, y en
``app.css`` los colores (hex, ``rgb()``/``rgba()``, ``hsl()``) solo aparecen
en el bloque de tokens, delimitado por ``/* tokens:inicio */`` y
``/* tokens:fin */``. El resto del archivo usa ``var(--…)``: un color nuevo
pasa primero por un token con nombre, definido en el tema claro y en el
oscuro, y los pares de texto/fondo que usa la UI cumplen contraste AA.
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


_TEMA_OSCURO = "@media (prefers-color-scheme: dark)"


def _declaraciones(bloque: str) -> dict[str, str]:
    """``{propiedad: valor}`` de un bloque ``:root { … }`` sin las llaves."""
    pares = [d.split(":", 1) for d in bloque.split(";") if d.strip()]
    assert all(len(p) == 2 for p in pares), f"declaración inválida en: {bloque!r}"
    return {k.strip(): v.strip() for k, v in pares}


def _temas() -> tuple[dict[str, str], dict[str, str]]:
    """Tokens del tema claro y del oscuro, como ``{--token: valor}``.

    Forma esperada del bloque: ``:root { … }`` y después
    ``@media (prefers-color-scheme: dark) { :root { … } }``, nada más."""
    tokens, _ = _partes_de_app_css()
    patron = (
        r"^:root\s*\{([^{}]*)\}\s*"
        + re.escape(_TEMA_OSCURO)
        + r"\s*\{\s*:root\s*\{([^{}]*)\}\s*\}$"
    )
    m = re.match(patron, _sin_comentarios(tokens).strip())
    assert m, "el bloque de tokens debe ser :root { … } y su versión oscura"
    return _declaraciones(m.group(1)), _declaraciones(m.group(2))


def test_bloque_de_tokens_solo_declara_variables():
    claro, oscuro = _temas()
    assert claro.pop("color-scheme", None) == "light dark"
    no_variables = [k for k in [*claro, *oscuro] if not k.startswith("--")]
    assert not no_variables, f"en los tokens solo van variables: {no_variables}"


def test_cada_color_se_define_en_ambos_temas():
    claro, oscuro = _temas()
    colores = {k for k, v in claro.items() if _COLOR.search(v)}
    assert colores
    assert set(oscuro) == colores, (
        f"faltan en oscuro: {sorted(colores - set(oscuro))}; "
        f"sobran en oscuro: {sorted(set(oscuro) - colores)}"
    )


def _luminancia(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    assert len(h) == 6, f"se espera #RRGGBB: {hex_color}"
    canales = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lineal = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in canales
    ]
    return 0.2126 * lineal[0] + 0.7152 * lineal[1] + 0.0722 * lineal[2]


def _contraste(a: str, b: str) -> float:
    la, lb = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# Texto (izquierda) sobre fondo (derecha) que la UI combina de verdad.
_FONDOS_NEUTROS = (
    "--fondo",
    "--superficie",
    "--superficie-sutil",
    "--superficie-hover",
    "--neutro-fondo",
)
_SEMANTICOS = ("ok", "aviso", "error")
_PARES_DE_TEXTO = [
    *[("--tinta", f) for f in (*_FONDOS_NEUTROS, "--input-fondo")],
    *[("--tinta-suave", f) for f in _FONDOS_NEUTROS],
    ("--acento", "--superficie"),
    ("--acento", "--fondo"),
    ("--acento", "--superficie-sutil"),
    ("--acento", "--acento-tinte"),
    ("--sobre-acento", "--acento"),
    ("--sobre-acento", "--acento-hover"),
    *[(f"--{s}", f"--{s}-fondo") for s in _SEMANTICOS],
    *[(f"--{s}-tinta", f"--{s}-fondo") for s in _SEMANTICOS],
    *[("--sobre-semantico", f"--{s}") for s in _SEMANTICOS],
    ("--sobre-semantico", "--tinta-suave"),
]


@pytest.mark.parametrize("tema", ["claro", "oscuro"])
@pytest.mark.parametrize(("texto", "fondo"), _PARES_DE_TEXTO)
def test_contraste_aa(tema: str, texto: str, fondo: str):
    claro, oscuro = _temas()
    valores = claro if tema == "claro" else {**claro, **oscuro}
    ratio = _contraste(valores[texto], valores[fondo])
    assert ratio >= 4.5, f"{tema}: {texto} sobre {fondo} = {ratio:.2f}:1 (< 4.5)"


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
    assert '<meta name="color-scheme" content="light dark">' in r.text
    css = api.get("/static/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

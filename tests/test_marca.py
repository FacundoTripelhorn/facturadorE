"""Marca en la UI: favicon, ícono y wordmark del header."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from facturador.web.routes import STATIC_DIR, TEMPLATES_DIR

SVG_NS = "{http://www.w3.org/2000/svg}"
WORDMARK_SVG = STATIC_DIR / "brand" / "wordmark.svg"
WORDMARK_PARTIAL = TEMPLATES_DIR / "_wordmark.html"


def _paths(svg_xml: str) -> list[tuple[str | None, str]]:
    raiz = ET.fromstring(svg_xml)
    return [(p.get("class"), p.attrib["d"]) for p in raiz.iter(f"{SVG_NS}path")]


def test_wordmark_en_linea_es_copia_fiel_del_svg():
    partial = WORDMARK_PARTIAL.read_text(encoding="utf-8")
    svg_en_linea = partial[partial.index("<svg") :]
    original = WORDMARK_SVG.read_text(encoding="utf-8")
    assert _paths(svg_en_linea) == _paths(original)
    viewbox = re.compile(r'viewBox="([^"]+)"')
    assert viewbox.search(svg_en_linea)[1] == viewbox.search(original)[1]
    # El trazo toma el color del texto; la E, el acento vía .wordmark-e.
    assert [c for c, _ in _paths(svg_en_linea)] == [None, "wordmark-e"]
    assert 'role="img" aria-label="FacturadorE"' in svg_en_linea


def test_header_y_favicon_usan_la_marca(api):
    html = api.get("/clientes").text
    favicon = '<link rel="icon" type="image/svg+xml" href="/static/brand/icon.svg">'
    assert favicon in html
    marca = html[html.index('<span class="marca">') :]
    marca = marca[: marca.index("</svg>")]
    assert '<img src="/static/brand/icon.svg" width="28" height="28" alt="">' in marca
    assert '<svg class="wordmark"' in marca
    assert 'class="wordmark-e"' in marca


def test_assets_de_marca_se_sirven(api):
    for nombre in ("icon.svg", "wordmark.svg"):
        r = api.get(f"/static/brand/{nombre}")
        assert r.status_code == 200, nombre
        assert r.headers["content-type"].startswith("image/svg+xml")


def test_wordmark_toma_los_colores_del_tema():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert ".wordmark .wordmark-e { fill: var(--acento); }" in css
    assert re.search(r"\.wordmark \{[^}]*color: var\(--tinta\)", css)

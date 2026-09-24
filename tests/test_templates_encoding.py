"""Guardia de codificación de templates.

Un cambio anterior reescribió dos templates con doble codificación UTF-8 (mojibake):
bytes UTF-8 leídos como Latin-1/cp1252 y reguardados como UTF-8, dejando
`Configuración` → `ConfiguraciÃ³n`, `está` → `estÃ¡`, etc. El texto sigue
siendo UTF-8 válido, así que un decode no alcanza para detectarlo; hay que
buscar las secuencias características que el español rioplatense nunca usa.

Este test recorre todos los templates HTML del paquete y falla si aparece
alguno de esos marcadores, para que la regresión no vuelva a colarse en
silencio (el árbol renderiza estas páginas al usuario en onboarding/config).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "facturador"

# Secuencias que solo aparecen cuando UTF-8 se codifica dos veces. Ninguna
# es texto legítimo en los templates (español rioplatense + símbolos ★ —).
_MOJIBAKE_MARKERS = (
    "Ã",       # á/é/í/ó/ú/ñ mal codificados: Ã¡, Ã©, Ã³, Ã±, ...
    "Â",       # espacios/símbolos mal codificados: Â , Âº, ...
    "â€",      # comillas/guiones tipográficos: â€", â€œ, â€™
    "â˜",      # ★ mal codificado: â˜…
)


def _html_templates() -> list[Path]:
    return sorted(_TEMPLATES_ROOT.rglob("*.html"))


def test_hay_templates_para_revisar():
    # Red de seguridad: si el glob deja de encontrar templates el resto de
    # este archivo pasaría vacío y no guardaría nada.
    assert _html_templates(), "no se encontraron templates HTML para revisar"


@pytest.mark.parametrize("template", _html_templates(), ids=lambda p: p.name)
def test_template_sin_mojibake(template: Path):
    text = template.read_bytes().decode("utf-8")
    encontrados = [m for m in _MOJIBAKE_MARKERS if m in text]
    assert not encontrados, (
        f"{template.relative_to(_TEMPLATES_ROOT.parent)} contiene marcadores de "
        f"mojibake {encontrados}: UTF-8 doble-codificado "
        "Reguardá el archivo como UTF-8 limpio."
    )

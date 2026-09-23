"""Registry de renderers PDF versionados (FAC-53).

Cada factura guarda ``pdf_render_version`` en su snapshot inmutable
(FAC-52). El despacho elige el renderer registrado para esa versión;
una versión desconocida falla en claro (nunca cae al template más
nuevo).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

# Un renderer recibe el snapshot (factura + ítems) y devuelve los bytes del PDF.
PdfRenderer = Callable[[sqlite3.Row, list[sqlite3.Row]], bytes]

# Se llena al importar ``facturador.pdf.render`` (registro de v1).
PDF_RENDERERS: dict[int, PdfRenderer] = {}


def register_pdf_renderer(version: int, renderer: PdfRenderer) -> None:
    """Registra (o reemplaza) el renderer PDF de una versión."""
    if version < 1:
        raise ValueError(f"pdf_render_version inválida: {version}")
    PDF_RENDERERS[version] = renderer


def known_pdf_render_versions() -> list[int]:
    return sorted(PDF_RENDERERS)


def get_pdf_renderer(version: int) -> PdfRenderer:
    try:
        return PDF_RENDERERS[version]
    except KeyError as exc:
        known = ", ".join(str(v) for v in known_pdf_render_versions()) or "(ninguna)"
        raise ValueError(
            f"pdf_render_version={version} no tiene renderer registrado; "
            f"versiones conocidas: {known}."
        ) from exc

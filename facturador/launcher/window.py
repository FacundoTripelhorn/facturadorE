"""Ventana nativa del launcher vía pywebview (FAC-83).

Tras ``GET /health``, el launcher abre ``http://127.0.0.1:<port>/`` en una
ventana del webview del SO (WebView2 en Windows, WebKit en macOS) en lugar de
una pestaña genérica del navegador. Si pywebview no puede arrancar, se cae al
navegador del sistema con un mensaje claro en el log.

``webview.start()`` bloquea el hilo que lo invoca hasta que se cierra la
ventana (o ``interrupt_check`` fuerza ``destroy``). El caller (supervisor /
``__main__``) usa ese retorno para apagar el backend o atender un cambio de
ambiente (FAC-32).
"""

from __future__ import annotations

import logging
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.25
_DEFAULT_WIDTH = 1100
_DEFAULT_HEIGHT = 780


class UiEndReason(StrEnum):
    """Por qué terminó ``open_app_ui``."""

    CLOSED = "closed"
    """El usuario cerró la ventana nativa."""

    INTERRUPTED = "interrupted"
    """``interrupt_check`` pidió destruir la ventana (p.ej. cambio de ambiente)."""

    BROWSER_FALLBACK = "browser"
    """No hubo webview; se abrió (o intentó abrir) el navegador del sistema."""


@dataclass(frozen=True)
class UiOpenResult:
    reason: UiEndReason


def app_window_title(display_name: str) -> str:
    """Título de ventana: producto + cue de ambiente (Homologación / Producción)."""
    name = display_name.strip() or "FacturadorE"
    if name.lower().startswith("facturadore"):
        return name
    return f"FacturadorE — {name}"


def open_app_ui(
    url: str,
    *,
    title: str,
    interrupt_check: Callable[[], bool] | None = None,
    webview_module: Any | None = None,
    browser_opener: Callable[[str], Any] | None = None,
) -> UiOpenResult:
    """Abre la UI local en webview o, si falla, en el navegador del sistema.

    Args:
        url: Base URL del backend (típicamente ``http://127.0.0.1:<port>/``).
        title: Título de la ventana nativa.
        interrupt_check: Si devuelve True, se destruye la ventana y el
            resultado es ``INTERRUPTED`` (para FAC-32 sin dejar el GUI loop
            colgado).
        webview_module: Inyectable en tests; por defecto ``import webview``.
        browser_opener: Fallback; por defecto ``webbrowser.open``.
    """
    open_browser = browser_opener or webbrowser.open
    try:
        if webview_module is not None:
            webview = webview_module
        else:
            import webview as _webview

            webview = _webview
    except ImportError as exc:
        _log.warning(
            "pywebview no está disponible (%s); abriendo el navegador del sistema "
            "en %s",
            exc,
            url,
        )
        _open_system_browser(open_browser, url)
        return UiOpenResult(reason=UiEndReason.BROWSER_FALLBACK)

    interrupted = False
    try:
        window = webview.create_window(
            title,
            url,
            width=_DEFAULT_WIDTH,
            height=_DEFAULT_HEIGHT,
            min_size=(640, 480),
            text_select=True,
        )
        if window is None:
            raise RuntimeError("webview.create_window devolvió None")

        def _watch(win: Any) -> None:
            nonlocal interrupted
            if interrupt_check is None:
                return
            while True:
                try:
                    if interrupt_check():
                        interrupted = True
                        win.destroy()
                        return
                except Exception:
                    _log.debug(
                        "interrupt_check falló; se sigue con la ventana",
                        exc_info=True,
                    )
                time.sleep(_POLL_INTERVAL_S)

        if interrupt_check is not None:
            webview.start(_watch, window)
        else:
            webview.start()
    except Exception as exc:
        _log.warning(
            "No se pudo abrir la ventana nativa de FacturadorE (%s); "
            "abriendo el navegador del sistema en %s",
            exc,
            url,
            exc_info=True,
        )
        _open_system_browser(open_browser, url)
        return UiOpenResult(reason=UiEndReason.BROWSER_FALLBACK)

    if interrupted:
        return UiOpenResult(reason=UiEndReason.INTERRUPTED)
    return UiOpenResult(reason=UiEndReason.CLOSED)


def _open_system_browser(opener: Callable[[str], Any], url: str) -> None:
    try:
        opener(url)
    except Exception:
        _log.warning(
            "Tampoco se pudo abrir el navegador del sistema; la app ya está en %s",
            url,
            exc_info=True,
        )

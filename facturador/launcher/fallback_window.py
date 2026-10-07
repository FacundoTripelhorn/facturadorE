"""Aviso del launcher cuando la ventana propia de la app no abre.

La app sigue funcionando: se abre en el navegador del sistema. Antes de
abrirlo, esta ventana lo explica (panel de aviso, no de error) y ofrece:

- "Abrir en el navegador": cierra el aviso; el launcher abre el navegador.
- "Ver registro": abre ``launcher.log`` con el programa por defecto (solo si
  el archivo existe).
- "Copiar detalle técnico": el motivo, el ambiente y la versión de la app.
- "Ver ayuda": abre en el navegador la sección de ayuda del README.

Esc o la X equivalen a "Abrir en el navegador": cerrar el aviso sin abrir
nada dejaría la app andando sin ninguna ventana. Con terminal, bajo pytest o
con ``FACTURADOR_NO_DIALOGS`` no se abre nada: el motivo ya quedó en el log.
"""

from __future__ import annotations

import logging
import os
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..constants import ArcaEnvironment
from .failure_window import dialogs_enabled, technical_detail
from .frozen import launcher_log_path
from .theme import MARGEN

_log = logging.getLogger(__name__)

HELP_URL = (
    "https://github.com/FacundoTripelhorn/facturadorE/blob/master/README.md"
    "#la-ventana-no-abre-y-se-abre-el-navegador"
)
_TITULO = "No se pudo abrir la ventana de FacturadorE"
_TEXTO = "La app funciona igual: se va a abrir en el navegador."
_ABRIR = "Abrir en el navegador"
_REGISTRO = "Ver registro"
_AYUDA = "Ver ayuda"
_COPIAR = "Copiar detalle técnico"
_COPIADO = "Detalle copiado"
_COPIADO_MS = 2000


def prompt_browser_fallback(message: str, environment: ArcaEnvironment) -> None:
    """Muestra el aviso si corresponde y vuelve cuando el usuario lo acepta."""
    if not dialogs_enabled():
        return
    from .widgets import cerrar_ventana

    try:
        show_browser_fallback_window(
            message, environment=environment, log_path=launcher_log_path()
        )
    except Exception:
        # Sin Tk (o sin pantalla) el motivo ya quedó en launcher.log.
        _log.debug("no se pudo mostrar el aviso de navegador", exc_info=True)
    finally:
        # La app sigue en el navegador: el launcher no deja ventana abierta.
        cerrar_ventana()


def open_with_default_program(path: Path) -> None:
    """Abre un archivo con el programa por defecto del sistema."""
    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        startfile(str(path))
    else:
        webbrowser.open(path.as_uri())


def show_browser_fallback_window(
    message: str,
    *,
    environment: ArcaEnvironment,
    log_path: Path | None = None,
    open_file: Callable[[Path], Any] = open_with_default_program,
    open_url: Callable[[str], Any] = webbrowser.open,
) -> None:
    """La ventana de aviso; bloquea hasta "Abrir en el navegador"."""
    from .widgets import Boton, panel_aviso, ventana

    v = ventana()

    def _intentar(accion: Callable[[], Any], que: str) -> None:
        try:
            accion()
        except Exception:
            _log.debug("no se pudo %s", que, exc_info=True)

    def _copiar() -> None:
        try:
            v.root.clipboard_clear()
            v.root.clipboard_append(technical_detail(message, environment))
            v.root.update()
        except Exception:
            _log.debug("no se pudo copiar al portapapeles", exc_info=True)
            return
        copiar.cambiar_texto(_COPIADO)
        v.despues(_COPIADO_MS, lambda: copiar.cambiar_texto(_COPIAR))

    margen = v.px(MARGEN)
    panel_aviso(v, v.contenido, _TEXTO, titulo=_TITULO, con_icono=True).pack(
        side="top", anchor="w", padx=margen
    )
    copiar = Boton(v, v.contenido, _COPIAR, variante="enlace", command=_copiar)
    copiar.pack(side="top", anchor="w", pady=(v.px(12 - 2 * MARGEN), 0))
    Boton(
        v,
        v.contenido,
        _AYUDA,
        variante="enlace",
        command=lambda: _intentar(lambda: open_url(HELP_URL), "abrir la ayuda"),
    ).pack(side="top", anchor="w")

    abrir = Boton(v, v.pie, _ABRIR, variante="primario", command=v.terminar)
    abrir.pack(side="right")
    if log_path is not None:
        registro = log_path
        Boton(
            v,
            v.pie,
            _REGISTRO,
            command=lambda: _intentar(
                lambda: open_file(registro), "abrir el registro"
            ),
        ).pack(side="right", padx=(0, v.px(8 - 2 * MARGEN)))

    v.tecla("<Escape>", lambda _e: v.terminar())
    v.root.protocol("WM_DELETE_WINDOW", v.terminar)
    v.foco_inicial = abrir
    v.ejecutar()

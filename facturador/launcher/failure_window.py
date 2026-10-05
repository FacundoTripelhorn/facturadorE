"""Ventana de error del launcher (reemplaza al ``messagebox`` de Windows).

Muestra el mensaje tal cual (el texto de ``LauncherError`` no cambia) en un
panel de error y ofrece:

- "Reintentar": vuelve a arrancar el mismo ambiente.
- "Elegir otro ambiente": vuelve al selector (solo si se llegó desde el
  selector; con ``--env`` es "Cerrar").
- "Copiar detalle técnico": el mensaje, el ambiente y la versión de la app.

Esc o la X equivalen a "Elegir otro ambiente" (o "Cerrar"). Con terminal,
bajo pytest o con ``FACTURADOR_NO_DIALOGS`` no se abre nada: el error ya
salió por stderr.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from importlib import metadata

from ..constants import ArcaEnvironment
from .frozen import has_interactive_terminal
from .theme import MARGEN

_log = logging.getLogger(__name__)

_TITULOS = {
    ArcaEnvironment.HOMO: "Homologación",
    ArcaEnvironment.PROD: "Producción",
}
_COPIAR = "Copiar detalle técnico"
_COPIADO = "Detalle copiado"
_COPIADO_MS = 2000


class FailureAction(StrEnum):
    """Qué eligió el usuario en la ventana de error."""

    RETRY = "retry"
    CHOOSE = "choose"
    CLOSE = "close"


def dialogs_enabled() -> bool:
    """False con terminal, bajo pytest o con ``FACTURADOR_NO_DIALOGS``: un
    diálogo modal sin nadie que lo cierre colgaría el proceso (smoke del exe
    en CI)."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get(
        "FACTURADOR_NO_DIALOGS"
    ):
        return False
    return not has_interactive_terminal()


def app_version() -> str:
    try:
        return metadata.version("facturador")
    except metadata.PackageNotFoundError:
        return "desconocida"


def technical_detail(message: str, environment: ArcaEnvironment | None) -> str:
    """Lo que copia "Copiar detalle técnico"."""
    ambiente = _TITULOS[environment] if environment is not None else "sin elegir"
    return (
        f"FacturadorE {app_version()}\n"
        f"Ambiente: {ambiente}\n"
        f"Error: {message}\n"
    )


def prompt_failure(
    message: str,
    *,
    environment: ArcaEnvironment | None = None,
    can_choose: bool = False,
) -> FailureAction | None:
    """Muestra la ventana si corresponde; ``None`` si no se mostró."""
    if not dialogs_enabled():
        return None
    try:
        return show_failure_window(
            message, environment=environment, can_choose=can_choose
        )
    except Exception:
        # Sin Tk (o sin pantalla) el error ya quedó en stderr / launcher.log.
        _log.debug("no se pudo mostrar la ventana de error", exc_info=True)
        return None


def show_failure_window(
    message: str,
    *,
    environment: ArcaEnvironment | None,
    can_choose: bool,
) -> FailureAction:
    """La ventana de error; bloquea hasta que el usuario elige."""
    from .widgets import Boton, Ventana, panel_error, pildora

    v = Ventana()
    salida = FailureAction.CHOOSE if can_choose else FailureAction.CLOSE
    accion: list[FailureAction] = [salida]

    def _terminar(elegida: FailureAction) -> None:
        accion[0] = elegida
        v.cerrar()

    def _copiar() -> None:
        try:
            v.root.clipboard_clear()
            v.root.clipboard_append(technical_detail(message, environment))
            v.root.update()
        except Exception:
            _log.debug("no se pudo copiar al portapapeles", exc_info=True)
            return
        copiar.cambiar_texto(_COPIADO)
        v.root.after(_COPIADO_MS, lambda: copiar.cambiar_texto(_COPIAR))

    margen = v.px(MARGEN)
    if environment is not None:
        titulo = f"No se pudo abrir {_TITULOS[environment]}"
        pildora(v, v.contenido, environment, _TITULOS[environment]).pack(
            side="top", anchor="w", padx=margen, pady=(0, v.px(12))
        )
    else:
        titulo = "No se pudo iniciar FacturadorE"
    panel_error(v, v.contenido, message, titulo=titulo, con_icono=True).pack(
        side="top", anchor="w", padx=margen
    )
    copiar = Boton(v, v.contenido, _COPIAR, variante="enlace", command=_copiar)
    copiar.pack(side="top", anchor="w", pady=(v.px(12 - 2 * MARGEN), 0))

    secundario = Boton(
        v,
        v.pie,
        "Elegir otro ambiente" if can_choose else "Cerrar",
        command=lambda: _terminar(salida),
    )
    botones = [secundario]
    if environment is not None:
        reintentar = Boton(
            v,
            v.pie,
            "Reintentar",
            variante="primario",
            command=lambda: _terminar(FailureAction.RETRY),
        )
        botones.append(reintentar)
    for i, boton in enumerate(reversed(botones)):
        boton.pack(side="right", padx=(0, 0 if i == 0 else v.px(8 - 2 * MARGEN)))

    v.root.bind("<Escape>", lambda _e: _terminar(salida))
    v.root.protocol("WM_DELETE_WINDOW", lambda: _terminar(salida))
    v.foco_inicial = botones[-1]
    v.ejecutar()
    return accion[0]

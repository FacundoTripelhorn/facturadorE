"""Diálogo de primer uso de Producción en el launcher.

La persistencia del ack vive en ``facturador.production_ack`` (también la
usa el backend/Docker). Acá solo está el flujo interactivo del launcher.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..constants import ArcaEnvironment
from ..production_ack import (
    is_production_acknowledged,
    save_production_ack,
)
from ..profile import EnvironmentProfile
from .chooser import ChooserUnavailable
from .frozen import stdin_is_tty
from .theme import MARGEN

_CONFIRM_TITLE = "Producción — confirmación"
_CONFIRM_CONSEQUENCES = (
    "Los comprobantes que autorices tendrán validez fiscal real y "
    "numeración definitiva en ARCA. Esta acción no se puede deshacer "
    "después de autorizar."
)
_CONFIRM_QUESTION = "¿Continuar con Producción?"
_CONFIRM_BODY = (
    "Estás por abrir el ambiente de Producción por primera vez.\n\n"
    f"{_CONFIRM_CONSEQUENCES}\n\n"
    f"{_CONFIRM_QUESTION}"
)
# Título de la ventana (el menú de texto usa _CONFIRM_BODY completo).
_CONFIRM_HEADING = "Estás por abrir Producción por primera vez"
_CONFIRM_OK = "Entiendo: abrir Producción"
_CONFIRM_CANCEL = "Cancelar"

# Re-export para callers del launcher / tests.
__all__ = [
    "confirm_production_first_use",
    "ensure_production_acknowledged",
    "is_production_acknowledged",
    "prompt_production_confirm_gui",
    "prompt_production_confirm_tty",
    "save_production_ack",
]


def ensure_production_acknowledged(
    environment: ArcaEnvironment,
    *,
    app_data_root: Path | None = None,
    confirm: Callable[[], bool] | None = None,
) -> bool:
    """Pide confirmación la primera vez que se abre Producción.

    Returns:
        True si se puede arrancar (homo, ack previo, o confirmación OK).
        False si el usuario canceló (volver al chooser).

    Raises:
        ChooserUnavailable: no hay UI/TTY para confirmar.
        ProfileError: raíz de perfil inválida.
        OSError: no se pudo persistir el ack tras confirmar.
    """
    if environment is not ArcaEnvironment.PROD:
        return True

    profile = EnvironmentProfile.resolve(
        environment, app_data_root=app_data_root
    )
    if is_production_acknowledged(profile.paths):
        return True

    # Bajo pytest, sin callback inyectado, no abrir diálogos ni tocar el
    # perfil real del usuario. Los tests pasan ``confirm=``.
    if confirm is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return True

    prompt = confirm or confirm_production_first_use
    if not prompt():
        return False

    save_production_ack(profile.paths)
    return True


def confirm_production_first_use(
    *,
    prompt_gui: Callable[[], bool] | None = None,
    prompt_tty: Callable[[], bool] | None = None,
    force_tty: bool = False,
) -> bool:
    """Diálogo de confirmación; False = cancelar / volver al chooser."""
    gui = prompt_gui or prompt_production_confirm_gui
    tty = prompt_tty or prompt_production_confirm_tty

    if force_tty:
        return tty()

    try:
        return gui()
    except ChooserUnavailable:
        if stdin_is_tty():
            return tty()
        raise


def prompt_production_confirm_tty(
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> bool:
    """Confirmación textual: sí/s/y o vacío/n/q para cancelar."""
    print_fn(_CONFIRM_TITLE)
    print_fn("")
    print_fn(_CONFIRM_BODY)
    print_fn("")
    print_fn("  s / sí para continuar. Enter o n para cancelar.")
    print_fn("")

    while True:
        try:
            raw = input_fn("Confirmar Producción: ").strip().lower()
        except EOFError:
            return False
        if raw in {"", "n", "no", "q", "quit", "salir", "cancelar"}:
            return False
        if raw in {"s", "si", "sí", "y", "yes", "ok"}:
            return True
        print_fn("Opción inválida. Probá de nuevo.")


def prompt_production_confirm_gui() -> bool:
    """Ventana de confirmación con el tema; cerrar o Esc = cancelar.

    El foco arranca en "Cancelar": un Enter apurado no abre Producción.
    """
    try:
        from .widgets import Boton, Ventana, panel_error, pildora

        ventana = Ventana()
    except Exception as exc:
        raise ChooserUnavailable(f"no se pudo abrir la ventana: {exc}") from exc

    v = ventana
    selection: list[bool] = [False]

    def _ok() -> None:
        selection[0] = True
        v.cerrar()

    def _cancel() -> None:
        selection[0] = False
        v.cerrar()

    margen = v.px(MARGEN)
    pildora(v, v.contenido, ArcaEnvironment.PROD, "Producción").pack(
        side="top", anchor="w", padx=margen
    )
    v.texto(v.contenido, _CONFIRM_HEADING, tamanio=17, negrita=True).pack(
        side="top", anchor="w", padx=margen, pady=(v.px(16), v.px(12))
    )
    panel_error(v, v.contenido, _CONFIRM_CONSEQUENCES).pack(
        side="top", anchor="w", padx=margen
    )
    v.texto(v.contenido, _CONFIRM_QUESTION, token="tinta-suave").pack(
        side="top", anchor="w", padx=margen, pady=(v.px(12), 0)
    )
    cancelar = Boton(v, v.pie, _CONFIRM_CANCEL, command=_cancel)
    confirmar = Boton(v, v.pie, _CONFIRM_OK, variante="peligro", command=_ok)
    confirmar.pack(side="right")
    cancelar.pack(side="right", padx=(0, v.px(8 - 2 * MARGEN)))

    v.root.bind("<Escape>", lambda _e: _cancel())
    v.root.protocol("WM_DELETE_WINDOW", _cancel)
    v.foco_inicial = cancelar
    v.ejecutar()
    return selection[0]

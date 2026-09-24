"""Diálogo de primer uso de Producción en el launcher.

La persistencia del ack vive en ``facturador.production_ack`` (también la
usa el backend/Docker). Acá solo está el flujo interactivo del launcher.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from ..constants import ArcaEnvironment
from ..production_ack import (
    is_production_acknowledged,
    save_production_ack,
)
from ..profile import EnvironmentProfile
from .chooser import ChooserUnavailable

_CONFIRM_TITLE = "Producción — confirmación"
_CONFIRM_BODY = (
    "Estás por abrir el ambiente de Producción por primera vez.\n\n"
    "Los comprobantes que autorices tendrán validez fiscal real y "
    "numeración definitiva en ARCA. Esta acción no se puede deshacer "
    "después de autorizar.\n\n"
    "¿Continuar con Producción?"
)
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
        if sys.stdin.isatty():
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
    """Ventana nativa de confirmación; cerrar = cancelar."""
    try:
        import tkinter
        from tkinter import ttk
    except Exception as exc:
        raise ChooserUnavailable("tkinter no disponible") from exc

    selection: list[bool] = [False]

    try:
        root = tkinter.Tk()
    except Exception as exc:
        raise ChooserUnavailable(f"no se pudo abrir la ventana: {exc}") from exc

    root.title("FacturadorE")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=20)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(
        frame,
        text=_CONFIRM_TITLE,
        font=("Segoe UI", 14, "bold"),
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
    ttk.Label(
        frame,
        text=_CONFIRM_BODY,
        wraplength=440,
        justify="left",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))

    def _ok() -> None:
        selection[0] = True
        root.destroy()

    def _cancel() -> None:
        selection[0] = False
        root.destroy()

    ttk.Button(frame, text=_CONFIRM_CANCEL, command=_cancel).grid(
        row=2, column=0, sticky="w"
    )
    ttk.Button(frame, text=_CONFIRM_OK, command=_ok).grid(
        row=2, column=1, sticky="e"
    )

    root.protocol("WM_DELETE_WINDOW", _cancel)
    root.update_idletasks()
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    root.geometry(
        f"+{(screen_w - width) // 2}+{(screen_h - height) // 2}"
    )
    root.mainloop()
    return selection[0]

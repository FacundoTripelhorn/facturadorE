"""Confirmación de primer uso de Producción (FAC-40).

La primera vez que se elige Producción, el launcher exige un ack explícito
sobre la validez fiscal real. El flag vive solo en el perfil ``prod``
(``data/production_ack.json``); Homologación nunca lo pide ni lo escribe.
Los relanzamientos normales con el ack presente no vuelven a mostrar el
diálogo.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..constants import ArcaEnvironment
from ..profile import EnvironmentProfile, ProfilePaths
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


def production_ack_path(paths: ProfilePaths) -> Path:
    """Path del ack bajo el perfil dado (solo se persiste en prod)."""
    return paths.production_ack


def is_production_acknowledged(paths: ProfilePaths) -> bool:
    """True si el perfil ya tiene un ack de primer uso de Producción."""
    path = production_ack_path(paths)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(raw, dict) and raw.get("acknowledged") is True


def save_production_ack(paths: ProfilePaths) -> None:
    """Persiste el ack solo en el perfil indicado (layout mínimo incluido)."""
    paths.ensure_layout()
    payload = {
        "acknowledged": True,
        "acknowledged_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path = production_ack_path(paths)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
    """
    if environment is not ArcaEnvironment.PROD:
        return True

    profile = EnvironmentProfile.resolve(
        environment, app_data_root=app_data_root
    )
    if is_production_acknowledged(profile.paths):
        return True

    # Bajo pytest, sin callback inyectado, no abrir diálogos ni tocar el
    # perfil real del usuario. Los tests de FAC-40 pasan ``confirm=``.
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

"""Chooser de ambiente del launcher (FAC-29, ADR 0001).

Pantalla previa al supervisor: Homologación o Producción en lenguaje de
negocio. No expone perfiles, directorios, Docker ni variables de entorno.
Tras un error de arranque el caller puede volver a mostrar el chooser.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..constants import ArcaEnvironment

# Títulos = EnvironmentProfile.display_name (Homologación / Producción).
_HOMO_DESCRIPTION = (
    "Ambiente de prueba seguro. Podés practicar y verificar el flujo "
    "sin emitir comprobantes con valor fiscal."
)
_PROD_DESCRIPTION = (
    "Emisión fiscal real. Los comprobantes que autorices tienen "
    "validez fiscal."
)


@dataclass(frozen=True)
class EnvironmentOption:
    """Opción visible del chooser: título + descripción de negocio."""

    environment: ArcaEnvironment
    title: str
    description: str


ENVIRONMENT_OPTIONS: tuple[EnvironmentOption, ...] = (
    EnvironmentOption(
        environment=ArcaEnvironment.HOMO,
        title="Homologación",
        description=_HOMO_DESCRIPTION,
    ),
    EnvironmentOption(
        environment=ArcaEnvironment.PROD,
        title="Producción",
        description=_PROD_DESCRIPTION,
    ),
)


def environment_options() -> tuple[EnvironmentOption, ...]:
    """Opciones del chooser (Homologación, Producción)."""
    return ENVIRONMENT_OPTIONS


def choose_environment(
    *,
    options: Sequence[EnvironmentOption] | None = None,
    prompt_gui: Callable[[Sequence[EnvironmentOption]], ArcaEnvironment | None]
    | None = None,
    prompt_tty: Callable[[Sequence[EnvironmentOption]], ArcaEnvironment | None]
    | None = None,
    force_tty: bool = False,
) -> ArcaEnvironment | None:
    """Pide Homologación o Producción; ``None`` si el usuario cancela.

    La pantalla gráfica es el camino principal; el menú TTY es respaldo
    cuando no hay toolkit/display (o con ``force_tty=True`` en tests).
    """
    choices = tuple(options) if options is not None else ENVIRONMENT_OPTIONS
    if not choices:
        raise ValueError("El chooser necesita al menos una opción de ambiente.")

    gui = prompt_gui or prompt_environment_gui
    tty = prompt_tty or prompt_environment_tty

    if force_tty:
        return tty(choices)

    try:
        return gui(choices)
    except ChooserUnavailable:
        if sys.stdin.isatty():
            return tty(choices)
        raise


class ChooserUnavailable(RuntimeError):
    """No hay toolkit gráfico usable (p.ej. tkinter ausente o sin display)."""


def prompt_environment_tty(
    options: Sequence[EnvironmentOption],
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> ArcaEnvironment | None:
    """Menú textual: 1/2 o vacío/q para cancelar."""
    print_fn("FacturadorE — elegí el ambiente")
    print_fn("")
    for index, option in enumerate(options, start=1):
        print_fn(f"  {index}) {option.title}")
        print_fn(f"     {option.description}")
        print_fn("")
    print_fn("  Enter o q para cancelar.")
    print_fn("")

    while True:
        try:
            raw = input_fn("Ambiente: ").strip().lower()
        except EOFError:
            return None
        if raw in {"", "q", "quit", "salir"}:
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1].environment
        for option in options:
            if raw in {option.environment.value, option.title.lower()}:
                return option.environment
        print_fn("Opción inválida. Probá de nuevo.")


def prompt_environment_gui(
    options: Sequence[EnvironmentOption],
) -> ArcaEnvironment | None:
    """Ventana nativa: dos opciones de negocio; cerrar cancela."""
    try:
        import tkinter
        from tkinter import ttk
    except Exception as exc:
        raise ChooserUnavailable("tkinter no disponible") from exc

    selection: list[ArcaEnvironment | None] = [None]

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
        text="FacturadorE",
        font=("Segoe UI", 16, "bold"),
    ).grid(row=0, column=0, sticky="w", pady=(0, 4))
    ttk.Label(
        frame,
        text="Elegí el ambiente",
        font=("Segoe UI", 11),
    ).grid(row=1, column=0, sticky="w", pady=(0, 16))

    def _select(environment: ArcaEnvironment) -> None:
        selection[0] = environment
        root.destroy()

    def _bind_select(environment: ArcaEnvironment) -> Callable[[], None]:
        def _on_click() -> None:
            _select(environment)

        return _on_click

    for row, option in enumerate(options, start=2):
        card = ttk.Frame(frame, padding=12, relief="solid", borderwidth=1)
        card.grid(row=row, column=0, sticky="ew", pady=6)
        ttk.Label(
            card,
            text=option.title,
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            card,
            text=option.description,
            wraplength=420,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 10))
        ttk.Button(
            card,
            text=f"Abrir {option.title}",
            command=_bind_select(option.environment),
        ).grid(row=2, column=0, sticky="w")

    ttk.Button(frame, text="Cancelar", command=root.destroy).grid(
        row=2 + len(options), column=0, sticky="e", pady=(12, 0)
    )

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    # Centrar de forma aproximada sin exponer paths ni metadatos internos.
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

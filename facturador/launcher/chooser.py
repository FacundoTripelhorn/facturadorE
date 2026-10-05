"""Chooser de ambiente del launcher (ADR 0001).

Pantalla previa al supervisor: Homologación o Producción en lenguaje de
negocio. No expone perfiles, directorios, Docker ni variables de entorno.
Tras un error de arranque el caller puede volver a mostrar el chooser.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from ..constants import ArcaEnvironment
from .frozen import stdin_is_tty
from .theme import MARGEN

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
    current: ArcaEnvironment | None = None,
) -> ArcaEnvironment | None:
    """Pide Homologación o Producción; ``None`` si el usuario cancela.

    La pantalla gráfica es el camino principal; el menú TTY es respaldo
    cuando no hay toolkit/display (o con ``force_tty=True`` en tests).

    ``current`` es el ambiente en uso cuando el pedido viene de "Cambiar
    ambiente": la ventana preselecciona el otro. Si no, preselecciona el
    último que abrió bien (o Homologación).
    """
    choices = tuple(options) if options is not None else ENVIRONMENT_OPTIONS
    if not choices:
        raise ValueError("El chooser necesita al menos una opción de ambiente.")

    gui = prompt_gui or _default_gui(choices, current)
    tty = prompt_tty or prompt_environment_tty

    if force_tty:
        return tty(choices)

    try:
        return gui(choices)
    except ChooserUnavailable:
        if stdin_is_tty():
            return tty(choices)
        raise


def _default_gui(
    choices: Sequence[EnvironmentOption],
    current: ArcaEnvironment | None,
) -> Callable[[Sequence[EnvironmentOption]], ArcaEnvironment | None]:
    """La ventana con la preselección resuelta."""
    from .last_environment import read_last_environment

    environments = [o.environment for o in choices]
    if current is not None:
        others = [e for e in environments if e is not current]
        return partial(
            prompt_environment_gui,
            selected=others[0] if others else current,
            current=current,
        )
    last_used = read_last_environment()
    if last_used not in environments:
        last_used = None
    return partial(
        prompt_environment_gui,
        selected=last_used or ArcaEnvironment.HOMO,
        last_used=last_used,
    )


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
    *,
    selected: ArcaEnvironment | None = None,
    current: ArcaEnvironment | None = None,
    last_used: ArcaEnvironment | None = None,
) -> ArcaEnvironment | None:
    """Ventana del selector: tarjetas seleccionables y un botón "Abrir …".

    ``selected`` es la tarjeta preseleccionada (por defecto, la primera).
    En un cambio de ambiente, ``current`` marca el actual con "Actual"; si
    no, ``last_used`` marca el último que abrió bien con "Último usado".
    Click selecciona; doble click o Enter abre; ↑/↓ cambian la selección;
    Esc, "Cancelar" o la X cancelan.
    """
    try:
        from .widgets import PUNTO, Boton, Tarjeta, Ventana

        ventana = Ventana()
    except Exception as exc:
        raise ChooserUnavailable(f"no se pudo abrir la ventana: {exc}") from exc

    opciones = list(options)
    elegidas = [o.environment for o in opciones]
    indice = [elegidas.index(selected) if selected in elegidas else 0]
    resultado: list[ArcaEnvironment | None] = [None]
    v = ventana

    def _abrir() -> None:
        resultado[0] = opciones[indice[0]].environment
        v.cerrar()

    def _seleccionar(i: int) -> None:
        indice[0] = i
        for j, tarjeta in enumerate(tarjetas):
            tarjeta.seleccionar(j == i)
        abrir.cambiar_texto(f"Abrir {opciones[i].title}")

    def _etiqueta(environment: ArcaEnvironment) -> str | None:
        if current is not None:
            return "Actual" if environment is current else None
        return "Último usado" if environment is last_used else None

    v.texto(v.contenido, "Elegí el ambiente", tamanio=17, negrita=True).pack(
        side="top", anchor="w", padx=v.px(MARGEN), pady=(0, v.px(14 - MARGEN))
    )

    def _al_click(i: int) -> Callable[[], None]:
        return lambda: _seleccionar(i)

    def _al_doble_click(i: int) -> Callable[[], None]:
        def _abrir_esta() -> None:
            _seleccionar(i)
            _abrir()

        return _abrir_esta

    tarjetas: list[Tarjeta] = []
    for i, option in enumerate(opciones):
        tarjeta = Tarjeta(
            v,
            v.contenido,
            titulo=option.title,
            descripcion=option.description,
            punto=PUNTO[option.environment],
            etiqueta=_etiqueta(option.environment),
            command=_al_click(i),
            al_abrir=_al_doble_click(i),
        )
        tarjeta.pack(side="top", anchor="w", pady=(0, v.px(10 - 2 * MARGEN)))
        tarjetas.append(tarjeta)

    # Creados en el orden de Tab: tarjetas, Cancelar, Abrir. Los botones se
    # empaquetan antes que la ayuda: si falta lugar, se recorta la ayuda.
    cancelar = Boton(v, v.pie, "Cancelar", command=v.cerrar)
    abrir = Boton(v, v.pie, "Abrir", variante="primario", command=_abrir)
    abrir.pack(side="right")
    cancelar.pack(side="right", padx=(0, v.px(8 - 2 * MARGEN)))
    v.texto(
        v.pie, "↑↓ cambia · Enter abre", tamanio=11, token="tinta-suave", mono=True
    ).pack(side="left", padx=v.px(MARGEN))

    def _mover(paso: int) -> str:
        nuevo = max(0, min(len(tarjetas) - 1, indice[0] + paso))
        _seleccionar(nuevo)
        tarjetas[nuevo].focus_set()
        return "break"

    v.root.bind("<Up>", lambda _e: _mover(-1))
    v.root.bind("<Down>", lambda _e: _mover(1))
    # Enter sobre un botón lo activa el botón; en el resto, abre.
    v.root.bind("<Return>", lambda _e: _abrir())
    v.root.bind("<KP_Enter>", lambda _e: _abrir())
    v.root.bind("<Escape>", lambda _e: v.cerrar())
    v.root.protocol("WM_DELETE_WINDOW", v.cerrar)

    _seleccionar(indice[0])
    v.foco_inicial = tarjetas[indice[0]]
    v.ejecutar()
    return resultado[0]

"""CLI del launcher: ``python -m facturador.launcher [--env homo|prod]``.

Sin ``--env`` muestra el chooser (FAC-29): Homologación o Producción en
lenguaje de negocio. Con ``--env`` arranca ese ambiente de una vez (tests /
automatización). El supervisor (FAC-28) arranca el backend del perfil oculto,
espera readiness, abre el browser y permanece en primer plano hasta Ctrl+C.
Si el perfil ya tiene una sesión sana (FAC-30), reabre el browser y sale sin
duplicar el backend. Un error de arranque desde el chooser vuelve a la
pantalla de elección.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable

from ..constants import DEFAULT_PORT, ArcaEnvironment
from ..profile import ProfileError
from .chooser import ChooserUnavailable, choose_environment
from .command import resolve_launch_environment
from .supervisor import LauncherError, ProcessSupervisor

# Inyectable en tests: reemplaza la pantalla/menú del chooser.
_ChooseFn = Callable[..., ArcaEnvironment | None]


def _parse_port(value: str) -> int:
    """Puerto TCP 1–65535; falla con mensaje de argparse (sin traceback)."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"puerto inválido: {value!r}"
        ) from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(
            f"puerto fuera de rango (1-65535): {port}"
        )
    return port


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m facturador.launcher",
        description=(
            "Launcher de FacturadorE: elegí Homologación o Producción, "
            "arranca el backend, espera /health y abre el navegador."
        ),
    )
    parser.add_argument(
        "--env",
        required=False,
        default=None,
        choices=[e.value for e in ArcaEnvironment],
        help=(
            "Ambiente a iniciar sin mostrar el chooser: "
            "homo (Homologación) o prod (Producción)."
        ),
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=DEFAULT_PORT,
        help=f"Puerto local del backend (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="No abrir el navegador al quedar listo.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Segundos máximos esperando readiness (default: 60).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    choose: _ChooseFn | None = None,
    supervisor_factory: Callable[..., ProcessSupervisor] | None = None,
    report_failure: Callable[[str], None] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    factory = supervisor_factory or ProcessSupervisor
    chooser = choose or choose_environment
    fail = report_failure or _report_failure

    if args.env is not None:
        try:
            environment = resolve_launch_environment(args.env)
        except ProfileError as exc:
            fail(str(exc))
            return 2
        code = _run_session(
            environment, args, factory=factory, report_failure=fail
        )
        # Sin chooser: _run_session nunca pide reintento (None).
        return 0 if code is None else code

    # Chooser interactivo: un error de arranque vuelve a la elección.
    while True:
        try:
            selected = chooser()
        except ChooserUnavailable as exc:
            fail(
                "No se pudo mostrar el selector de ambiente. "
                f"{exc}"
            )
            return 2
        if selected is None:
            return 0
        code = _run_session(
            selected,
            args,
            factory=factory,
            report_failure=fail,
            return_to_chooser_on_startup_error=True,
        )
        if code is None:
            continue
        return code


def _run_session(
    environment: ArcaEnvironment,
    args: argparse.Namespace,
    *,
    factory: Callable[..., ProcessSupervisor],
    report_failure: Callable[[str], None],
    return_to_chooser_on_startup_error: bool = False,
) -> int | None:
    """Supervisa una sesión. ``None`` = volver al chooser tras fallo de start."""
    supervisor = factory(
        environment=environment,
        port=args.port,
        open_browser=not args.no_browser,
        readiness_timeout=args.timeout,
    )
    try:
        result = supervisor.start()
    except LauncherError as exc:
        report_failure(str(exc))
        return None if return_to_chooser_on_startup_error else 1
    except ProfileError as exc:
        # Cinturón por si el plan falla fuera del parser (p.ej. puerto vía API).
        report_failure(str(exc))
        return None if return_to_chooser_on_startup_error else 2
    except KeyboardInterrupt:
        # start() ya apagó el hijo; no dejar traceback al usuario.
        print("\nDeteniendo…", flush=True)
        return 0

    plan = result.plan
    if result.reused:
        print(
            f"FacturadorE ({plan.profile.display_name}) ya estaba en marcha "
            f"en {plan.base_url}/; se reabrió la sesión existente.",
            flush=True,
        )
        return 0

    print(
        f"FacturadorE ({plan.profile.display_name}) listo en {plan.base_url}/",
        flush=True,
    )
    print("Ctrl+C para detener.", flush=True)
    try:
        while supervisor.is_running:
            assert supervisor.process is not None
            try:
                supervisor.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
        code = (
            supervisor.process.returncode
            if supervisor.process is not None
            else 1
        )
        report_failure(
            f"El backend de {plan.profile.display_name} se detuvo solo "
            f"(código {code})."
        )
        # Caída post-arranque: desde el chooser se puede elegir de nuevo.
        return None if return_to_chooser_on_startup_error else 1
    except KeyboardInterrupt:
        print("\nDeteniendo…", flush=True)
        supervisor.stop()
        return 0


def _report_failure(message: str) -> None:
    """Falla visible: stderr siempre; diálogo nativo si no hay TTY.

    Sin diálogo bajo pytest (``PYTEST_CURRENT_TEST``) para no bloquear la suite
    con un messagebox modal en el display del agente.
    """
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if sys.stdin.isatty() and sys.stderr.isatty():
        return
    try:
        import tkinter
        from tkinter import messagebox
    except Exception:
        return
    try:
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("FacturadorE", message)
        root.destroy()
    except Exception:
        return


if __name__ == "__main__":
    raise SystemExit(main())

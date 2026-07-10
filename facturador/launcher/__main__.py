"""CLI del launcher: ``python -m facturador.launcher --env homo|prod``.

Sin chooser (FAC-29): el ambiente se pasa explícito. Arranca el backend del
perfil oculto, espera readiness, abre el browser y permanece en primer plano
hasta Ctrl+C; entonces apaga el hijo sin dejarlo huérfano.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from ..constants import ArcaEnvironment
from ..profile import ProfileError
from .command import resolve_launch_environment
from .supervisor import LauncherError, ProcessSupervisor


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m facturador.launcher",
        description=(
            "Supervisor del launcher de FacturadorE: arranca un backend "
            "ligado a Homologación o Producción, espera /health y abre el "
            "navegador."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=[e.value for e in ArcaEnvironment],
        help="Ambiente a iniciar: homo (Homologación) o prod (Producción).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8399,
        help="Puerto local del backend (default: 8399).",
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        environment = resolve_launch_environment(args.env)
    except ProfileError as exc:
        _report_failure(str(exc))
        return 2

    supervisor = ProcessSupervisor(
        environment=environment,
        port=args.port,
        open_browser=not args.no_browser,
        readiness_timeout=args.timeout,
    )
    try:
        plan = supervisor.start()
    except LauncherError as exc:
        _report_failure(str(exc))
        return 1

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
        _report_failure(
            f"El backend de {plan.profile.display_name} se detuvo solo "
            f"(código {code})."
        )
        return 1
    except KeyboardInterrupt:
        print("\nDeteniendo…", flush=True)
        supervisor.stop()
        return 0


def _report_failure(message: str) -> None:
    """Falla visible: stderr siempre; diálogo nativo si no hay TTY."""
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
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

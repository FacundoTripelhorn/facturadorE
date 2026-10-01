"""CLI del launcher: ``python -m facturador.launcher [--env homo|prod]``.

Sin ``--env`` muestra el chooser: Homologación o Producción en
lenguaje de negocio. Con ``--env`` arranca ese ambiente de una vez (tests /
automatización). El supervisor arranca el backend del perfil oculto,
espera readiness, abre una ventana nativa (pywebview) y permanece en
primer plano hasta que se cierra la ventana o Ctrl+C. Si el webview no puede
arrancar, cae al navegador del sistema. ``--no-browser`` salta la UI (agents /
headless). Si el perfil ya tiene una sesión sana, reabre la UI y sale
sin duplicar el backend. Un error de arranque desde el chooser vuelve a la
pantalla de elección.

Si la UI pide "Cambiar ambiente", el launcher muestra el chooser
mientras el backend actual sigue vivo (cancelar = seguir igual). Al confirmar
otro ambiente, detiene el backend corriente *antes* de arrancar el perfil
nuevo; un fallo de arranque del destino no muta ninguno de los dos perfiles.

La primera vez que se abre Producción pide confirmación explícita
sobre la validez fiscal; cancelar vuelve al chooser sin arrancar.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from ..constants import DEFAULT_PORT, ArcaEnvironment
from ..profile import ProfileError
from .chooser import ChooserUnavailable, choose_environment
from .command import resolve_launch_environment
from .frozen import set_window_icon, stdin_is_tty
from .production_ack import ensure_production_acknowledged
from .supervisor import LauncherError, ProcessSupervisor
from .window import UiEndReason

# Inyectable en tests: reemplaza la pantalla/menú del chooser.
_ChooseFn = Callable[..., ArcaEnvironment | None]
# Inyectable en tests: confirmación de primer uso de Producción.
_ConfirmProdFn = Callable[[], bool]


@dataclass(frozen=True)
class SwitchTo:
    """El usuario confirmó otro ambiente: arrancar ese perfil a continuación."""

    environment: ArcaEnvironment


@dataclass(frozen=True)
class ReturnToChooser:
    """Volver al chooser sin error (canceló confirmación de Producción)."""


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
            "arranca el backend, espera /health y abre la ventana de la app."
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
        help=(
            "No abrir la ventana nativa ni el navegador al quedar listo "
            "(útil para automatización / agents)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Segundos máximos esperando readiness (default: 60).",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help=(
            "Restaurar el perfil desde seed.age + rebuild ARCA "
            "(backend debe estar detenido). Requiere --env e --identity."
        ),
    )
    parser.add_argument(
        "--identity",
        help="Ruta a la identidad age privada (requerida con --restore).",
    )
    parser.add_argument(
        "--seed",
        help="Ruta a seed.age (default: backups/seed.age del perfil).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    choose: _ChooseFn | None = None,
    confirm_production: _ConfirmProdFn | None = None,
    supervisor_factory: Callable[..., ProcessSupervisor] | None = None,
    report_failure: Callable[[str], None] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    factory = supervisor_factory or ProcessSupervisor
    chooser = choose or choose_environment
    fail = report_failure or _report_failure

    if args.restore:
        if args.env is None:
            fail("--restore requiere --env homo|prod.")
            return 2
        if not args.identity:
            fail("--restore requiere --identity <ruta-a-age-key>.")
            return 2
        from pathlib import Path

        from .restore_flow import run_launcher_restore

        try:
            env = resolve_launch_environment(args.env)
        except ProfileError as exc:
            fail(str(exc))
            return 2
        return run_launcher_restore(
            env,
            identity_path=Path(args.identity),
            seed_path=Path(args.seed) if args.seed else None,
        )

    # Sesión inicial: --env fija el primer perfil; sin flag, el chooser.
    if args.env is not None:
        try:
            pending: ArcaEnvironment | None = resolve_launch_environment(
                args.env
            )
        except ProfileError as exc:
            fail(str(exc))
            return 2
        allow_chooser_retry = False
    else:
        pending = None
        allow_chooser_retry = True

    while True:
        if pending is None:
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
        else:
            selected = pending
            pending = None

        outcome = _run_session(
            selected,
            args,
            factory=factory,
            choose=chooser,
            confirm_production=confirm_production,
            report_failure=fail,
            return_to_chooser_on_startup_error=allow_chooser_retry,
        )
        if isinstance(outcome, SwitchTo):
            # Ya confirmado en el chooser del cambio: arrancar el destino.
            # Vuelve a pedir ack solo si el perfil prod aún no lo tiene.
            pending = outcome.environment
            allow_chooser_retry = True
            continue
        if isinstance(outcome, ReturnToChooser):
            # Canceló la confirmación de Producción: chooser, sin salir.
            allow_chooser_retry = True
            continue
        if outcome is None:
            # Fallo de arranque / caída: volver al chooser si está permitido.
            if allow_chooser_retry:
                continue
            return 1
        return outcome


def _run_session(
    environment: ArcaEnvironment,
    args: argparse.Namespace,
    *,
    factory: Callable[..., ProcessSupervisor],
    choose: _ChooseFn,
    confirm_production: _ConfirmProdFn | None,
    report_failure: Callable[[str], None],
    return_to_chooser_on_startup_error: bool = False,
) -> int | None | SwitchTo | ReturnToChooser:
    """Supervisa una sesión.

    Retornos:
    - ``int``: código de salida del proceso launcher.
    - ``None``: volver al chooser (fallo de start o caída post-arranque).
    - ``ReturnToChooser``: canceló ack de Producción; sin error.
    - ``SwitchTo``: el usuario eligió otro ambiente; el backend actual ya
      está detenido.
    """
    try:
        acknowledged = ensure_production_acknowledged(
            environment,
            confirm=confirm_production,
        )
    except ChooserUnavailable as exc:
        report_failure(
            "No se pudo mostrar la confirmación de Producción. "
            f"{exc}"
        )
        return None if return_to_chooser_on_startup_error else 2
    except ProfileError as exc:
        # Misma ruta que supervisor.start(): p.ej. FACTURADOR_APP_DATA inválida.
        report_failure(str(exc))
        return None if return_to_chooser_on_startup_error else 2
    except OSError as exc:
        report_failure(
            "No se pudo guardar la confirmación de Producción: "
            f"{exc}"
        )
        return None if return_to_chooser_on_startup_error else 2
    if not acknowledged:
        print(
            "Apertura de Producción cancelada. "
            "Volvé a elegir el ambiente cuando quieras.",
            flush=True,
        )
        return ReturnToChooser()

    want_ui = not args.no_browser
    supervisor = factory(
        environment=environment,
        port=args.port,
        # start() no abre UI: el mensaje "listo" debe imprimirse antes de que
        # webview bloquee el hilo.
        open_browser=False,
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

    # Tras readiness: habilitar UI según CLI (fakes pueden ignorar el kwargs).
    try:
        supervisor.open_browser = want_ui
    except Exception:
        pass

    plan = result.plan
    if result.reused:
        print(
            f"FacturadorE ({plan.profile.display_name}) ya estaba en marcha "
            f"en {plan.base_url}/; se reabre la sesión existente.",
            flush=True,
        )
        if want_ui:
            try:
                _reopen_ui(supervisor)
            except KeyboardInterrupt:
                # No somos dueños del backend: no stop(); solo salir limpio.
                print("\nDeteniendo…", flush=True)
        # Reuse: este launcher no es dueño del backend; al cerrar la UI sale.
        return 0

    print(
        f"FacturadorE ({plan.profile.display_name}) listo en {plan.base_url}/",
        flush=True,
    )

    # Dueños del backend: cualquier Ctrl+C mientras la UI bloquea (webview) o
    # mientras supervisamos debe apagar el hijo (evita huérfanos).
    try:
        ui_reason: UiEndReason | None = None
        if want_ui:
            ui_reason = _reopen_ui(supervisor)

        if (
            want_ui
            and ui_reason is not None
            and ui_reason is not UiEndReason.BROWSER_FALLBACK
        ):
            return _after_native_ui_session(
                supervisor,
                environment=environment,
                choose=choose,
                confirm_production=confirm_production,
                report_failure=report_failure,
                return_to_chooser_on_startup_error=return_to_chooser_on_startup_error,
                initial_reason=ui_reason,
            )

        # --no-browser, opener inyectado, o fallback al navegador del sistema.
        print("Ctrl+C para detener.", flush=True)
        while supervisor.is_running:
            assert supervisor.process is not None
            switch = _handle_change_environment_request(
                supervisor,
                current=environment,
                choose=choose,
                confirm_production=confirm_production,
            )
            if switch is not None:
                return switch
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


def _after_native_ui_session(
    supervisor: ProcessSupervisor,
    *,
    environment: ArcaEnvironment,
    choose: _ChooseFn,
    confirm_production: _ConfirmProdFn | None,
    report_failure: Callable[[str], None],
    return_to_chooser_on_startup_error: bool,
    initial_reason: UiEndReason,
) -> int | None | SwitchTo | ReturnToChooser:
    """Ciclo post-webview: cerrar = apagar backend; interrupt = cambio de
    ambiente / muerte."""
    reason = initial_reason
    try:
        while True:
            if not supervisor.is_running:
                code = (
                    supervisor.process.returncode
                    if supervisor.process is not None
                    else 1
                )
                report_failure(
                    f"El backend de {supervisor.plan.profile.display_name} "
                    f"se detuvo solo (código {code})."
                )
                return None if return_to_chooser_on_startup_error else 1

            if reason is UiEndReason.INTERRUPTED:
                switch = _handle_change_environment_request(
                    supervisor,
                    current=environment,
                    choose=choose,
                    confirm_production=confirm_production,
                    reopen_on_same=False,
                )
                if switch is not None:
                    return switch
                # Canceló / mismo ambiente: reabrir ventana (o browser si falla).
                print(
                    f"FacturadorE ({supervisor.plan.profile.display_name}) "
                    f"sigue en {supervisor.base_url}/",
                    flush=True,
                )
                reopened = _reopen_ui(supervisor)
                if reopened is None:
                    # open_browser desactivado a mitad de sesión: supervisar.
                    break
                if reopened is UiEndReason.BROWSER_FALLBACK:
                    print("Ctrl+C para detener.", flush=True)
                    break
                reason = reopened
                continue

            # CLOSED: el usuario cerró la ventana → apagar backend (ownership).
            print("Ventana cerrada; deteniendo…", flush=True)
            supervisor.stop()
            return 0
    except KeyboardInterrupt:
        print("\nDeteniendo…", flush=True)
        supervisor.stop()
        return 0

    # Fallback browser tras reopen fallido / no-webview: loop clásico.
    try:
        while supervisor.is_running:
            assert supervisor.process is not None
            switch = _handle_change_environment_request(
                supervisor,
                current=environment,
                choose=choose,
                confirm_production=confirm_production,
            )
            if switch is not None:
                return switch
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
            f"El backend de {supervisor.plan.profile.display_name} "
            f"se detuvo solo (código {code})."
        )
        return None if return_to_chooser_on_startup_error else 1
    except KeyboardInterrupt:
        print("\nDeteniendo…", flush=True)
        supervisor.stop()
        return 0


def _reopen_ui(supervisor: ProcessSupervisor) -> UiEndReason | None:
    """Abre o reabre la UI (ventana nativa o opener inyectado).

    Returns:
        Razón de fin de la sesión de UI, o ``None`` si no hay que abrir UI.
    """
    if not getattr(supervisor, "open_browser", False):
        return None
    open_ui = getattr(supervisor, "open_ui", None)
    if callable(open_ui):
        result = open_ui()
        if result is None:
            return None
        return result.reason
    # Fake supervisors de tests: opener inyectado no bloqueante.
    opener = getattr(supervisor, "browser_opener", None)
    base_url = getattr(supervisor, "base_url", None)
    if callable(opener) and base_url:
        try:
            opener(f"{base_url}/")
        except Exception:
            pass
        return UiEndReason.BROWSER_FALLBACK
    # Sin open_ui ni opener: no hay GUI real (fakes de smoke de main).
    return UiEndReason.BROWSER_FALLBACK


def _handle_change_environment_request(
    supervisor: ProcessSupervisor,
    *,
    current: ArcaEnvironment,
    choose: _ChooseFn,
    confirm_production: _ConfirmProdFn | None = None,
    reopen_on_same: bool = True,
) -> SwitchTo | None:
    """Chooser con el backend aún vivo; stop solo si confirma otro.

    ``None`` = no hay pedido, o el usuario canceló / eligió el mismo ambiente.
    Si el destino es Producción sin ack, la confirmación corre *antes* de
    ``stop()``: cancelar mantiene la sesión actual.

    ``reopen_on_same``: si False, el caller reabre la UI (sesión webview nativa
    donde ``open_ui`` bloquearía de nuevo dentro de este handler).
    """
    request = supervisor.poll_change_environment_request()
    if request is None:
        return None

    print(
        "Cambio de ambiente pedido desde la app. "
        "Elegí Homologación o Producción (Cancelar mantiene el actual).",
        flush=True,
    )
    try:
        selected = choose()
    except ChooserUnavailable as exc:
        # Sin chooser no se puede cambiar: limpiar pedido y seguir.
        supervisor.clear_change_environment_request()
        print(
            f"No se pudo mostrar el selector de ambiente ({exc}). "
            "Se mantiene el ambiente actual.",
            flush=True,
        )
        return None

    supervisor.clear_change_environment_request()
    if selected is None:
        # Cancelar: el backend corriente sigue corriendo.
        print("Cambio de ambiente cancelado.", flush=True)
        return None
    if selected is current:
        # Mismo ambiente: reabrir la app sin reiniciar.
        print(
            f"Ya estás en {supervisor.plan.profile.display_name}.",
            flush=True,
        )
        if reopen_on_same and supervisor.open_browser:
            open_ui = getattr(supervisor, "open_ui", None)
            if callable(open_ui):
                try:
                    open_ui()
                except Exception:
                    pass
            else:
                opener = getattr(supervisor, "browser_opener", None)
                if callable(opener):
                    try:
                        opener(f"{supervisor.base_url}/")
                    except Exception:
                        pass
        return None

    # Ack de Producción antes de apagar el backend actual.
    try:
        acknowledged = ensure_production_acknowledged(
            selected,
            confirm=confirm_production,
        )
    except ChooserUnavailable as exc:
        print(
            f"No se pudo mostrar la confirmación de Producción ({exc}). "
            "Se mantiene el ambiente actual.",
            flush=True,
        )
        return None
    except (ProfileError, OSError) as exc:
        print(
            f"No se pudo confirmar Producción ({exc}). "
            "Se mantiene el ambiente actual.",
            flush=True,
        )
        return None
    if not acknowledged:
        print(
            "Cambio a Producción cancelado. "
            "Se mantiene el ambiente actual.",
            flush=True,
        )
        return None

    # Invariante ADR 0001: apagar el actual ANTES de arrancar el otro.
    print(
        f"Deteniendo {supervisor.plan.profile.display_name} "
        "antes de abrir el otro ambiente…",
        flush=True,
    )
    supervisor.stop()
    return SwitchTo(selected)


def _report_failure(message: str) -> None:
    """Falla visible: stderr siempre; diálogo nativo si no hay TTY.

    Sin diálogo bajo pytest (``PYTEST_CURRENT_TEST``) ni con
    ``FACTURADOR_NO_DIALOGS`` (smoke del exe en CI): un messagebox modal sin
    nadie que lo cierre colgaría el proceso.
    """
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get(
        "FACTURADOR_NO_DIALOGS"
    ):
        return
    if _is_interactive_terminal():
        return
    try:
        import tkinter
        from tkinter import messagebox
    except Exception:
        return
    try:
        root = tkinter.Tk()
        root.withdraw()
        set_window_icon(root)
        messagebox.showerror("FacturadorE", message)
        root.destroy()
    except Exception:
        return


def _is_interactive_terminal() -> bool:
    """True si hay terminal para leer el error. El exe de ventana no tiene
    stdin/stderr (``None``) o los tiene redirigidos a un archivo."""
    stderr = sys.stderr
    try:
        return stdin_is_tty() and bool(stderr is not None and stderr.isatty())
    except (AttributeError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())

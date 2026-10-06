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
from .failure_window import FailureAction, prompt_failure
from .frozen import has_interactive_terminal
from .last_environment import save_last_environment
from .production_ack import ensure_production_acknowledged
from .progress import StartupCancelled, StartupUI, StartupWindow, close_window
from .supervisor import LauncherError, ProcessSupervisor
from .window import UiEndReason

# Inyectable en tests: reemplaza la pantalla/menú del chooser.
_ChooseFn = Callable[..., ArcaEnvironment | None]
# Inyectable en tests: confirmación de primer uso de Producción.
_ConfirmProdFn = Callable[[], bool]
# Inyectable en tests: ventana de error (mensaje, ambiente, si puede volver
# al chooser) → qué eligió el usuario, o None si no se mostró.
_FailurePromptFn = Callable[..., FailureAction | None]
# Inyectable en tests: recordar el ambiente que abrió bien.
_RememberFn = Callable[[ArcaEnvironment], None]


@dataclass(frozen=True)
class SwitchTo:
    """El usuario confirmó otro ambiente: arrancar ese perfil a continuación."""

    environment: ArcaEnvironment
    # El ambiente que se cerró para el cambio (para la vista "Cambiando a").
    from_environment: ArcaEnvironment | None = None


@dataclass(frozen=True)
class ReturnToChooser:
    """Volver al chooser sin error (canceló confirmación de Producción)."""


@dataclass(frozen=True)
class Retry:
    """El usuario pidió "Reintentar" en la ventana de error."""

    environment: ArcaEnvironment


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
    failure_prompt: _FailurePromptFn | None = None,
    remember_environment: _RememberFn | None = None,
    startup_ui: StartupUI | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    # Una sola ventana del launcher hasta que abre la app; al salir, se
    # cierra si quedó abierta.
    try:
        return _main(
            args,
            choose=choose,
            confirm_production=confirm_production,
            supervisor_factory=supervisor_factory,
            report_failure=report_failure,
            failure_prompt=failure_prompt,
            remember_environment=remember_environment,
            startup_ui=startup_ui,
        )
    finally:
        close_window()


def _main(
    args: argparse.Namespace,
    *,
    choose: _ChooseFn | None,
    confirm_production: _ConfirmProdFn | None,
    supervisor_factory: Callable[..., ProcessSupervisor] | None,
    report_failure: Callable[[str], None] | None,
    failure_prompt: _FailurePromptFn | None,
    remember_environment: _RememberFn | None,
    startup_ui: StartupUI | None,
) -> int:
    factory = supervisor_factory or ProcessSupervisor
    chooser = choose or choose_environment
    report = report_failure or _report_failure
    prompt = failure_prompt or prompt_failure
    remember = remember_environment or _remember_last_environment
    progress = startup_ui or StartupWindow(show=not args.no_browser)

    def fail(message: str) -> None:
        # Errores sin ambiente: solo "Cerrar".
        report(message)
        prompt(message, environment=None, can_choose=False)

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
    switching_from: ArcaEnvironment | None = None

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
            report_failure=report,
            failure_prompt=prompt,
            remember_environment=remember,
            startup_ui=progress,
            switching_from=switching_from,
            return_to_chooser_on_startup_error=allow_chooser_retry,
        )
        switching_from = None
        if isinstance(outcome, Retry):
            # "Reintentar" en la ventana de error: el mismo ambiente.
            pending = outcome.environment
            continue
        if isinstance(outcome, SwitchTo):
            # Ya confirmado en el chooser del cambio: arrancar el destino.
            # Vuelve a pedir ack solo si el perfil prod aún no lo tiene.
            pending = outcome.environment
            switching_from = outcome.from_environment
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
    failure_prompt: _FailurePromptFn | None = None,
    remember_environment: _RememberFn | None = None,
    startup_ui: StartupUI | None = None,
    switching_from: ArcaEnvironment | None = None,
    return_to_chooser_on_startup_error: bool = False,
) -> int | None | SwitchTo | ReturnToChooser | Retry:
    """Supervisa una sesión.

    Retornos:
    - ``int``: código de salida del proceso launcher.
    - ``None``: volver al chooser (fallo de start o caída post-arranque).
    - ``ReturnToChooser``: canceló ack de Producción; sin error.
    - ``SwitchTo``: el usuario eligió otro ambiente; el backend actual ya
      está detenido.
    - ``Retry``: el usuario pidió reintentar tras un error.
    - ``ReturnToChooser`` también si canceló el arranque desde el progreso.
    """
    prompt = failure_prompt or prompt_failure
    remember = remember_environment or _remember_last_environment
    progress = startup_ui or StartupWindow()

    def failed(message: str, code: int) -> int | None | Retry:
        return _session_failed(
            message,
            environment,
            report_failure=report_failure,
            failure_prompt=prompt,
            can_choose=return_to_chooser_on_startup_error,
            code=code,
        )

    try:
        acknowledged = ensure_production_acknowledged(
            environment,
            confirm=confirm_production,
        )
    except ChooserUnavailable as exc:
        return failed(
            "No se pudo mostrar la confirmación de Producción. "
            f"{exc}",
            2,
        )
    except ProfileError as exc:
        # Misma ruta que supervisor.start(): p.ej. FACTURADOR_APP_DATA inválida.
        return failed(str(exc), 2)
    except OSError as exc:
        return failed(
            "No se pudo guardar la confirmación de Producción: "
            f"{exc}",
            2,
        )
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
        # Con ventana, start() corre en un hilo y la ventana muestra el
        # progreso; "Cancelar" lo detiene con stop().
        result = progress.start(
            environment,
            supervisor.start,
            lambda: supervisor.stop(),
            switching_from=switching_from,
        )
    except StartupCancelled:
        print(
            f"Inicio de {_display_name(environment)} cancelado.",
            flush=True,
        )
        return ReturnToChooser() if return_to_chooser_on_startup_error else 0
    except LauncherError as exc:
        return failed(str(exc), 1)
    except ProfileError as exc:
        # Cinturón por si el plan falla fuera del parser (p.ej. puerto vía API).
        return failed(str(exc), 2)
    except KeyboardInterrupt:
        # start() (o la ventana de progreso) ya apagó el hijo; no dejar
        # traceback al usuario.
        print("\nDeteniendo…", flush=True)
        return 0

    # Listo: la ventana del launcher deja lugar a la de la app.
    close_window()

    # /health respondió: es el ambiente a preseleccionar la próxima vez.
    remember(environment)

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
                failure_prompt=prompt,
                startup_ui=progress,
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
                startup_ui=progress,
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
        # Caída post-arranque: desde el chooser se puede elegir de nuevo.
        return failed(
            f"El backend de {plan.profile.display_name} se detuvo solo "
            f"(código {code}).",
            1,
        )
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
    failure_prompt: _FailurePromptFn | None = None,
    startup_ui: StartupUI | None = None,
) -> int | None | SwitchTo | ReturnToChooser | Retry:
    """Ciclo post-webview: cerrar = apagar backend; interrupt = cambio de
    ambiente / muerte."""
    prompt = failure_prompt or prompt_failure

    def died() -> int | None | Retry:
        code = (
            supervisor.process.returncode
            if supervisor.process is not None
            else 1
        )
        return _session_failed(
            f"El backend de {supervisor.plan.profile.display_name} "
            f"se detuvo solo (código {code}).",
            environment,
            report_failure=report_failure,
            failure_prompt=prompt,
            can_choose=return_to_chooser_on_startup_error,
            code=1,
        )

    reason = initial_reason
    try:
        while True:
            if not supervisor.is_running:
                return died()

            if reason is UiEndReason.INTERRUPTED:
                switch = _handle_change_environment_request(
                    supervisor,
                    current=environment,
                    choose=choose,
                    confirm_production=confirm_production,
                    reopen_on_same=False,
                    startup_ui=startup_ui,
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
                startup_ui=startup_ui,
            )
            if switch is not None:
                return switch
            try:
                supervisor.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                continue
        return died()
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
    # La ventana del launcher (selector, progreso) deja lugar a la app.
    close_window()
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
    startup_ui: StartupUI | None = None,
) -> SwitchTo | ReturnToChooser | None:
    """Chooser con el backend aún vivo; stop solo si confirma otro.

    ``None`` = no hay pedido, o el usuario canceló / eligió el mismo ambiente.
    Si el destino es Producción sin ack, la confirmación corre *antes* de
    ``stop()``: cancelar mantiene la sesión actual. ``ReturnToChooser`` si
    canceló mientras se cerraba el actual (ya no hay sesión que mantener).

    ``reopen_on_same``: si False, el caller reabre la UI (sesión webview nativa
    donde ``open_ui`` bloquearía de nuevo dentro de este handler).
    """
    outcome = _decide_change_environment(
        supervisor,
        current=current,
        choose=choose,
        confirm_production=confirm_production,
        reopen_on_same=reopen_on_same,
        startup_ui=startup_ui or StartupWindow(),
    )
    if outcome is None:
        # Sigue la sesión actual: el selector no queda abierto encima.
        close_window()
    return outcome


def _decide_change_environment(
    supervisor: ProcessSupervisor,
    *,
    current: ArcaEnvironment,
    choose: _ChooseFn,
    confirm_production: _ConfirmProdFn | None,
    reopen_on_same: bool,
    startup_ui: StartupUI,
) -> SwitchTo | ReturnToChooser | None:
    request = supervisor.poll_change_environment_request()
    if request is None:
        return None

    print(
        "Cambio de ambiente pedido desde la app. "
        "Elegí Homologación o Producción (Cancelar mantiene el actual).",
        flush=True,
    )
    try:
        selected = _choose_for_switch(choose, current)
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
            close_window()
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

    # Invariante ADR 0001: apagar el actual ANTES de arrancar el otro. Con
    # ventana, el progreso "Cambiando a …" aparece ya, mientras se cierra.
    print(
        f"Deteniendo {supervisor.plan.profile.display_name} "
        "antes de abrir el otro ambiente…",
        flush=True,
    )
    cancelled = startup_ui.stop_for_switch(
        current, selected, lambda: supervisor.stop()
    )
    if cancelled:
        print(
            f"Cambio a {_display_name(selected)} cancelado; "
            f"{supervisor.plan.profile.display_name} ya se cerró.",
            flush=True,
        )
        return ReturnToChooser()
    return SwitchTo(selected, from_environment=current)


def _display_name(environment: ArcaEnvironment) -> str:
    return "Producción" if environment is ArcaEnvironment.PROD else "Homologación"


def _choose_for_switch(
    choose: _ChooseFn, current: ArcaEnvironment
) -> ArcaEnvironment | None:
    """El chooser de un cambio de ambiente: el por defecto preselecciona el
    otro y marca el actual; uno inyectado (tests) se llama sin argumentos."""
    if choose is choose_environment:
        return choose_environment(current=current)
    return choose()


def _session_failed(
    message: str,
    environment: ArcaEnvironment,
    *,
    report_failure: Callable[[str], None],
    failure_prompt: _FailurePromptFn,
    can_choose: bool,
    code: int,
) -> int | None | Retry:
    """Informa un error de la sesión y resuelve qué sigue.

    ``Retry`` si el usuario pidió reintentar; si no, ``None`` (volver al
    chooser) cuando se llegó desde el chooser, o ``code``.
    """
    report_failure(message)
    action = failure_prompt(
        message, environment=environment, can_choose=can_choose
    )
    if action is FailureAction.RETRY:
        return Retry(environment)
    return None if can_choose else code


def _remember_last_environment(environment: ArcaEnvironment) -> None:
    """Guarda el ambiente que abrió bien; un fallo no corta la sesión.

    Bajo pytest no toca el app-data real del usuario (los tests inyectan
    ``remember_environment``).
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        save_last_environment(environment)
    except (OSError, ProfileError) as exc:
        print(
            f"No se pudo guardar el último ambiente usado ({exc}).",
            flush=True,
        )


def _report_failure(message: str) -> None:
    """Falla visible en stderr (o en launcher.log en el exe de ventana).

    La ventana de error es aparte (``failure_window.prompt_failure``).
    """
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


def _is_interactive_terminal() -> bool:
    return has_interactive_terminal()


if __name__ == "__main__":
    raise SystemExit(main())

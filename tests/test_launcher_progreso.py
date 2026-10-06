"""Progreso del launcher (sin pantalla): el arranque en un hilo con
cancelación sin huérfanos, y el flujo de ``main`` con un doble de la
ventana (arrancar, cancelar, cambiar de ambiente)."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from facturador.constants import ArcaEnvironment
from facturador.launcher import ProcessSupervisor
from facturador.launcher.progress import (
    StartupCancelled,
    StartupWindow,
    _Arranque,
    textos,
)
from facturador.launcher.supervisor import LauncherError

HOMO, PROD = ArcaEnvironment.HOMO, ArcaEnvironment.PROD


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- textos de la vista ------------------------------------------------------


def test_textos_al_iniciar():
    titulo, explicacion, pasos = textos(HOMO, switching_from=None, cerrando=False)
    assert titulo == "Iniciando Homologación"
    assert explicacion == (
        "Preparando el servidor local. La app se abre sola cuando esté lista."
    )
    assert pasos == [(False, "Esperando al servidor…")]


def test_textos_al_cambiar_de_ambiente():
    titulo, explicacion, pasos = textos(PROD, switching_from=HOMO, cerrando=True)
    assert titulo == "Cambiando a Producción"
    assert explicacion == (
        "Cerramos Homologación y abrimos Producción. La app se abre sola "
        "cuando esté lista."
    )
    assert pasos == [(False, "Cerrando Homologación…")]
    _, _, pasos = textos(PROD, switching_from=HOMO, cerrando=False)
    assert pasos == [(True, "Homologación cerrada"), (False, "Iniciando Producción…")]


# --- sin ventana todo sigue igual -------------------------------------------


def test_sin_ventana_arranca_y_cierra_en_el_hilo_principal():
    ui = StartupWindow()
    assert not ui.wanted()  # bajo pytest
    hilo: list[str] = []

    def _start():
        hilo.append(threading.current_thread().name)
        return "listo"

    assert ui.start(HOMO, _start, lambda: None) == "listo"
    detenidos: list[str] = []
    assert ui.stop_for_switch(HOMO, PROD, lambda: detenidos.append("stop")) is False
    assert hilo == [threading.main_thread().name]
    assert detenidos == ["stop"]


def test_con_no_browser_no_hay_ventana(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.setattr(
        "facturador.launcher.progress.has_interactive_terminal", lambda: False
    )
    assert StartupWindow(show=True).wanted()
    assert not StartupWindow(show=False).wanted()
    monkeypatch.setenv("FACTURADOR_NO_DIALOGS", "1")
    assert not StartupWindow(show=True).wanted()


# --- el arranque en un hilo ---------------------------------------------------


def test_arranque_devuelve_el_resultado_y_relanza_errores():
    ok = _Arranque(lambda: 42, lambda: None)
    ok.iniciar()
    assert ok.esperar(5)
    assert ok.resultado() == (42, False)

    def _falla():
        raise LauncherError("puerto ocupado")

    mal = _Arranque(_falla, lambda: None)
    mal.iniciar()
    assert mal.esperar(5)
    with pytest.raises(LauncherError, match="puerto ocupado"):
        mal.resultado()


def test_cancelar_antes_del_hijo_lo_apaga_apenas_aparece():
    """Cancelar antes de que exista el hijo: el apagado se reintenta y actúa
    cuando el hijo aparece, sin esperar a que start() termine solo."""
    hijo_vivo = threading.Event()
    muerto = threading.Event()
    llamadas: list[bool] = []

    def _start():
        time.sleep(0.3)
        hijo_vivo.set()  # el hijo aparece después del pedido de cancelar
        # Como _wait_until_ready: espera /health hasta que matan al hijo.
        if not muerto.wait(30):
            raise AssertionError("nadie apagó al hijo")
        raise LauncherError("terminó antes de quedar listo")

    def _stop_si_hay_hijo():
        llamadas.append(hijo_vivo.is_set())
        if hijo_vivo.is_set():
            muerto.set()

    arranque = _Arranque(_start, _stop_si_hay_hijo)
    arranque.iniciar()
    assert arranque.pedir_cancelar()
    assert not arranque.pedir_cancelar()  # un solo pedido
    inicio = time.monotonic()
    assert arranque.esperar(5)
    assert time.monotonic() - inicio < 2
    assert arranque.resultado() == (None, True)
    assert llamadas[0] is False and llamadas[-1] is True


def test_cancelar_el_cierre_no_lo_interrumpe():
    """En un cambio, cancelar no corta el stop() del actual: se espera y se
    informa el pedido."""
    terminado = threading.Event()

    def _stop_actual():
        time.sleep(0.2)
        terminado.set()

    arranque = _Arranque(_stop_actual, None)
    arranque.iniciar()
    arranque.pedir_cancelar()
    assert arranque.esperar(5)
    assert terminado.is_set()
    assert arranque.resultado() == (None, True)


def _supervisor_lento(tmp_path):
    return ProcessSupervisor(
        environment=HOMO,
        port=_free_port(),
        app_data_root=tmp_path / "appdata",
        home=tmp_path / "home",
        readiness_timeout=30.0,
        open_browser=False,
        command_override=[sys.executable, "-c", "import time; time.sleep(60)"],
    )


def _lock_libre(supervisor: ProcessSupervisor) -> bool:
    from facturador.launcher.lock import ProfileLock, ProfileLockHeld

    lock = ProfileLock(supervisor.plan.profile.paths.launcher_lock)
    try:
        lock.acquire(port=supervisor.port, environment="homo")
    except ProfileLockHeld:
        return False
    lock.release()
    return True


def test_cancelar_durante_el_arranque_no_deja_huerfanos(tmp_path):
    """Con un supervisor real: el hijo que espera /health muere al cancelar."""
    from facturador.launcher.__main__ import _stop_spawned

    supervisor = _supervisor_lento(tmp_path)
    arranque = _Arranque(supervisor.start, lambda: _stop_spawned(supervisor))
    arranque.iniciar()
    limite = time.monotonic() + 10
    while supervisor.process is None and time.monotonic() < limite:
        time.sleep(0.05)
    hijo = supervisor.process
    assert hijo is not None

    arranque.pedir_cancelar()
    assert arranque.esperar(15)
    assert arranque.resultado()[1] is True
    assert hijo.poll() is not None  # el hijo terminó
    assert not supervisor.is_running
    assert _lock_libre(supervisor)


def test_cancelar_antes_de_lanzar_el_hijo_no_suelta_el_lock_ni_espera(
    tmp_path, monkeypatch
):
    """Cancelar con el lock tomado y el hijo todavía sin lanzar: el lock no
    se suelta antes de tiempo (otro launcher podría tomar el mismo perfil) y
    el hijo muere apenas aparece, sin esperar el timeout de /health."""
    from facturador.launcher.__main__ import _stop_spawned

    supervisor = _supervisor_lento(tmp_path)
    pedido = threading.Event()
    lock_libre_antes_del_hijo: list[bool] = []
    lanzar = supervisor._spawn

    def _spawn_demorado(plan):
        pedido.wait(5)
        time.sleep(0.3)  # el pedido de cancelar ya se reintentó varias veces
        lock_libre_antes_del_hijo.append(_lock_libre(supervisor))
        return lanzar(plan)

    monkeypatch.setattr(supervisor, "_spawn", _spawn_demorado)
    hijos: list[subprocess.Popen[bytes]] = []

    def _cancelar() -> None:
        if supervisor.process is not None:
            hijos.append(supervisor.process)
        _stop_spawned(supervisor)

    arranque = _Arranque(supervisor.start, _cancelar)
    arranque.iniciar()
    arranque.pedir_cancelar()
    pedido.set()
    inicio = time.monotonic()
    assert arranque.esperar(10)
    assert time.monotonic() - inicio < 5  # no esperó los 30 s de readiness
    assert arranque.resultado()[1] is True
    assert lock_libre_antes_del_hijo == [False]
    assert hijos and all(h.poll() is not None for h in hijos)
    assert not supervisor.is_running
    assert _lock_libre(supervisor)


# --- flujo de main con un doble de la ventana --------------------------------


class _UI:
    """Doble de la ventana de progreso: registra y simula cancelar."""

    def __init__(
        self,
        *,
        cancelar_inicio: set[ArcaEnvironment] | None = None,
        cancelar_cambio: bool = False,
    ):
        self.llamadas: list[tuple[str, ArcaEnvironment, ArcaEnvironment | None]] = []
        self.cancelar_inicio = cancelar_inicio or set()
        self.cancelar_cambio = cancelar_cambio

    def start(self, environment, start, cancel, *, switching_from=None):
        self.llamadas.append(("start", environment, switching_from))
        if environment in self.cancelar_inicio:
            cancel()
            raise StartupCancelled
        return start()

    def stop_for_switch(self, current, target, stop):
        self.llamadas.append(("cambio", target, current))
        stop()
        return self.cancelar_cambio


class _Proc:
    returncode = 0

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 1)


def _supervisores(eventos: list[tuple[str, str]], *, pide_cambio: bool):
    """Supervisores falsos: la primera sesión de homologación pide cambiar
    de ambiente; las siguientes arrancan como sesión reutilizada (sale 0)."""
    from facturador.launcher.command import plan_backend_launch
    from facturador.launcher.supervisor import LaunchResult

    sesiones = {"n": 0}

    class _Supervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment
            self.process = _Proc()
            self.is_running = True
            self.open_browser = False
            self.browser_opener = lambda _url: None
            self.plan = plan_backend_launch(environment, port=8399)
            self.base_url = self.plan.base_url
            sesiones["n"] += 1
            self._primera = sesiones["n"] == 1
            self._pidio = False

        def start(self):
            eventos.append(("start", self.environment.value))
            return LaunchResult(plan=self.plan, reused=not self._primera)

        def stop(self):
            eventos.append(("stop", self.environment.value))
            self.is_running = False

        def poll_change_environment_request(self):
            from facturador.launcher.switch import ChangeEnvironmentRequest

            if pide_cambio and self._primera and not self._pidio:
                self._pidio = True
                return ChangeEnvironmentRequest(
                    from_environment=self.environment, requested_at=1.0
                )
            return None

        def clear_change_environment_request(self):
            return None

    return _Supervisor


def _main(
    argv: list[str],
    picks: list[ArcaEnvironment | None],
    ui: _UI,
    eventos: list[tuple[str, str]],
    *,
    pide_cambio: bool = False,
    prompts: list[object] | None = None,
) -> int:
    from facturador.launcher.__main__ import main

    elecciones = iter(picks)
    avisos = prompts if prompts is not None else []
    return main(
        argv,
        choose=lambda: next(elecciones),
        supervisor_factory=_supervisores(eventos, pide_cambio=pide_cambio),
        report_failure=lambda _msg: None,
        failure_prompt=lambda *a, **k: avisos.append((a, k)),
        remember_environment=lambda _env: None,
        startup_ui=ui,  # type: ignore[arg-type]
    )


def test_cancelar_el_arranque_vuelve_al_selector():
    eventos: list[tuple[str, str]] = []
    avisos: list[object] = []
    ui = _UI(cancelar_inicio={PROD})
    code = _main([], [PROD, None], ui, eventos, prompts=avisos)
    assert code == 0
    assert ui.llamadas == [("start", PROD, None)]
    # cancel() apagó lo que se levantaba; no se muestra error.
    assert eventos == [("stop", "prod")]
    assert avisos == []


def test_cancelar_el_arranque_con_env_sale():
    eventos: list[tuple[str, str]] = []
    ui = _UI(cancelar_inicio={HOMO})
    code = _main(["--env", "homo"], [], ui, eventos)
    assert code == 0
    assert ui.llamadas == [("start", HOMO, None)]


def test_cambio_de_ambiente_cierra_el_actual_y_arranca_el_otro():
    eventos: list[tuple[str, str]] = []
    ui = _UI()
    code = _main([], [HOMO, PROD], ui, eventos, pide_cambio=True)
    assert code == 0
    assert ui.llamadas == [
        ("start", HOMO, None),
        ("cambio", PROD, HOMO),
        ("start", PROD, HOMO),
    ]
    # Invariante: el actual se apaga antes de arrancar el otro.
    assert eventos == [("start", "homo"), ("stop", "homo"), ("start", "prod")]


def test_cancelar_durante_el_cambio_vuelve_al_selector_sin_arrancar_el_otro():
    eventos: list[tuple[str, str]] = []
    ui = _UI(cancelar_cambio=True)
    code = _main([], [HOMO, PROD, None], ui, eventos, pide_cambio=True)
    assert code == 0
    assert ui.llamadas == [("start", HOMO, None), ("cambio", PROD, HOMO)]
    assert eventos == [("start", "homo"), ("stop", "homo")]


def test_reintentar_vuelve_a_mostrar_iniciando(monkeypatch):
    """Un error y "Reintentar": el progreso vuelve a "Iniciando", no a un
    cambio de ambiente."""
    from facturador.launcher.__main__ import main
    from facturador.launcher.command import plan_backend_launch
    from facturador.launcher.failure_window import FailureAction
    from facturador.launcher.supervisor import LaunchResult

    intentos = {"n": 0}

    class _Supervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment
            self.process = None
            self.is_running = False
            self.plan = plan_backend_launch(environment, port=8399)

        def start(self):
            intentos["n"] += 1
            if intentos["n"] == 1:
                raise LauncherError("puerto ocupado")
            return LaunchResult(plan=self.plan, reused=True)

        def stop(self):
            return None

    ui = _UI()
    code = main(
        ["--env", "prod"],
        supervisor_factory=_Supervisor,
        report_failure=lambda _msg: None,
        failure_prompt=lambda *_a, **_k: FailureAction.RETRY,
        remember_environment=lambda _env: None,
        startup_ui=ui,  # type: ignore[arg-type]
    )
    assert code == 0
    assert ui.llamadas == [("start", PROD, None), ("start", PROD, None)]


def test_main_cierra_la_ventana_al_salir(monkeypatch):
    from facturador.launcher import __main__ as launcher_main

    cerradas: list[str] = []
    monkeypatch.setattr(launcher_main, "close_window", lambda: cerradas.append("x"))
    assert launcher_main.main([], choose=lambda: None) == 0
    assert cerradas == ["x"]

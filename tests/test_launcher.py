"""Launcher: resolución de comando/perfil y supervisión de proceso (FAC-28).

También cubre el chooser de ambiente (FAC-29), el lock anti-duplicado
(FAC-30) y el cambio de ambiente por reinicio (FAC-32).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from facturador.constants import ArcaEnvironment
from facturador.launcher import (
    ENVIRONMENT_OPTIONS,
    LauncherError,
    ProcessSupervisor,
    build_backend_command,
    build_backend_env,
    choose_environment,
    environment_options,
    plan_backend_launch,
    prompt_environment_tty,
    resolve_launch_environment,
    resolve_launch_profile,
)
from facturador.profile import EnvironmentProfile, ProfileError, resolve_app_data_root


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _perfil_con_certs(
    environment: ArcaEnvironment,
    app_data_root: Path,
    cert_and_key: tuple[bytes, bytes],
):
    profile = resolve_launch_profile(environment, app_data_root=app_data_root)
    profile.paths.ensure_layout()
    cert_pem, key_pem = cert_and_key
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    profile.paths.key.chmod(0o400)
    return profile


# --- Resolución de comando / perfil -----------------------------------------


def test_resolve_launch_environment_acepta_homo_y_prod():
    assert resolve_launch_environment("homo") is ArcaEnvironment.HOMO
    assert resolve_launch_environment("PROD") is ArcaEnvironment.PROD


def test_resolve_launch_environment_rechaza_valores_invalidos():
    with pytest.raises(ProfileError, match="launcher"):
        resolve_launch_environment("staging")


@pytest.mark.parametrize("environment", list(ArcaEnvironment))
def test_resolve_launch_profile_aisla_por_ambiente(environment, tmp_path):
    profile = resolve_launch_profile(environment, app_data_root=tmp_path / "app")
    assert profile.environment is environment
    assert profile.paths.root == tmp_path / "app" / environment.value


def test_build_backend_command_usa_python_m_facturador():
    assert build_backend_command(python="/usr/bin/python3") == [
        "/usr/bin/python3",
        "-m",
        "facturador",
    ]


def test_build_backend_env_pasa_un_solo_ambiente(tmp_path):
    env = build_backend_env(
        ArcaEnvironment.PROD,
        port=8401,
        app_data_root=tmp_path / "appdata",
        home=tmp_path / "home",
        base_env={
            "PATH": "/bin",
            "ARCA_ENV": "homo",
            "FACTURADOR_IN_DOCKER": "1",
            "FACTURADOR_PORT": "1",
        },
    )

    assert env["ARCA_ENV"] == "prod"
    assert env["FACTURADOR_PORT"] == "8401"
    assert env["FACTURADOR_HOME"] == str(tmp_path / "home")
    assert env["FACTURADOR_APP_DATA"] == str(tmp_path / "appdata")
    assert env["FACTURADOR_LAUNCHER"] == "1"
    assert "FACTURADOR_IN_DOCKER" not in env
    # Un solo ambiente: el valor previo de homo queda reemplazado, no duplicado.
    assert list(k for k in env if k == "ARCA_ENV") == ["ARCA_ENV"]


def test_plan_backend_launch_liga_perfil_comando_y_puerto(tmp_path):
    plan = plan_backend_launch(
        ArcaEnvironment.HOMO,
        port=8410,
        app_data_root=tmp_path / "app",
        home=tmp_path / "home",
        python=sys.executable,
    )

    assert plan.environment is ArcaEnvironment.HOMO
    assert plan.profile.display_name == "Homologación"
    assert plan.port == 8410
    assert plan.base_url == "http://127.0.0.1:8410"
    assert plan.health_url == "http://127.0.0.1:8410/health"
    assert plan.command == [sys.executable, "-m", "facturador"]
    assert plan.env["ARCA_ENV"] == "homo"
    assert str(plan.profile.paths.root) not in repr(plan.profile)


def test_facturador_app_data_override_en_resolve_app_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTURADOR_APP_DATA", str(tmp_path / "custom"))
    assert resolve_app_data_root() == tmp_path / "custom"


def test_facturador_app_data_relativo_rechazado(monkeypatch):
    monkeypatch.setenv("FACTURADOR_APP_DATA", "relative/path")
    with pytest.raises(ProfileError, match="FACTURADOR_APP_DATA"):
        resolve_app_data_root()


# --- FAC-29: chooser de ambiente --------------------------------------------


_FORBIDDEN_CHOOSER_TERMS = (
    "docker",
    "profile",
    "perfil",
    "FACTURADOR_HOME",
    "FACTURADOR_APP_DATA",
    "ARCA_ENV",
    ".env",
    "sqlite",
    "uvicorn",
    "localhost",
    "127.0.0.1",
    "secrets/",
    "cert.crt",
    "cert.key",
    "/homo",
    "/prod",
)


def test_chooser_options_lenguaje_de_negocio_sin_jerga_interna(tmp_path):
    options = environment_options()
    assert options is ENVIRONMENT_OPTIONS
    assert len(options) == 2

    by_env = {opt.environment: opt for opt in options}
    assert by_env[ArcaEnvironment.HOMO].title == "Homologación"
    assert by_env[ArcaEnvironment.PROD].title == "Producción"
    # Títulos alineados con el display_name del perfil.
    for environment, option in by_env.items():
        profile = EnvironmentProfile.for_testing(
            environment, tmp_path / environment.value
        )
        assert option.title == profile.display_name

    homo_desc = by_env[ArcaEnvironment.HOMO].description.lower()
    prod_desc = by_env[ArcaEnvironment.PROD].description.lower()
    assert "prueba" in homo_desc or "seguro" in homo_desc
    assert "valor fiscal" in homo_desc
    assert "fiscal" in prod_desc
    assert "real" in prod_desc

    visible = " ".join(
        f"{opt.title} {opt.description}" for opt in options
    ).lower()
    for term in _FORBIDDEN_CHOOSER_TERMS:
        assert term.lower() not in visible, term


def test_prompt_environment_tty_selecciona_por_numero():
    outputs: list[str] = []
    answers = iter(["1"])

    chosen = prompt_environment_tty(
        ENVIRONMENT_OPTIONS,
        input_fn=lambda _prompt: next(answers),
        print_fn=lambda *args, **_kwargs: outputs.append(" ".join(map(str, args))),
    )

    assert chosen is ArcaEnvironment.HOMO
    joined = "\n".join(outputs)
    assert "Homologación" in joined
    assert "Producción" in joined
    assert "prueba" in joined.lower() or "seguro" in joined.lower()
    assert "fiscal" in joined.lower()
    for term in ("docker", "ARCA_ENV", "FACTURADOR_HOME", ".env"):
        assert term.lower() not in joined.lower()


def test_prompt_environment_tty_cancelar_con_enter():
    chosen = prompt_environment_tty(
        ENVIRONMENT_OPTIONS,
        input_fn=lambda _prompt: "",
        print_fn=lambda *_args, **_kwargs: None,
    )
    assert chosen is None


def test_prompt_environment_tty_acepta_nombre_de_negocio():
    answers = iter(["producción"])
    chosen = prompt_environment_tty(
        ENVIRONMENT_OPTIONS,
        input_fn=lambda _prompt: next(answers),
        print_fn=lambda *_args, **_kwargs: None,
    )
    assert chosen is ArcaEnvironment.PROD


def test_choose_environment_force_tty_usa_prompt_tty():
    called: list[object] = []

    def _tty(options):
        called.append(options)
        return ArcaEnvironment.PROD

    def _gui(_options):
        raise AssertionError("no debería abrir GUI con force_tty")

    assert (
        choose_environment(force_tty=True, prompt_tty=_tty, prompt_gui=_gui)
        is ArcaEnvironment.PROD
    )
    assert called == [ENVIRONMENT_OPTIONS]


def test_prompt_environment_gui_selecciona_homologacion(monkeypatch):
    """Humo de la ventana: programa un click y verifica el ambiente elegido."""
    pytest.importorskip("tkinter")
    import tkinter
    from tkinter import ttk

    from facturador.launcher.chooser import prompt_environment_gui

    original_mainloop = tkinter.Tk.mainloop

    def _auto_select(self: tkinter.Tk) -> None:
        def _walk(widget: tkinter.Misc):
            yield widget
            for child in widget.winfo_children():
                yield from _walk(child)

        def _click() -> None:
            for widget in _walk(self):
                if isinstance(widget, ttk.Button):
                    text = str(widget.cget("text"))
                    if text.startswith("Abrir Homologación"):
                        widget.invoke()
                        return

        self.after(50, _click)
        original_mainloop(self)

    monkeypatch.setattr(tkinter.Tk, "mainloop", _auto_select)
    try:
        root_probe = tkinter.Tk()
        root_probe.destroy()
    except tkinter.TclError as exc:
        pytest.skip(f"display no disponible para tkinter: {exc}")

    assert prompt_environment_gui(ENVIRONMENT_OPTIONS) is ArcaEnvironment.HOMO


def test_main_sin_env_usa_chooser_y_arranca_solo_ese_perfil():
    from facturador.launcher.__main__ import main

    started: list[ArcaEnvironment] = []

    class _FakeSupervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment
            self.process = None
            self.is_running = False

        def start(self):
            started.append(self.environment)
            from facturador.launcher.command import plan_backend_launch
            from facturador.launcher.supervisor import LaunchResult

            plan = plan_backend_launch(self.environment, port=8399)
            return LaunchResult(plan=plan, reused=True)

        def stop(self):
            return None

    code = main(
        [],
        choose=lambda: ArcaEnvironment.HOMO,
        supervisor_factory=_FakeSupervisor,
    )
    assert code == 0
    assert started == [ArcaEnvironment.HOMO]


def test_main_con_env_no_abre_chooser():
    from facturador.launcher.__main__ import main

    class _FakeSupervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment
            self.process = None
            self.is_running = False

        def start(self):
            from facturador.launcher.command import plan_backend_launch
            from facturador.launcher.supervisor import LaunchResult

            return LaunchResult(
                plan=plan_backend_launch(self.environment, port=8399),
                reused=True,
            )

    def _boom_chooser():
        raise AssertionError("con --env no debe abrirse el chooser")

    code = main(
        ["--env", "prod"],
        choose=_boom_chooser,
        supervisor_factory=_FakeSupervisor,
    )
    assert code == 0


def test_main_chooser_cancel_sale_cero():
    from facturador.launcher.__main__ import main

    def _boom_factory(**_kwargs):
        raise AssertionError("cancelar no debe crear supervisor")

    assert main([], choose=lambda: None, supervisor_factory=_boom_factory) == 0


def test_main_startup_error_vuelve_al_chooser():
    """Tras un fallo de arranque el chooser sigue usable (segunda elección)."""
    from facturador.launcher.__main__ import main

    picks = iter([ArcaEnvironment.PROD, ArcaEnvironment.HOMO])
    attempts: list[ArcaEnvironment] = []

    class _FakeSupervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment
            self.process = None
            self.is_running = False

        def start(self):
            attempts.append(self.environment)
            if self.environment is ArcaEnvironment.PROD:
                raise LauncherError(
                    "No se pudo iniciar el backend de Producción: sin certificados."
                )
            from facturador.launcher.command import plan_backend_launch
            from facturador.launcher.supervisor import LaunchResult

            return LaunchResult(
                plan=plan_backend_launch(self.environment, port=8399),
                reused=True,
            )

    code = main(
        [],
        choose=lambda: next(picks),
        supervisor_factory=_FakeSupervisor,
        report_failure=lambda _msg: None,
    )
    assert code == 0
    assert attempts == [ArcaEnvironment.PROD, ArcaEnvironment.HOMO]


def test_main_startup_error_con_env_no_reintenta():
    from facturador.launcher.__main__ import main

    class _FakeSupervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment

        def start(self):
            raise LauncherError("fallo de arranque")

    code = main(
        ["--env", "homo"],
        choose=lambda: (_ for _ in ()).throw(AssertionError("no chooser")),
        supervisor_factory=_FakeSupervisor,
        report_failure=lambda _msg: None,
    )
    assert code == 1


# --- Supervisor: fallos visibles y humo launcher→backend --------------------


def test_supervisor_falla_si_el_backend_muere_antes_de_ready(tmp_path):
    """Sin certificados el backend aborta: el error es LauncherError visible."""
    opened: list[str] = []
    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=tmp_path / "appdata",
        home=tmp_path / "home",
        readiness_timeout=15.0,
        open_browser=True,
        browser_opener=opened.append,
    )

    with pytest.raises(LauncherError, match="terminó antes de quedar listo"):
        supervisor.start()

    assert opened == []
    assert not supervisor.is_running


def test_supervisor_timeout_de_readiness_es_visible(tmp_path):
    opened: list[str] = []
    # Proceso que vive pero nunca sirve /health.
    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=tmp_path / "appdata",
        home=tmp_path / "home",
        readiness_timeout=0.6,
        open_browser=True,
        browser_opener=opened.append,
        command_override=[sys.executable, "-c", "import time; time.sleep(30)"],
    )

    with pytest.raises(LauncherError, match="no respondió"):
        supervisor.start()

    assert opened == []
    assert not supervisor.is_running


def test_supervisor_ctrl_c_durante_readiness_no_deja_huerfano(tmp_path, monkeypatch):
    """Ctrl+C en la espera de readiness debe apagar el hijo (PR review)."""
    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=tmp_path / "appdata",
        home=tmp_path / "home",
        readiness_timeout=30.0,
        open_browser=True,
        browser_opener=lambda _url: None,
        command_override=[sys.executable, "-c", "import time; time.sleep(60)"],
    )

    def _interrupt(_plan):
        raise KeyboardInterrupt

    monkeypatch.setattr(supervisor, "_wait_until_ready", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        supervisor.start()

    assert not supervisor.is_running
    assert not supervisor.is_ready


def test_supervisor_browser_opener_falla_sin_tumbar_backend(
    tmp_path, test_cert_and_key
):
    """Fallo al abrir el browser no debe detener un backend ya listo."""

    def _boom(_url: str) -> None:
        raise RuntimeError("browser boom")

    app_data = tmp_path / "appdata"
    _perfil_con_certs(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=app_data,
        home=tmp_path / "home",
        readiness_timeout=30.0,
        open_browser=True,
        browser_opener=_boom,
    )
    try:
        result = supervisor.start()
        plan = result.plan
        assert not result.reused
        assert supervisor.is_ready
        with urllib.request.urlopen(plan.health_url, timeout=2) as response:
            assert response.status == 200
    finally:
        supervisor.stop()
    assert not supervisor.is_running


@pytest.mark.parametrize("bad_port", ["0", "99999", "nope"])
def test_main_puerto_invalido_sin_traceback(bad_port):
    """``--port`` fuera de rango / no numérico: SystemExit de argparse, no stack."""
    from facturador.launcher.__main__ import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--env", "homo", f"--port={bad_port}"])
    assert excinfo.value.code == 2


def test_plan_usa_default_port_compartido():
    from facturador.constants import DEFAULT_PORT
    from facturador.launcher.command import plan_backend_launch

    plan = plan_backend_launch(ArcaEnvironment.HOMO)
    assert plan.port == DEFAULT_PORT
    assert ProcessSupervisor(environment=ArcaEnvironment.HOMO).port == DEFAULT_PORT


@pytest.mark.parametrize("environment", list(ArcaEnvironment))
def test_smoke_launcher_arranca_backend_abre_browser_y_apaga(
    environment, tmp_path, test_cert_and_key
):
    """Humo launcher→backend: un solo perfil, browser post-ready, stop limpio."""
    app_data = tmp_path / "appdata"
    home = tmp_path / "home"
    _perfil_con_certs(environment, app_data, test_cert_and_key)
    port = _free_port()
    opened: list[str] = []

    supervisor = ProcessSupervisor(
        environment=environment,
        port=port,
        app_data_root=app_data,
        home=home,
        readiness_timeout=30.0,
        open_browser=True,
        browser_opener=opened.append,
    )

    try:
        result = supervisor.start()
        plan = result.plan
        assert not result.reused
        assert plan.environment is environment
        assert plan.env["ARCA_ENV"] == environment.value
        assert supervisor.is_ready
        assert opened == [f"http://127.0.0.1:{port}/"]
        # Lock de perfil tomado mientras corre; liberado en stop().
        assert plan.profile.paths.launcher_lock.is_file()

        with urllib.request.urlopen(plan.health_url, timeout=2) as response:
            payload = json.loads(response.read().decode())
        assert payload == {"status": "ok", "environment": environment.value}
    finally:
        pid = supervisor.process.pid if supervisor.process else None
        lock_path = supervisor.plan.profile.paths.launcher_lock
        supervisor.stop()

    assert not supervisor.is_running
    # Lock liberado (flock); el archivo puede quedar como leftover inofensivo.
    assert lock_path.is_file()
    if pid is not None:
        # El proceso no debe quedar huérfano tras stop().
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
            still_alive = True
        except OSError:
            still_alive = False
        assert not still_alive


def test_smoke_browser_no_abre_si_open_browser_false(tmp_path, test_cert_and_key):
    app_data = tmp_path / "appdata"
    _perfil_con_certs(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    opened: list[str] = []
    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=app_data,
        home=tmp_path / "home",
        readiness_timeout=30.0,
        open_browser=False,
        browser_opener=opened.append,
    )
    try:
        supervisor.start()
        assert opened == []
    finally:
        supervisor.stop()


# --- FAC-30: lock de perfil / anti-duplicado ---------------------------------


def test_profile_lock_acquire_release_y_metadata(tmp_path):
    from facturador.launcher import ProfileLock, read_lock_holder

    path = tmp_path / "data" / "launcher.lock"
    lock = ProfileLock(path)
    holder = lock.acquire(port=8399, environment="homo")

    assert lock.is_held
    assert holder.port == 8399
    assert holder.environment == "homo"
    assert holder.pid == os.getpid()
    assert read_lock_holder(path) == holder
    # Sentinel en byte 0; JSON legible sin tomar el lock.
    raw = path.read_bytes()
    assert raw[:1] == b"\0"
    assert b'"port": 8399' in raw or b'"port":8399' in raw

    lock.release()
    assert not lock.is_held
    # El archivo queda: flock es la autoridad; unlink post-unlock es racy.
    assert path.is_file()
    assert read_lock_holder(path) == holder


def test_read_lock_holder_acepta_json_legacy_sin_sentinel(tmp_path):
    """Archivos pre-sentinel (solo JSON) siguen siendo legibles."""
    from facturador.launcher import read_lock_holder

    path = tmp_path / "data" / "launcher.lock"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"v":1,"pid":42,"port":8399,"environment":"prod"}\n',
        encoding="utf-8",
    )
    holder = read_lock_holder(path)
    assert holder is not None
    assert holder.pid == 42
    assert holder.port == 8399
    assert holder.environment == "prod"


def test_read_lock_holder_no_lee_byte_sentinel_bloqueado(tmp_path, monkeypatch):
    """Simula mandatory lock en byte 0: el reader debe seek(1) antes de read."""
    from facturador.launcher import read_lock_holder

    path = tmp_path / "data" / "launcher.lock"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b'\0{"v":1,"pid":7,"port":8410,"environment":"homo"}\n'
    )

    original_open = Path.open

    def guarded_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        fh = original_open(self, mode, *args, **kwargs)
        if self.resolve() != path.resolve():
            return fh
        if "b" not in mode:
            fh.close()
            raise AssertionError("read_lock_holder debe abrir en binario")
        inner_read = fh.read

        def read_guard(size: int = -1) -> bytes:
            if fh.tell() == 0:
                raise OSError("byte 0 locked (simulated Windows mandatory lock)")
            return inner_read(size)

        fh.read = read_guard  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr(Path, "open", guarded_open)
    holder = read_lock_holder(path)
    assert holder is not None
    assert holder.pid == 7
    assert holder.port == 8410
    assert holder.environment == "homo"


def test_profile_lock_release_no_borra_archivo_para_evitar_race(tmp_path):
    """Tras release el path sigue existiendo; un segundo acquire toma el flock."""
    from facturador.launcher import ProfileLock, read_lock_holder

    path = tmp_path / "data" / "launcher.lock"
    first = ProfileLock(path)
    first.acquire(port=8601, environment="homo")
    first.release()

    assert path.is_file()
    leftover = read_lock_holder(path)
    assert leftover is not None
    assert leftover.port == 8601

    second = ProfileLock(path)
    holder = second.acquire(port=8602, environment="homo")
    assert holder.port == 8602
    assert holder.pid == os.getpid()
    second.release()


def test_profile_lock_segundo_proceso_no_puede_adquirir(tmp_path):
    """Dos procesos: el segundo ve ProfileLockHeld con el puerto del primero."""
    import subprocess

    from facturador.launcher import ProfileLock, ProfileLockHeld

    path = tmp_path / "data" / "launcher.lock"
    holder_script = f"""
import time
from pathlib import Path
from facturador.launcher import ProfileLock
lock = ProfileLock(Path({str(path)!r}))
lock.acquire(port=8411, environment="homo")
print("LOCKED", flush=True)
time.sleep(60)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "LOCKED"

        lock2 = ProfileLock(path)
        with pytest.raises(ProfileLockHeld) as excinfo:
            lock2.acquire(port=8412, environment="homo")
        assert excinfo.value.holder.port == 8411
        assert excinfo.value.holder.environment == "homo"
        assert "ya está en marcha" in str(excinfo.value)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_profile_lock_stale_file_sin_flock_se_recupera(tmp_path):
    """Archivo leftover sin flock (crash): el siguiente acquire debe ganar."""
    from facturador.launcher import ProfileLock, read_lock_holder

    path = tmp_path / "data" / "launcher.lock"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"v":1,"pid":999999,"port":8399,"environment":"homo"}\n',
        encoding="utf-8",
    )
    assert read_lock_holder(path) is not None

    lock = ProfileLock(path)
    holder = lock.acquire(port=8420, environment="homo")
    assert holder.port == 8420
    assert holder.pid == os.getpid()
    lock.release()
    assert path.is_file()
    # Tras soltar, otro acquire debe poder tomar el mismo path.
    lock2 = ProfileLock(path)
    holder2 = lock2.acquire(port=8421, environment="homo")
    assert holder2.port == 8421
    lock2.release()


def test_profile_locks_de_distintos_ambientes_son_independientes(tmp_path):
    from facturador.launcher import ProfileLock

    homo = ProfileLock(tmp_path / "homo" / "data" / "launcher.lock")
    prod = ProfileLock(tmp_path / "prod" / "data" / "launcher.lock")
    try:
        homo.acquire(port=8501, environment="homo")
        prod.acquire(port=8502, environment="prod")
        assert homo.is_held and prod.is_held
    finally:
        homo.release()
        prod.release()


def test_segundo_launcher_reutiliza_sesion_sana(tmp_path, test_cert_and_key):
    """Misma perfil + /health OK → reabre browser, no arranca otro backend."""
    app_data = tmp_path / "appdata"
    home = tmp_path / "home"
    _perfil_con_certs(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    port = _free_port()
    opened_first: list[str] = []
    opened_second: list[str] = []

    first = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=port,
        app_data_root=app_data,
        home=home,
        readiness_timeout=30.0,
        open_browser=True,
        browser_opener=opened_first.append,
    )
    second = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=port,
        app_data_root=app_data,
        home=home,
        readiness_timeout=5.0,
        open_browser=True,
        browser_opener=opened_second.append,
    )
    try:
        result1 = first.start()
        assert not result1.reused
        assert first.is_running
        first_pid = first.process.pid if first.process else None

        result2 = second.start()
        assert result2.reused
        assert result2.plan.port == port
        assert second.was_reused
        assert not second.is_running  # no spawneó un segundo backend
        assert opened_second == [f"http://127.0.0.1:{port}/"]
        # El primero sigue siendo el dueño del proceso.
        assert first.is_running
        assert first.process is not None and first.process.pid == first_pid
    finally:
        second.stop()
        first.stop()


def test_reuso_actualiza_plan_del_supervisor_si_puerto_difiere(
    tmp_path, test_cert_and_key
):
    """Tras reuse, supervisor.plan/base_url apuntan al puerto de la sesión."""
    app_data = tmp_path / "appdata"
    home = tmp_path / "home"
    _perfil_con_certs(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    live_port = _free_port()
    other_port = _free_port()
    assert live_port != other_port

    first = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=live_port,
        app_data_root=app_data,
        home=home,
        readiness_timeout=30.0,
        open_browser=False,
    )
    second = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=other_port,
        app_data_root=app_data,
        home=home,
        readiness_timeout=5.0,
        open_browser=False,
    )
    try:
        first.start()
        result = second.start()
        assert result.reused
        assert result.plan.port == live_port
        # Callers que solo miran el supervisor (no LaunchResult) también.
        assert second.plan.port == live_port
        assert second.base_url == f"http://127.0.0.1:{live_port}"
        assert second.plan.health_url == f"http://127.0.0.1:{live_port}/health"
    finally:
        second.stop()
        first.stop()


def test_segundo_launcher_falla_claro_si_lock_vivo_sin_health(tmp_path):
    """Lock tomado pero /health caído → LauncherError visible, sin segundo backend."""
    import subprocess

    app_data = tmp_path / "appdata"
    profile = resolve_launch_profile(ArcaEnvironment.HOMO, app_data_root=app_data)
    profile.paths.ensure_layout()
    lock_path = profile.paths.launcher_lock
    port = _free_port()

    holder_script = f"""
import time
from pathlib import Path
from facturador.launcher import ProfileLock
lock = ProfileLock(Path({str(lock_path)!r}))
lock.acquire(port={port}, environment="homo")
print("LOCKED", flush=True)
time.sleep(60)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "LOCKED"

        opened: list[str] = []
        supervisor = ProcessSupervisor(
            environment=ArcaEnvironment.HOMO,
            port=port,
            app_data_root=app_data,
            home=tmp_path / "home",
            readiness_timeout=2.0,
            open_browser=True,
            browser_opener=opened.append,
        )
        with pytest.raises(LauncherError, match="ya está en marcha"):
            supervisor.start()
        assert opened == []
        assert not supervisor.is_running
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_supervisor_recupera_lock_stale_y_arranca(
    tmp_path, test_cert_and_key
):
    """Leftover launcher.lock sin flock no bloquea un arranque limpio."""
    app_data = tmp_path / "appdata"
    profile = _perfil_con_certs(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    stale = profile.paths.launcher_lock
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        '{"v":1,"pid":1,"port":1,"environment":"homo"}\n',
        encoding="utf-8",
    )

    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=app_data,
        home=tmp_path / "home",
        readiness_timeout=30.0,
        open_browser=False,
    )
    try:
        result = supervisor.start()
        assert not result.reused
        assert supervisor.is_ready
    finally:
        supervisor.stop()
    # Leftover file OK; lo importante es que el flock quedó libre y arrancamos.
    assert stale.is_file()


def test_spawn_falla_libera_lock_para_reintentar(tmp_path):
    """Si ``_spawn`` falla tras tomar el lock, el lock se suelta (retry OK)."""
    app_data = tmp_path / "appdata"
    profile = resolve_launch_profile(ArcaEnvironment.HOMO, app_data_root=app_data)
    profile.paths.ensure_layout()
    lock_path = profile.paths.launcher_lock

    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=app_data,
        home=tmp_path / "home",
        readiness_timeout=2.0,
        open_browser=False,
        # Comando imposible: _spawn → OSError → LauncherError.
        command_override=["/nonexistent/facturador-backend-bin"],
    )
    with pytest.raises(LauncherError, match="No se pudo iniciar"):
        supervisor.start()
    assert not supervisor.is_running
    assert supervisor._lock is None

    # Otro acquire en el mismo proceso debe poder tomar el flock.
    from facturador.launcher import ProfileLock

    lock = ProfileLock(lock_path)
    lock.acquire(port=8999, environment="homo")
    lock.release()


def test_backend_huerfano_sano_falla_claro_sin_segundo_proceso(
    tmp_path, monkeypatch
):
    """Flock libre + /health OK (launcher muerto): no spawnea; mensaje claro."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    app_data = tmp_path / "appdata"
    profile = resolve_launch_profile(ArcaEnvironment.HOMO, app_data_root=app_data)
    profile.paths.ensure_layout()
    orphan_port = _free_port()

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok","environment":"homo"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", orphan_port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    from facturador.launcher import ProfileLock, read_lock_holder

    # Simular leftover tras muerte del launcher: metadata sin flock.
    seed = ProfileLock(profile.paths.launcher_lock)
    seed.acquire(port=orphan_port, environment="homo")
    seed.release()
    assert read_lock_holder(profile.paths.launcher_lock) is not None

    spawned: list[object] = []

    def _no_spawn(self: ProcessSupervisor, plan: object) -> object:
        spawned.append(plan)
        raise AssertionError("no debería spawnear con huérfano sano")

    monkeypatch.setattr(ProcessSupervisor, "_spawn", _no_spawn)

    supervisor = ProcessSupervisor(
        environment=ArcaEnvironment.HOMO,
        port=_free_port(),
        app_data_root=app_data,
        home=tmp_path / "home",
        readiness_timeout=5.0,
        open_browser=False,
    )
    try:
        with pytest.raises(LauncherError, match="sin un launcher activo"):
            supervisor.start()
        assert spawned == []
        assert not supervisor.is_running
        # Metadata del huérfano restaurado para el próximo intento.
        leftover = read_lock_holder(profile.paths.launcher_lock)
        assert leftover is not None
        assert leftover.port == orphan_port
    finally:
        server.shutdown()
        server.server_close()


# --- FAC-32: cambio de ambiente por reinicio ---------------------------------


def test_change_environment_request_write_read_clear(tmp_path):
    from facturador.launcher import (
        clear_change_environment_request,
        read_change_environment_request,
        write_change_environment_request,
    )
    from facturador.profile import ProfilePaths

    paths = ProfilePaths(root=tmp_path / "homo")
    paths.ensure_layout()
    assert read_change_environment_request(paths) is None

    write_change_environment_request(paths, ArcaEnvironment.HOMO)
    req = read_change_environment_request(paths)
    assert req is not None
    assert req.from_environment is ArcaEnvironment.HOMO
    assert req.requested_at > 0

    clear_change_environment_request(paths)
    assert read_change_environment_request(paths) is None


def test_handle_change_cancel_mantiene_backend_sin_stop(tmp_path):
    """Cancelar el chooser limpia el pedido y no detiene el backend actual."""
    from facturador.launcher.__main__ import _handle_change_environment_request
    from facturador.launcher.switch import (
        read_change_environment_request,
        write_change_environment_request,
    )
    from facturador.profile import ProfilePaths

    paths = ProfilePaths(root=tmp_path / "homo")
    paths.ensure_layout()
    write_change_environment_request(paths, ArcaEnvironment.HOMO)

    stopped: list[str] = []

    class _Fake:
        plan = type(
            "P",
            (),
            {
                "profile": type(
                    "Pr",
                    (),
                    {"paths": paths, "display_name": "Homologación"},
                )(),
            },
        )()
        open_browser = False
        base_url = "http://127.0.0.1:8399"
        browser_opener = lambda _url: None  # noqa: E731

        def poll_change_environment_request(self):
            return read_change_environment_request(paths)

        def clear_change_environment_request(self):
            from facturador.launcher.switch import clear_change_environment_request

            clear_change_environment_request(paths)

        def stop(self):
            stopped.append("stop")

    result = _handle_change_environment_request(
        _Fake(),  # type: ignore[arg-type]
        current=ArcaEnvironment.HOMO,
        choose=lambda: None,
    )
    assert result is None
    assert stopped == []
    assert read_change_environment_request(paths) is None


def test_handle_change_confirma_otro_ambiente_stop_antes_de_switch(tmp_path):
    """Al confirmar el otro ambiente, stop() corre antes de devolver SwitchTo."""
    from facturador.launcher.__main__ import SwitchTo, _handle_change_environment_request
    from facturador.launcher.switch import write_change_environment_request
    from facturador.profile import ProfilePaths

    paths = ProfilePaths(root=tmp_path / "homo")
    paths.ensure_layout()
    write_change_environment_request(paths, ArcaEnvironment.HOMO)

    order: list[str] = []

    class _Fake:
        plan = type(
            "P",
            (),
            {
                "profile": type(
                    "Pr",
                    (),
                    {"paths": paths, "display_name": "Homologación"},
                )(),
            },
        )()
        open_browser = False
        base_url = "http://127.0.0.1:8399"

        def poll_change_environment_request(self):
            from facturador.launcher.switch import read_change_environment_request

            return read_change_environment_request(paths)

        def clear_change_environment_request(self):
            from facturador.launcher.switch import clear_change_environment_request

            clear_change_environment_request(paths)
            order.append("clear")

        def stop(self):
            order.append("stop")

    result = _handle_change_environment_request(
        _Fake(),  # type: ignore[arg-type]
        current=ArcaEnvironment.HOMO,
        choose=lambda: ArcaEnvironment.PROD,
    )
    assert isinstance(result, SwitchTo)
    assert result.environment is ArcaEnvironment.PROD
    # Pedido consumido y backend detenido antes de arrancar el otro perfil.
    assert order == ["clear", "stop"]


def test_handle_change_mismo_ambiente_no_reinicia(tmp_path):
    from facturador.launcher.__main__ import _handle_change_environment_request
    from facturador.launcher.switch import (
        read_change_environment_request,
        write_change_environment_request,
    )
    from facturador.profile import ProfilePaths

    paths = ProfilePaths(root=tmp_path / "prod")
    paths.ensure_layout()
    write_change_environment_request(paths, ArcaEnvironment.PROD)
    opened: list[str] = []
    stopped: list[str] = []

    class _Fake:
        plan = type(
            "P",
            (),
            {
                "profile": type(
                    "Pr",
                    (),
                    {"paths": paths, "display_name": "Producción"},
                )(),
            },
        )()
        open_browser = True
        base_url = "http://127.0.0.1:8410"
        browser_opener = opened.append

        def poll_change_environment_request(self):
            return read_change_environment_request(paths)

        def clear_change_environment_request(self):
            from facturador.launcher.switch import clear_change_environment_request

            clear_change_environment_request(paths)

        def stop(self):
            stopped.append("stop")

    result = _handle_change_environment_request(
        _Fake(),  # type: ignore[arg-type]
        current=ArcaEnvironment.PROD,
        choose=lambda: ArcaEnvironment.PROD,
    )
    assert result is None
    assert stopped == []
    assert opened == ["http://127.0.0.1:8410/"]


def test_main_switch_stop_antes_de_arrancar_destino_y_fallo_vuelve_chooser():
    """Flujo FAC-32: stop del actual → start del nuevo; fallo vuelve al chooser."""
    from facturador.launcher.__main__ import main
    from facturador.launcher.command import plan_backend_launch
    from facturador.launcher.supervisor import LaunchResult

    events: list[tuple[str, str]] = []
    # 1ª = arranque homo; 2ª = chooser del cambio → prod; 3ª = tras fallo → homo.
    picks = iter(
        [ArcaEnvironment.HOMO, ArcaEnvironment.PROD, ArcaEnvironment.HOMO]
    )
    session_n = {"homo": 0}

    class _Proc:
        returncode = 0

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 1)

    class _FakeSupervisor:
        def __init__(self, *, environment, **_kwargs):
            self.environment = environment
            self.process = _Proc()
            self.is_running = True
            self.open_browser = False
            self.browser_opener = lambda _url: None
            self._plan = plan_backend_launch(environment, port=8399)
            self._polled = False
            if environment is ArcaEnvironment.HOMO:
                session_n["homo"] += 1
                self._session = session_n["homo"]
            else:
                self._session = 0

        @property
        def plan(self):
            return self._plan

        @property
        def base_url(self):
            return self._plan.base_url

        def start(self):
            events.append(("start", self.environment.value))
            if self.environment is ArcaEnvironment.PROD:
                self.is_running = False
                raise LauncherError(
                    "No se pudo iniciar el backend de Producción: sin certificados."
                )
            # Segunda sesión homo: sesión "reused" → main sale 0 sin wait loop.
            if self._session >= 2:
                return LaunchResult(plan=self._plan, reused=True)
            return LaunchResult(plan=self._plan, reused=False)

        def poll_change_environment_request(self):
            from facturador.launcher.switch import ChangeEnvironmentRequest

            if (
                self.environment is ArcaEnvironment.HOMO
                and self._session == 1
                and not self._polled
            ):
                self._polled = True
                return ChangeEnvironmentRequest(
                    from_environment=ArcaEnvironment.HOMO,
                    requested_at=1.0,
                )
            return None

        def clear_change_environment_request(self):
            events.append(("clear", self.environment.value))

        def stop(self):
            events.append(("stop", self.environment.value))
            self.is_running = False

    code = main(
        [],
        choose=lambda: next(picks),
        supervisor_factory=_FakeSupervisor,
        report_failure=lambda _msg: None,
    )
    assert code == 0
    assert events == [
        ("start", "homo"),
        ("clear", "homo"),
        ("stop", "homo"),
        ("start", "prod"),
        ("start", "homo"),
    ]


def test_no_hot_switch_en_api_de_pedido(tmp_path, test_cert_and_key, monkeypatch):
    """El endpoint solo escribe el pedido; Config/perfil quedan inmutables."""
    import httpx
    from fastapi.testclient import TestClient

    from facturador import db
    from facturador.api import create_app
    from facturador.arca.wsfex import WsfexClient
    from facturador.config import Config
    from facturador.launcher.switch import (
        LAUNCHER_SUPERVISED_ENV,
        LAUNCHER_SUPERVISED_VALUE,
        read_change_environment_request,
    )
    from facturador.profile import EnvironmentProfile
    from tests.arca_fake import FakeArca, FakeWsaa
    from tests.conftest import seed_params, seed_settings

    monkeypatch.setenv(LAUNCHER_SUPERVISED_ENV, LAUNCHER_SUPERVISED_VALUE)
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "profile-homo"
    )
    cert_pem, key_pem = test_cert_and_key
    profile.paths.ensure_layout()
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    conn = db.connect(profile.paths.db)
    seed_params(conn)
    seed_settings(conn, ambiente="homo")
    arca = FakeArca()
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(profile, config=config, conn=conn, wsfex=wsfex)
    client = TestClient(app)

    # Config frozen: no hay setter de ambiente.
    with pytest.raises(Exception):
        config.env = ArcaEnvironment.PROD  # type: ignore[misc]

    before_env = app.state.profile.environment
    before_service_env = app.state.service.config.env
    before_wsfex_id = id(app.state.service.wsfex)
    before_conn_id = id(app.state.service.conn)

    r = client.post("/ui/cambiar-ambiente")
    assert r.status_code == 200
    assert "no se tocan" in r.text.lower() or "no cambia en caliente" in r.text.lower()

    assert app.state.profile.environment is before_env is ArcaEnvironment.HOMO
    assert app.state.service.config.env is before_service_env is ArcaEnvironment.HOMO
    assert id(app.state.service.wsfex) == before_wsfex_id
    assert id(app.state.service.conn) == before_conn_id
    req = read_change_environment_request(profile.paths)
    assert req is not None
    assert req.from_environment is ArcaEnvironment.HOMO

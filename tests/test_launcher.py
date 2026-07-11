"""Launcher: resolución de comando/perfil y supervisión de proceso (FAC-28)."""

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
    LauncherError,
    ProcessSupervisor,
    build_backend_command,
    build_backend_env,
    plan_backend_launch,
    resolve_launch_environment,
    resolve_launch_profile,
)
from facturador.profile import ProfileError, resolve_app_data_root


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
    assert not lock_path.exists()
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

    lock.release()
    assert not lock.is_held
    assert not path.exists()


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
    assert not path.exists()


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


def test_segundo_launcher_falla_claro_si_lock_vivo_sin_health(tmp_path):
    """Lock tomado pero /health caído → LauncherError visible, sin segundo backend."""
    import subprocess

    from facturador.launcher import ProfileLock

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
    assert not stale.exists()

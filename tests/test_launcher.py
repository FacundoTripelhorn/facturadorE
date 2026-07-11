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
        plan = supervisor.start()
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
        plan = supervisor.start()
        assert plan.environment is environment
        assert plan.env["ARCA_ENV"] == environment.value
        assert supervisor.is_ready
        assert opened == [f"http://127.0.0.1:{port}/"]

        with urllib.request.urlopen(plan.health_url, timeout=2) as response:
            payload = json.loads(response.read().decode())
        assert payload == {"status": "ok", "environment": environment.value}
    finally:
        pid = supervisor.process.pid if supervisor.process else None
        supervisor.stop()

    assert not supervisor.is_running
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

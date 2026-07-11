"""Supervisor de proceso del launcher (FAC-28, ADR 0001).

Arranca un backend ligado a un único perfil, espera ``GET /health``, abre el
browser solo si quedó listo, y apaga el hijo sin dejarlo huérfano. Sin
chooser (FAC-29) ni locks anti-duplicado (FAC-30): acá solo supervisión.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..constants import DEFAULT_PORT, ArcaEnvironment
from ..profile import EnvironmentProfile
from .command import BackendLaunchPlan, plan_backend_launch

DEFAULT_READINESS_TIMEOUT_S = 60.0
DEFAULT_STOP_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2

_log = logging.getLogger(__name__)


class LauncherError(RuntimeError):
    """Fallo de arranque/parada visible para el usuario (sin jerga interna)."""


@dataclass
class ProcessSupervisor:
    """Supervisa el ciclo de vida de un backend de un solo ambiente."""

    environment: ArcaEnvironment
    port: int = DEFAULT_PORT
    app_data_root: Path | None = None
    home: Path | None = None
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_S
    open_browser: bool = True
    browser_opener: Callable[[str], Any] = field(default=webbrowser.open)
    python: str | None = None
    base_env: Mapping[str, str] | None = None
    # Inyectable en tests: reemplaza ``python -m facturador``.
    command_override: list[str] | None = None

    _plan: BackendLaunchPlan | None = field(default=None, init=False, repr=False)
    _process: subprocess.Popen[bytes] | None = field(
        default=None, init=False, repr=False
    )
    _ready: bool = field(default=False, init=False, repr=False)
    _output_chunks: list[bytes] = field(default_factory=list, init=False, repr=False)
    _output_thread: threading.Thread | None = field(
        default=None, init=False, repr=False
    )

    @property
    def plan(self) -> BackendLaunchPlan:
        if self._plan is None:
            self._plan = plan_backend_launch(
                self.environment,
                port=self.port,
                app_data_root=self.app_data_root,
                home=self.home,
                python=self.python,
                base_env=self.base_env,
            )
            if self.command_override is not None:
                self._plan = BackendLaunchPlan(
                    environment=self._plan.environment,
                    profile=self._plan.profile,
                    command=list(self.command_override),
                    env=self._plan.env,
                    port=self._plan.port,
                )
        return self._plan

    @property
    def profile(self) -> EnvironmentProfile:
        return self.plan.profile

    @property
    def base_url(self) -> str:
        return self.plan.base_url

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def is_ready(self) -> bool:
        return self._ready and self.is_running

    def start(self) -> BackendLaunchPlan:
        """Arranca el backend, espera readiness y opcionalmente abre el browser."""
        if self.is_running:
            raise LauncherError(
                "Ya hay un backend en marcha para este launcher. "
                "Detenerlo antes de volver a iniciar."
            )
        plan = self.plan
        self._ready = False
        self._process = self._spawn(plan)
        try:
            payload = self._wait_until_ready(plan)
            self._assert_ready_environment(payload, plan.environment)
            self._ready = True
        except (LauncherError, KeyboardInterrupt):
            # El hijo vive en su propia session/process group: Ctrl+C solo
            # llega al launcher. Hay que apagarlo acá o queda huérfano.
            self._stop_process(timeout=DEFAULT_STOP_TIMEOUT_S)
            raise
        if self.open_browser:
            try:
                self.browser_opener(f"{plan.base_url}/")
            except Exception:
                # El backend ya está listo: un fallo al abrir el browser no
                # debe tumbar el launcher ni dejar el proceso sin supervisión.
                _log.warning(
                    "No se pudo abrir el navegador; la app ya está en %s/",
                    plan.base_url,
                    exc_info=True,
                )
        return plan

    def stop(self, *, timeout: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        """Apagado limpio: SIGTERM/terminate y kill si no responde."""
        self._ready = False
        self._stop_process(timeout=timeout)

    def __enter__(self) -> ProcessSupervisor:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _spawn(self, plan: BackendLaunchPlan) -> subprocess.Popen[bytes]:
        self._output_chunks = []
        kwargs: dict[str, Any] = {
            "env": plan.env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP permite señalizar sin matar al launcher.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(plan.command, **kwargs)
        except OSError as exc:
            raise LauncherError(
                f"No se pudo iniciar el backend de {plan.profile.display_name}: "
                f"{exc}"
            ) from exc
        # Drenar stdout en background: si el PIPE se llena, el hijo se cuelga
        # y el readiness nunca llega.
        self._output_thread = threading.Thread(
            target=self._pump_output,
            args=(proc,),
            name="facturador-launcher-output",
            daemon=True,
        )
        self._output_thread.start()
        return proc

    def _pump_output(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stdout is None:
            return
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self._output_chunks.append(chunk)
        except (OSError, ValueError):
            return

    def _wait_until_ready(self, plan: BackendLaunchPlan) -> dict[str, Any]:
        assert self._process is not None
        deadline = time.monotonic() + self.readiness_timeout
        last_error = "sin respuesta"
        while time.monotonic() < deadline:
            code = self._process.poll()
            if code is not None:
                detail = self._drain_output()
                raise LauncherError(
                    f"El backend de {plan.profile.display_name} terminó antes "
                    f"de quedar listo (código {code}). {detail}"
                )
            try:
                return self._get_health(plan.health_url)
            except LauncherError as exc:
                last_error = str(exc)
            time.sleep(_POLL_INTERVAL_S)
        self._drain_output()
        raise LauncherError(
            f"El backend de {plan.profile.display_name} no respondió en "
            f"{self.readiness_timeout:.0f} s ({last_error})."
        )

    def _get_health(self, url: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status != 200:
                    raise LauncherError(
                        f"Readiness devolvió HTTP {response.status}"
                    )
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise LauncherError(f"Readiness devolvió HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LauncherError(f"sin respuesta ({exc})") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LauncherError("Readiness devolvió JSON inválido") from exc
        if not isinstance(payload, dict):
            raise LauncherError("Readiness devolvió un cuerpo inesperado")
        return payload

    def _assert_ready_environment(
        self, payload: dict[str, Any], expected: ArcaEnvironment
    ) -> None:
        status = payload.get("status")
        environment = payload.get("environment")
        if status != "ok":
            raise LauncherError(
                f"Readiness no OK (status={status!r})."
            )
        if environment != expected.value:
            raise LauncherError(
                f"El backend arrancó en {environment!r}, pero el launcher "
                f"pidió {expected.value!r}."
            )

    def _stop_process(self, *, timeout: float) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        if proc.poll() is not None:
            self._drain_output(proc)
            return
        try:
            self._signal_stop(proc)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal_kill(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise LauncherError(
                    "No se pudo detener el backend; puede haber quedado "
                    "un proceso huérfano."
                ) from exc
        finally:
            self._drain_output(proc)

    def _signal_stop(self, proc: subprocess.Popen[bytes]) -> None:
        if sys.platform == "win32":
            proc.terminate()
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.terminate()

    def _signal_kill(self, proc: subprocess.Popen[bytes]) -> None:
        if sys.platform == "win32":
            proc.kill()
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()

    def _drain_output(self, proc: subprocess.Popen[bytes] | None = None) -> str:
        del proc  # el pump thread ya acumuló la salida
        if self._output_thread is not None:
            self._output_thread.join(timeout=1.0)
            self._output_thread = None
        if not self._output_chunks:
            return ""
        text = b"".join(self._output_chunks).decode("utf-8", errors="replace").strip()
        self._output_chunks = []
        # Últimas líneas: útiles en el mensaje de error; el log del perfil
        # guarda el detalle completo.
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        tail = " | ".join(lines[-3:])
        return f"Detalle: {tail}"


def start_backend(
    environment: ArcaEnvironment | str,
    **kwargs: Any,
) -> ProcessSupervisor:
    """Atajo: crea el supervisor, arranca y devuelve la instancia lista."""
    if isinstance(environment, str):
        from .command import resolve_launch_environment

        environment = resolve_launch_environment(environment)
    supervisor = ProcessSupervisor(environment=environment, **kwargs)
    supervisor.start()
    return supervisor

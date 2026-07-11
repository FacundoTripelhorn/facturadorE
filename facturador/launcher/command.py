"""Resolución de comando y perfil para el supervisor del launcher (FAC-28).

El launcher elige un ambiente de negocio (homo/prod), resuelve el perfil
oculto correspondiente y arma el comando/entorno con el que arranca el
backend. Un solo ``ARCA_ENV`` viaja al proceso hijo: nunca ambos perfiles.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..constants import DEFAULT_PORT, ArcaEnvironment
from ..profile import EnvironmentProfile, ProfileError, parse_environment


@dataclass(frozen=True)
class BackendLaunchPlan:
    """Plan de arranque: un ambiente, su perfil oculto, argv y entorno hijo."""

    environment: ArcaEnvironment
    profile: EnvironmentProfile
    command: list[str]
    env: dict[str, str]
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"


def resolve_launch_environment(raw: str) -> ArcaEnvironment:
    """Valida el ambiente elegido por el launcher (homo|prod)."""
    try:
        return parse_environment(raw)
    except ProfileError as exc:
        raise ProfileError(
            f"Ambiente de launcher inválido: {raw!r}. "
            "Usar 'homo' (Homologación) o 'prod' (Producción)."
        ) from exc


def resolve_launch_profile(
    environment: ArcaEnvironment,
    *,
    app_data_root: Path | None = None,
) -> EnvironmentProfile:
    """Perfil oculto del ambiente seleccionado (ADR 0001)."""
    return EnvironmentProfile.resolve(environment, app_data_root=app_data_root)


def build_backend_command(*, python: str | None = None) -> list[str]:
    """Argv para el backend de un solo perfil: ``python -m facturador``."""
    return [python or sys.executable, "-m", "facturador"]


def build_backend_env(
    environment: ArcaEnvironment,
    *,
    port: int,
    app_data_root: Path | None = None,
    home: Path | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Entorno del proceso hijo con exactamente un ``ARCA_ENV`` seleccionado.

    - Fija ``ARCA_ENV`` al ambiente elegido (única fuente para el backend).
    - Fija ``FACTURADOR_PORT`` al puerto del launcher.
    - Quita ``FACTURADOR_IN_DOCKER`` para forzar bind a ``127.0.0.1`` en host.
    - Opcionalmente aísla app-data (``FACTURADOR_APP_DATA``) y home de
      bootstrap (tests / arranques controlados).
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["ARCA_ENV"] = environment.value
    env["FACTURADOR_PORT"] = str(port)
    env.pop("FACTURADOR_IN_DOCKER", None)
    if home is not None:
        env["FACTURADOR_HOME"] = str(home)
    if app_data_root is not None:
        # Misma raíz que resolve_launch_profile(): el hijo la lee vía
        # FACTURADOR_APP_DATA (profile.resolve_app_data_root).
        env["FACTURADOR_APP_DATA"] = str(app_data_root)
    return env


def plan_backend_launch(
    environment: ArcaEnvironment,
    *,
    port: int = DEFAULT_PORT,
    app_data_root: Path | None = None,
    home: Path | None = None,
    python: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> BackendLaunchPlan:
    """Resuelve perfil + comando + entorno para un único ambiente."""
    if port < 1 or port > 65535:
        raise ProfileError(f"Puerto inválido para el launcher: {port}")
    profile = resolve_launch_profile(environment, app_data_root=app_data_root)
    return BackendLaunchPlan(
        environment=environment,
        profile=profile,
        command=build_backend_command(python=python),
        env=build_backend_env(
            environment,
            port=port,
            app_data_root=app_data_root,
            home=home,
            base_env=base_env,
        ),
        port=port,
    )

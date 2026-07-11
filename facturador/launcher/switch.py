"""Pedido de cambio de ambiente (FAC-32 / ADR 0001).

El backend no muta el ambiente en proceso: solo escribe un archivo de pedido
en el perfil corriente. El launcher lo detecta, muestra el chooser y — si el
usuario confirma otro ambiente — detiene el backend actual antes de arrancar
el perfil nuevo. Cancelar borra el pedido y deja corriendo la sesión actual.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..constants import ArcaEnvironment
from ..profile import ProfileError, ProfilePaths, parse_environment

CHANGE_ENVIRONMENT_REQUEST_FILENAME = "change-environment.request"

# El launcher marca el hijo para que la UI ofrezca "Cambiar ambiente".
LAUNCHER_SUPERVISED_ENV = "FACTURADOR_LAUNCHER"
LAUNCHER_SUPERVISED_VALUE = "1"


@dataclass(frozen=True)
class ChangeEnvironmentRequest:
    """Pedido persistido: de qué ambiente parte el cambio."""

    from_environment: ArcaEnvironment
    requested_at: float


def change_environment_request_path(paths: ProfilePaths) -> Path:
    """Path del pedido bajo ``data/`` del perfil corriente."""
    return paths.data_dir / CHANGE_ENVIRONMENT_REQUEST_FILENAME


def is_launcher_supervised(
    environ: dict[str, str] | None = None,
) -> bool:
    """True cuando el proceso fue arrancado por el launcher (FAC-28+)."""
    env = os.environ if environ is None else environ
    return env.get(LAUNCHER_SUPERVISED_ENV) == LAUNCHER_SUPERVISED_VALUE


def mark_launcher_supervised(env: dict[str, str]) -> dict[str, str]:
    """Marca el entorno del hijo como supervisado por el launcher."""
    env[LAUNCHER_SUPERVISED_ENV] = LAUNCHER_SUPERVISED_VALUE
    return env


def write_change_environment_request(
    paths: ProfilePaths,
    from_environment: ArcaEnvironment,
) -> Path:
    """Escribe el pedido. No toca Config, clientes ARCA ni la DB."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    path = change_environment_request_path(paths)
    payload = {
        "v": 1,
        "from": from_environment.value,
        "requested_at": time.time(),
    }
    # Escritura atómica: tmp + replace evita lecturas a medias.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_change_environment_request(
    paths: ProfilePaths,
) -> ChangeEnvironmentRequest | None:
    """Lee el pedido si existe y es válido; ``None`` si no hay o está corrupto."""
    path = change_environment_request_path(paths)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    from_raw = payload.get("from")
    if not isinstance(from_raw, str):
        return None
    try:
        environment = parse_environment(from_raw)
    except ProfileError:
        return None
    requested_at = payload.get("requested_at", 0.0)
    try:
        stamp = float(requested_at)
    except (TypeError, ValueError):
        stamp = 0.0
    return ChangeEnvironmentRequest(
        from_environment=environment,
        requested_at=stamp,
    )


def clear_change_environment_request(paths: ProfilePaths) -> None:
    """Borra el pedido (cancelar o tras tomar la decisión)."""
    path = change_environment_request_path(paths)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # El launcher sigue: un leftover se re-limpia en el próximo ciclo.
        return

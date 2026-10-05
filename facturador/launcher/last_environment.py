"""Último ambiente que abrió bien, para preseleccionarlo en el chooser.

Vive en la raíz de app-data (no en un perfil): es del launcher, no de un
ambiente. Se escribe después de que el backend respondió ``/health``. Si el
archivo falta o está corrupto, el chooser preselecciona Homologación.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..constants import ArcaEnvironment
from ..profile import ProfileError, parse_environment, resolve_app_data_root

LAST_ENVIRONMENT_FILENAME = "launcher-last-environment.json"


def last_environment_path(app_data_root: Path | None = None) -> Path:
    """Path del archivo en la raíz de app-data."""
    root = app_data_root or resolve_app_data_root()
    return root / LAST_ENVIRONMENT_FILENAME


def read_last_environment(
    app_data_root: Path | None = None,
) -> ArcaEnvironment | None:
    """El último ambiente guardado; ``None`` si no hay o no se puede leer."""
    try:
        path = last_environment_path(app_data_root)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, ProfileError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("environment")
    if not isinstance(raw, str):
        return None
    try:
        return parse_environment(raw)
    except ProfileError:
        return None


def save_last_environment(
    environment: ArcaEnvironment,
    app_data_root: Path | None = None,
) -> Path:
    """Guarda el ambiente (escritura atómica).

    Raises:
        ProfileError: raíz de app-data inválida.
        OSError: no se pudo escribir.
    """
    path = last_environment_path(app_data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"v": 1, "environment": environment.value}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path

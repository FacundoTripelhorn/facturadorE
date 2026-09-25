"""Confirmación de primer uso de Producción.

El flag vive solo en el perfil ``prod`` (``data/production_ack.json``).
Homologación nunca lo pide ni lo escribe.

- Launcher nativo: diálogo interactivo (ver ``facturador.launcher.production_ack``).
- Backend / Docker: sin ack, el arranque falla salvo
  ``FACTURADOR_ACK_PRODUCTION=1`` (una sola vez; persiste en el perfil).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .constants import ArcaEnvironment
from .profile import EnvironmentProfile, ProfilePaths

ACK_PRODUCTION_ENV = "FACTURADOR_ACK_PRODUCTION"

_TRUTHY = frozenset({"1", "true", "yes", "si", "sí"})


class ProductionAckRequired(RuntimeError):
    """Producción sin ack de validez fiscal (arranque backend / Docker)."""


def production_ack_path(paths: ProfilePaths) -> Path:
    """Path del ack bajo el perfil dado (solo se persiste en prod)."""
    return paths.production_ack


def is_production_acknowledged(paths: ProfilePaths) -> bool:
    """True si el perfil ya tiene un ack de primer uso de Producción."""
    path = production_ack_path(paths)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(raw, dict) and raw.get("acknowledged") is True


def save_production_ack(paths: ProfilePaths) -> None:
    """Persiste el ack solo en el perfil indicado (layout mínimo incluido)."""
    paths.ensure_layout()
    payload = {
        "acknowledged": True,
        "acknowledged_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path = production_ack_path(paths)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def env_requests_production_ack() -> bool:
    """True si el proceso pide grabar el ack vía variable de entorno."""
    raw = os.environ.get(ACK_PRODUCTION_ENV, "").strip().lower()
    return raw in _TRUTHY


def require_production_ack(profile: EnvironmentProfile) -> None:
    """Exige ack de Producción en el arranque del backend (Docker / ``-m facturador``).

    Raises:
        ProductionAckRequired: ambiente prod sin ack y sin env de confirmación.
        OSError: no se pudo persistir el ack tras confirmar por env.
    """
    if profile.environment is not ArcaEnvironment.PROD:
        return
    if is_production_acknowledged(profile.paths):
        return

    # Tests inyectan Config sin pasar por este gate; load_config en pytest
    # no debe bloquear perfiles temporales de Producción.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    if env_requests_production_ack():
        save_production_ack(profile.paths)
        return

    raise ProductionAckRequired(
        "Producción requiere confirmación explícita: los comprobantes "
        "autorizados tienen validez fiscal real. "
        "Nativo: abrí el launcher y confirmá Producción. "
        "Docker / sin launcher: reiniciá una vez con "
        f"{ACK_PRODUCTION_ENV}=1 (queda guardado en el perfil)."
    )

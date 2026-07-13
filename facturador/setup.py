"""Estado de setup por perfil (FAC-35).

Cada perfil oculto (Homologación / Producción) guarda su propio
``data/onboarding.json`` vía ``ProfilePaths.onboarding``. El paso actual se
deriva de hechos del perfil (certificado, emisor activo, puntos de venta) y
se persiste para que un reinicio retome el mismo paso. FAC-37/38 avanzan el
flujo de UI; este módulo solo modela el estado y la readiness.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from .certs import (
    CertificateError,
    read_live_certificate_pair,
    validate_certificate_pair,
)
from .config import key_permissions_ok
from .fiscal_identity import (
    FiscalIdentityError,
    get_sealed_fiscal_cuit,
    resolve_profile_fiscal_cuit,
)
from .profile import EnvironmentProfile, ProfilePaths
from .settings import get_active_emisor_id, load_settings

logger = logging.getLogger(__name__)


class SetupState(StrEnum):
    """Pasos resumibles del primer arranque de un perfil."""

    UNINITIALIZED = "uninitialized"
    CERTIFICATE_REQUIRED = "certificate_required"
    EMISOR_REQUIRED = "emisor_required"
    POINT_OF_SALE_REQUIRED = "point_of_sale_required"
    READY = "ready"


SetupStateProvider = Callable[[], SetupState]


class SetupError(ValueError):
    """Estado de setup inválido o archivo de onboarding corrupto."""


_STATE_KEY = "state"


def is_ready(state: SetupState) -> bool:
    return state is SetupState.READY


def has_certificate_pair(paths: ProfilePaths) -> bool:
    """Par cert/key presente con permisos de key 400/600 (sin validar PEM)."""
    if not paths.cert.is_file() or not paths.key.is_file():
        return False
    return key_permissions_ok(paths.key)


def certificate_pair_is_usable(profile: EnvironmentProfile) -> bool:
    """True si el par del perfil valida (parseable, match, CUIT, vigencia).

    Archivos a mano inválidos o desparejados no desbloquean ``ready``.
    """
    if not has_certificate_pair(profile.paths):
        return False
    try:
        cert_pem, key_pem = read_live_certificate_pair(profile.paths)
        validate_certificate_pair(
            cert_pem, key_pem, environment=profile.environment
        )
    except CertificateError:
        return False
    return True


def load_setup_state(paths: ProfilePaths) -> SetupState:
    """Lee el estado persistido; ``uninitialized`` si el archivo no existe."""
    path = paths.onboarding
    if not path.is_file():
        return SetupState.UNINITIALIZED
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(
            f"No se pudo leer el estado de setup del perfil: {path.name}"
        ) from exc
    if not isinstance(payload, dict):
        raise SetupError("onboarding.json debe ser un objeto JSON")
    value = payload.get(_STATE_KEY)
    if not isinstance(value, str):
        raise SetupError(f"Estado de setup inválido: {value!r}")
    try:
        return SetupState(value)
    except ValueError as exc:
        raise SetupError(f"Estado de setup inválido: {value!r}") from exc


def save_setup_state(paths: ProfilePaths, state: SetupState) -> None:
    """Persiste el paso actual en ``onboarding.json`` del perfil.

    Temp único por llamada (mkstemp): dos reconciles concurrentes no se
    pisan el mismo ``.tmp`` antes del ``replace``.
    """
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {_STATE_KEY: state.value}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, name = tempfile.mkstemp(
        prefix=".onboarding.",
        suffix=".json.tmp",
        dir=paths.data_dir,
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(paths.onboarding)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def evaluate_setup_state(
    profile: EnvironmentProfile,
    conn: sqlite3.Connection,
) -> SetupState:
    """Deriva el paso requerido desde hechos del perfil (sin escribir)."""
    if not certificate_pair_is_usable(profile):
        return SetupState.CERTIFICATE_REQUIRED

    # FAC-39: si hay sello, el cert vivo debe coincidir (sin sellar acá).
    if get_sealed_fiscal_cuit(conn) is not None:
        try:
            resolve_profile_fiscal_cuit(conn, profile)
        except FiscalIdentityError:
            return SetupState.CERTIFICATE_REQUIRED

    active_id = get_active_emisor_id(conn)
    emisor = load_settings(conn).emisor
    if (
        active_id is None
        or not emisor.completo
        or emisor.ambiente != profile.environment.value
    ):
        return SetupState.EMISOR_REQUIRED
    if not emisor.puntos_venta:
        return SetupState.POINT_OF_SALE_REQUIRED
    return SetupState.READY


def reconcile_setup_state(
    profile: EnvironmentProfile,
    conn: sqlite3.Connection,
) -> SetupState:
    """Evalúa hechos, persiste el paso y lo devuelve.

    Un reinicio vuelve a reconciliar y retoma el mismo paso mientras los
    hechos no cambien. Perfiles ya configurados a mano (certs + emisor)
    quedan en ``ready`` sin pasar por la UI de onboarding.

    Si ``onboarding.json`` está corrupto, se ignora y se re-deriva el
    estado desde hechos (nunca 500 por el archivo de estado).
    """
    try:
        load_setup_state(profile.paths)
    except SetupError:
        logger.warning(
            "onboarding.json corrupto en %s; se re-deriva el estado",
            profile.paths.onboarding,
            exc_info=True,
        )
    state = evaluate_setup_state(profile, conn)
    save_setup_state(profile.paths, state)
    return state


def make_setup_state_provider(
    profile: EnvironmentProfile,
    conn: sqlite3.Connection,
) -> SetupStateProvider:
    """Callable para la guardia HTTP: re-reconcilia el perfil en cada llamada.

    Vive acá (no en ``api/app``) para poder testearlo sin montar FastAPI: tras
    instalar certs o completar emisor, la siguiente llamada refleja el nuevo
    paso sin reiniciar el proceso.
    """

    def get_state() -> SetupState:
        return reconcile_setup_state(profile, conn)

    return get_state

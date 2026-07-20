"""Launcher hook for FAC-65 full rebuild (backend must be stopped)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..certs import CertificateError
from ..constants import ArcaEnvironment
from ..fiscal_identity import FiscalIdentityError
from ..profile import EnvironmentProfile, ProfileError
from ..reconstruct import ReconstructError
from ..restore import run_restore
from ..seed_backup import SeedBackupError, seed_archive_path
from ..seed_import import SeedIdentityError
from .lock import LockHolder, ProfileLock, ProfileLockHeld

# Puerto sentinel solo para tomar el flock mientras corre el restore
# (no arranca un backend). Distinto de cualquier listen real.
_RESTORE_LOCK_PORT = 1


def _backend_session_healthy(
    holder: LockHolder, expected: ArcaEnvironment
) -> bool:
    """True si ``holder`` sirve ``/health`` OK en el ambiente esperado.

    Misma semántica que ``ProcessSupervisor._holder_session_healthy`` (FAC-30):
    flock libre + metadata displazada + health OK ⇒ backend huérfano vivo.
    """
    if holder.environment != expected.value:
        return False
    try:
        with urllib.request.urlopen(holder.health_url, timeout=1.0) as response:
            if response.status != 200:
                return False
            raw = response.read().decode("utf-8")
            payload: Any = json.loads(raw)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return False
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("status") == "ok"
        and payload.get("environment") == expected.value
    )


def run_launcher_restore(
    environment: ArcaEnvironment,
    *,
    identity_path: Path,
    seed_path: Path | None = None,
    app_data_root: Path | None = None,
    wsfex_factory=None,
    print_fn: Callable[..., None] = print,
) -> int:
    """Stop-gated restore: refuses if the profile lock is held (backend live)."""
    try:
        profile = EnvironmentProfile.resolve(
            environment, app_data_root=app_data_root
        )
    except ProfileError as exc:
        print_fn(f"ERROR: {exc}", file=sys.stderr)
        return 2

    lock = ProfileLock(profile.paths.launcher_lock)
    try:
        lock.acquire(
            port=_RESTORE_LOCK_PORT, environment=environment.value
        )
    except ProfileLockHeld as exc:
        print_fn(
            "ERROR: el backend de este perfil está en marcha. "
            "Detener FacturadorE antes de restaurar desde backup "
            f"({exc}).",
            file=sys.stderr,
        )
        return 1

    try:
        # Launcher muerto + flock libre + backend huérfano sano: no wipe.
        displaced = lock.displaced_holder
        if displaced is not None and _backend_session_healthy(
            displaced, environment
        ):
            lock.restore_displaced_holder()
            print_fn(
                f"ERROR: FacturadorE ({profile.display_name}) ya está "
                f"sirviendo en {displaced.base_url}/ sin un launcher activo. "
                "Cerrá esa instancia e intentá de nuevo.",
                file=sys.stderr,
            )
            return 1

        archive = seed_path or seed_archive_path(profile.paths)
        print_fn(f"Restaurando {profile.display_name} desde {archive}…")
        report = run_restore(
            environment.value,
            seed_path=archive,
            identity_path=identity_path,
            wsfex_factory=wsfex_factory,
        )
        print_fn(
            f"Listo: {report.inserted} comprobantes; "
            f"{len(report.gaps)} huecos conocidos."
        )
        return 0
    except (
        SeedBackupError,
        SeedIdentityError,
        ReconstructError,
        FiscalIdentityError,
        CertificateError,
    ) as exc:
        print_fn(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()

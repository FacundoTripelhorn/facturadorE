"""Launcher hook for FAC-65 full rebuild (backend must be stopped)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from ..constants import ArcaEnvironment
from ..profile import EnvironmentProfile, ProfileError
from ..reconstruct import ReconstructError
from ..restore import run_restore
from ..seed_backup import SeedBackupError, seed_archive_path
from ..seed_import import SeedIdentityError
from .lock import ProfileLock, ProfileLockHeld


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
        lock.acquire(port=1, environment=environment.value)
    except ProfileLockHeld as exc:
        print_fn(
            "ERROR: el backend de este perfil está en marcha. "
            "Detener FacturadorE antes de restaurar desde backup "
            f"({exc}).",
            file=sys.stderr,
        )
        return 1

    try:
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
    except (SeedBackupError, SeedIdentityError, ReconstructError) as exc:
        print_fn(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()

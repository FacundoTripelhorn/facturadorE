"""Perfiles de ambiente aislados (ADR 0001, FAC-23).

Cada ambiente fiscal (homologación / producción) mapea a un perfil interno con
su propia raíz en el app-data del sistema operativo. ``EnvironmentProfile``
fija el ambiente inmutable; ``ProfilePaths`` deriva todos los archivos de
runtime del perfil desde una única raíz.

Todo archivo de runtime del backend (DB, certificados, PDFs, TA cache,
params cache, logs, staging de backups, onboarding) se resuelve por acá
(FAC-25); ningún módulo construye paths por ambiente por su cuenta.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .constants import ArcaEnvironment

DB_FILENAME = "facturador.db"
CERT_FILENAME = "cert.crt"
KEY_FILENAME = "cert.key"
WSAA_TA_CACHE_FILENAME = "ta-wsfex.json"
LOG_FILENAME = "facturador.log"
ONBOARDING_FILENAME = "onboarding.json"

_DISPLAY_NAMES = {
    ArcaEnvironment.HOMO: "Homologación",
    ArcaEnvironment.PROD: "Producción",
}


class ProfileError(ValueError):
    """Configuración de perfil inválida o raíces de ambiente no aisladas."""


def parse_environment(raw: str) -> ArcaEnvironment:
    """Convierte texto de ambiente al tipo validado."""
    value = raw.strip().lower()
    try:
        return ArcaEnvironment(value)
    except ValueError as exc:
        allowed = ", ".join(e.value for e in ArcaEnvironment)
        raise ProfileError(
            f"Ambiente inválido: {raw!r} (valores permitidos: {allowed})"
        ) from exc


def resolve_app_data_root() -> Path:
    """Raíz de app-data del SO donde viven los perfiles ocultos."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if not base:
            raise ProfileError(
                "No se pudo resolver el app-data: LOCALAPPDATA/APPDATA "
                "no están definidos."
            )
        return Path(base) / "FacturadorE"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FacturadorE"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        expanded = os.path.expanduser(xdg)
        # La spec XDG dice IGNORAR rutas relativas (caer al default); acá se
        # falla fuerte a propósito, consistente con config.py: mejor negarse
        # a arrancar que resolver perfiles en una raíz inesperada.
        if not PurePosixPath(expanded).is_absolute():
            raise ProfileError(
                "XDG_DATA_HOME debe ser una ruta absoluta; "
                f"valor inválido: {xdg!r}"
            )
        return Path(expanded) / "facturadorE"
    return Path.home() / ".local" / "share" / "facturadorE"


def resolve_profile_root(
    environment: ArcaEnvironment,
    *,
    app_data_root: Path | None = None,
) -> Path:
    """Raíz del perfil para un ambiente bajo el app-data del SO."""
    base = (app_data_root or resolve_app_data_root()).expanduser()
    return base / environment.value


@dataclass(frozen=True)
class ProfilePaths:
    """Paths de runtime derivados de una única raíz de perfil.

    Sin sufijos ``<env>`` en nombres de archivo: el perfil ya es el ambiente.
    """

    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def db(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def cert(self) -> Path:
        return self.secrets_dir / CERT_FILENAME

    @property
    def key(self) -> Path:
        return self.secrets_dir / KEY_FILENAME

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @property
    def wsaa_ta_cache(self) -> Path:
        return self.data_dir / WSAA_TA_CACHE_FILENAME

    @property
    def arca_params_cache(self) -> Path:
        # Tabla ``arca_params`` dentro de la DB del perfil.
        return self.db

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / LOG_FILENAME

    @property
    def onboarding(self) -> Path:
        return self.data_dir / ONBOARDING_FILENAME

    def ensure_layout(self) -> None:
        """Crea la estructura mínima del perfil en el primer arranque.

        pdfs/ y logs/ los crea quien escribe en ellos; acá va lo que el
        usuario u otros procesos necesitan encontrar (secrets/ para colocar
        el par cert/key, data/ y backups/ como raíces de estado).
        """
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.secrets_dir.chmod(0o700)
        self.data_dir.mkdir(exist_ok=True)
        self.backups_dir.mkdir(exist_ok=True)

    def __repr__(self) -> str:
        return "ProfilePaths(...)"


@dataclass(frozen=True)
class EnvironmentProfile:
    """Ambiente fiscal inmutable y paths del perfil que lo respalda."""

    environment: ArcaEnvironment
    paths: ProfilePaths

    @property
    def display_name(self) -> str:
        return _DISPLAY_NAMES[self.environment]

    @classmethod
    def resolve(
        cls,
        environment: ArcaEnvironment,
        *,
        app_data_root: Path | None = None,
    ) -> EnvironmentProfile:
        """Resuelve el perfil desde el app-data del SO."""
        root = resolve_profile_root(environment, app_data_root=app_data_root)
        return cls(environment=environment, paths=ProfilePaths(root=root))

    @classmethod
    def for_testing(
        cls, environment: ArcaEnvironment, root: Path
    ) -> EnvironmentProfile:
        """Perfil aislado con raíz explícita (tests y herramientas internas)."""
        resolved = root.expanduser().resolve()
        return cls(environment=environment, paths=ProfilePaths(root=resolved))

    def __repr__(self) -> str:
        return f"EnvironmentProfile(environment={self.environment.value!r})"


def ensure_profile_roots_differ(
    first: EnvironmentProfile,
    second: EnvironmentProfile,
) -> None:
    """Garantiza que dos perfiles no compartan la misma raíz."""
    # resolve(): dos raíces textualmente distintas pueden ser el mismo
    # directorio real (symlinks/junctions, componentes ".."). El invariante
    # es sobre el directorio físico, no sobre el texto del path.
    if first.paths.root.resolve() == second.paths.root.resolve():
        raise ProfileError(
            f"Los perfiles ({first.environment.value} y "
            f"{second.environment.value}) no pueden compartir la misma "
            f"raíz: {first.paths.root}"
        )


def resolve_isolated_profiles(
    *,
    app_data_root: Path | None = None,
) -> tuple[EnvironmentProfile, EnvironmentProfile]:
    """Resuelve ambos perfiles y valida el aislamiento entre raíces."""
    homo = EnvironmentProfile.resolve(
        ArcaEnvironment.HOMO, app_data_root=app_data_root
    )
    prod = EnvironmentProfile.resolve(
        ArcaEnvironment.PROD, app_data_root=app_data_root
    )
    ensure_profile_roots_differ(homo, prod)
    return homo, prod

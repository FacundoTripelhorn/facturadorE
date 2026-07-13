"""Configuración de arranque del facturador.

Regla central (design.md §2.1.1 punto 1, reforzada por ADR 0001): TODO se
deriva de un único ambiente ``ArcaEnvironment`` que se INYECTA una sola vez
en el arranque (FAC-24). Las URLs de WSAA/WSFEX y los paths de runtime salen
del mismo perfil, por lo que es imposible por construcción usar el
certificado de homologación contra producción o viceversa. No existen
overrides por URL, y ningún módulo de la app lee ``ARCA_ENV`` por su cuenta:
el único punto de lectura es ``resolve_boot_environment()`` en el arranque
(hasta que el launcher de ADR 0001 pase a ser quien elige el ambiente).

Resolución de configuración (única, sin fallbacks al directorio de trabajo):

1. ``FACTURADOR_HOME`` (default ``~/facturador``; en Docker, ``/facturador``
   fijado por ENV en el Dockerfile) contiene SOLO el ``.env`` de bootstrap.
   Los archivos de runtime ya no viven ahí: cada ambiente tiene su perfil
   aislado bajo el app-data del SO (ADR 0001 / FAC-25) y ``ProfilePaths``
   es la única fuente de esos paths (DB, certs, PDFs, TA cache, logs,
   backups, onboarding).
2. El ``.env`` se lee SOLO de ``<home>/.env`` — nunca del CWD — y es el
   bootstrap mínimo: ``ARCA_ENV`` y, opcionalmente, ``FACTURADOR_PORT``.
   Si no existe, la app crea un esqueleto SIN ambiente activo: elegirlo es
   un acto explícito del usuario/launcher, nunca un default (FAC-24).
3. El resto de la configuración (datos del emisor, punto de venta, backups)
   vive en la DB del perfil y se edita desde la página Configuración
   (ver settings.py).

Los certificados son archivos en ``<perfil>/secrets/cert.{crt,key}``
(nombres genéricos: el perfil YA es el ambiente). FAC-35: el arranque
tolera el par ausente (estado ``certificate_required`` + guardia); si la
clave existe, sus permisos deben ser 400/600.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .constants import DEFAULT_PORT, WSAA_URLS, WSFEX_URLS, ArcaEnvironment
from .profile import (
    EnvironmentProfile,
    ProfileError,
    ProfilePaths,
    parse_environment,
)

DEFAULT_HOME = "~/facturador"

# Bootstrap creado en el primer arranque. Solo lo que no puede vivir en la
# DB: el flag de ambiente y el puerto local. El ambiente queda comentado a
# propósito (FAC-24): un arranque sin elección explícita debe fallar, no
# caer en homologación en silencio.
BOOTSTRAP_ENV = f"""\
# Bootstrap del facturador. El resto de la configuración (datos del emisor,
# punto de venta, backups) se edita desde la app, en la página Configuración.

# Ambiente ARCA: homo | prod. SIN default: descomentar y elegir uno (ADR
# 0001). Deriva URLs de WSAA/WSFEX y qué perfil aislado (con su par
# cert/key en secrets/cert.{{crt,key}}) se usa. Único flag: no hay overrides.
#ARCA_ENV=homo

# Puerto local (siempre en 127.0.0.1).
#FACTURADOR_PORT={DEFAULT_PORT}
"""


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    env: ArcaEnvironment   # inmutable: fijado al construir, nunca re-leído
    paths: ProfilePaths    # ÚNICA fuente de paths de runtime (FAC-25)

    @property
    def wsaa_url(self) -> str:
        return WSAA_URLS[self.env]

    @property
    def wsfex_url(self) -> str:
        return WSFEX_URLS[self.env]


def resolve_home() -> Path:
    """Único punto de resolución del home del bootstrap: FACTURADOR_HOME o
    ~/facturador. Sin fallback al directorio de trabajo."""
    return Path(os.environ.get("FACTURADOR_HOME") or DEFAULT_HOME).expanduser()


def ensure_home(home: Path) -> None:
    """Crea el home y el .env de bootstrap si no existen.

    El home ya no aloja archivos de runtime (FAC-25): secrets/, data/ y
    backups/ viven en el perfil de cada ambiente (``ProfilePaths``).
    """
    home.mkdir(parents=True, exist_ok=True)
    env_file = home / ".env"
    if not env_file.exists():
        env_file.write_text(BOOTSTRAP_ENV, encoding="utf-8")


def resolve_boot_environment() -> ArcaEnvironment:
    """Único punto de lectura de ``ARCA_ENV`` (proceso o <home>/.env).

    Solo para entrypoints (``__main__``, scripts): resuelve el ambiente UNA
    vez, antes de construir nada. El resto de la app recibe el ambiente ya
    inyectado y nunca vuelve a mirar variables de entorno. Sin default
    silencioso: el bootstrap auto-creado trae ``ARCA_ENV`` comentado, así
    que un primer arranque sin elección explícita falla acá.
    """
    home = resolve_home()
    ensure_home(home)
    # No pisa variables ya presentes en el entorno (p.ej. ARCA_ENV fijado
    # por el launcher, o FACTURADOR_PORT fijado por el Dockerfile).
    load_dotenv(home / ".env")

    raw = os.environ.get("ARCA_ENV")
    if not raw:
        raise ConfigError(
            f"ARCA_ENV no está definido (ni en el entorno ni en {home / '.env'}). "
            "El backend arranca contra exactamente un ambiente explícito: "
            "definir ARCA_ENV=homo o ARCA_ENV=prod."
        )
    try:
        return parse_environment(raw)
    except ProfileError as exc:
        raise ConfigError(f"ARCA_ENV inválido: {exc}") from exc


def resolve_boot_profile() -> EnvironmentProfile:
    """Perfil del ambiente elegido en el bootstrap (entrypoints y scripts)."""
    return EnvironmentProfile.resolve(resolve_boot_environment())


def load_config(profile: EnvironmentProfile) -> Config:
    """Config de un perfil YA elegido y validado.

    El perfil es un parámetro obligatorio (FAC-24/FAC-25): no hay camino que
    construya una Config sin decidir el ambiente, y los paths salen siempre
    del perfil — nunca de un layout compartido ni del CWD.
    """
    profile.paths.ensure_layout()
    config = Config(env=profile.environment, paths=profile.paths)
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    """Valida secretos presentes; permite arrancar sin par (FAC-35).

    Sin ``cert.crt``/``cert.key`` el backend igual arranca: el estado de
    setup queda en ``certificate_required`` y la guardia bloquea factura/
    ARCA hasta completar el onboarding. Si la clave existe, sus permisos
    deben ser 400/600 (design.md §2.5).
    """
    cert_ok = config.paths.cert.is_file()
    key_ok = config.paths.key.is_file()
    if cert_ok != key_ok:
        missing = "cert.key" if cert_ok else "cert.crt"
        present = "cert.crt" if cert_ok else "cert.key"
        raise ConfigError(
            f"Par incompleto en el perfil de {config.env}: está {present} "
            f"pero falta {missing} ({config.paths.secrets_dir})."
        )
    if key_ok:
        _check_key_permissions(config.paths.key)


def key_permissions_ok(key_path: Path) -> bool:
    """True si la key no existe (caller decide) o sus permisos son 400/600.

    En Windows los bits de modo no aplican; el layout definitivo es Docker.
    """
    if sys.platform == "win32":
        return True
    if not key_path.is_file():
        return True
    mode = stat.S_IMODE(key_path.stat().st_mode)
    return not bool(mode & 0o077)


def _check_key_permissions(key_path: Path) -> None:
    # Chequeo POSIX (dentro del contenedor Linux, design.md §2.5). En Windows
    # los bits de modo no aplican; el layout definitivo corre en Docker.
    if not key_permissions_ok(key_path):
        mode = stat.S_IMODE(key_path.stat().st_mode)
        raise ConfigError(
            f"Permisos laxos en {key_path} ({oct(mode)}): la clave privada "
            "debe ser 400/600. Corregir con: chmod 400 " + str(key_path)
        )

"""Configuración de arranque del facturador.

Regla central (design.md §2.1.1 punto 1, reforzada por ADR 0001): TODO se
deriva de un único ambiente ``ArcaEnvironment`` que se INYECTA una sola vez
en el arranque (FAC-24). Las URLs de WSAA/WSFEX y los paths de certificado
salen del mismo valor, por lo que es imposible por construcción usar el
certificado de homologación contra producción o viceversa. No existen
overrides por URL, y ningún módulo de la app lee ``ARCA_ENV`` por su cuenta:
el único punto de lectura es ``resolve_boot_environment()`` en el arranque
(hasta que el launcher de ADR 0001 pase a ser quien elige el ambiente).

Resolución de configuración (única, sin fallbacks al directorio de trabajo):

1. ``FACTURADOR_HOME`` (default ``~/facturador``; en Docker, ``/facturador``
   fijado por ENV en el Dockerfile) es LA raíz de datos. La app crea el home
   y su estructura (``secrets/``, ``data/``, ``backups/``) en el primer
   arranque.
2. El ``.env`` se lee SOLO de ``<home>/.env`` — nunca del CWD — y es el
   bootstrap mínimo: ``ARCA_ENV`` y, opcionalmente, ``FACTURADOR_PORT``.
   Si no existe, la app lo crea con ``ARCA_ENV=homo``.
3. El resto de la configuración (datos del emisor, punto de venta, backups)
   vive en la DB y se edita desde la página Configuración (ver settings.py).

Los certificados siguen siendo archivos en ``<home>/secrets/<env>.{crt,key}``
colocados a mano; los chequeos de arranque (permisos 400/600, par cert/env
consistente) se mantienen intactos.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .constants import WSAA_URLS, WSFEX_URLS, ArcaEnvironment
from .profile import ProfileError, parse_environment

DEFAULT_HOME = "~/facturador"

# Bootstrap creado en el primer arranque. Solo lo que no puede vivir en la
# DB: el flag de ambiente y el puerto local.
BOOTSTRAP_ENV = """\
# Bootstrap del facturador. El resto de la configuración (datos del emisor,
# punto de venta, backups) se edita desde la app, en la página Configuración.

# Ambiente ARCA: homo | prod. Deriva URLs de WSAA/WSFEX y qué par cert/key
# se usa (secrets/<env>.crt + secrets/<env>.key). Único flag: no hay overrides.
ARCA_ENV=homo

# Puerto local (siempre en 127.0.0.1).
#FACTURADOR_PORT=8399
"""


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    env: ArcaEnvironment  # inmutable: fijado al construir, nunca re-leído
    home: Path            # raíz de datos (secrets/, data/, backups/)

    @property
    def wsaa_url(self) -> str:
        return WSAA_URLS[self.env]

    @property
    def wsfex_url(self) -> str:
        return WSFEX_URLS[self.env]

    @property
    def cert_path(self) -> Path:
        return self.home / "secrets" / f"{self.env}.crt"

    @property
    def key_path(self) -> Path:
        return self.home / "secrets" / f"{self.env}.key"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"


def resolve_home() -> Path:
    """Único punto de resolución del home: FACTURADOR_HOME o ~/facturador.
    Sin fallback al directorio de trabajo, acá ni en backup/restore."""
    return Path(os.environ.get("FACTURADOR_HOME") or DEFAULT_HOME).expanduser()


def ensure_home(home: Path) -> None:
    """Crea el home y su estructura en el primer arranque, incluido el .env
    de bootstrap si no existe."""
    secrets = home / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        secrets.chmod(0o700)
    (home / "data").mkdir(exist_ok=True)
    (home / "backups").mkdir(exist_ok=True)
    env_file = home / ".env"
    if not env_file.exists():
        env_file.write_text(BOOTSTRAP_ENV, encoding="utf-8")


def resolve_boot_environment() -> ArcaEnvironment:
    """Único punto de lectura de ``ARCA_ENV`` (proceso o <home>/.env).

    Solo para entrypoints (``__main__``, scripts): resuelve el ambiente UNA
    vez, antes de construir nada. El resto de la app recibe el ambiente ya
    inyectado y nunca vuelve a mirar variables de entorno. Sin default
    silencioso: un ``.env`` sin ``ARCA_ENV`` es un arranque inválido (el
    bootstrap auto-creado siempre lo trae).
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
            "El backend arranca contra exactamente un ambiente explícito."
        )
    try:
        return parse_environment(raw)
    except ProfileError as exc:
        raise ConfigError(f"ARCA_ENV inválido: {exc}") from exc


def load_config(environment: ArcaEnvironment) -> Config:
    """Config del home compartido para un ambiente YA elegido y validado.

    El ambiente es un parámetro obligatorio (FAC-24): no hay camino que
    construya una Config sin decidirlo explícitamente.
    """
    home = resolve_home()
    ensure_home(home)
    config = Config(env=environment, home=home)
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    """La app se niega a arrancar con secretos ausentes o permisos laxos."""
    for path in (config.cert_path, config.key_path):
        if not path.is_file():
            raise ConfigError(
                f"Falta {path.name} para ARCA_ENV={config.env}: {path}. "
                "Colocar el par cert/key en <home>/secrets/ con los nombres "
                "<env>.crt / <env>.key."
            )
    _check_key_permissions(config.key_path)
    config.data_dir.mkdir(parents=True, exist_ok=True)


def _check_key_permissions(key_path: Path) -> None:
    # Chequeo POSIX (dentro del contenedor Linux, design.md §2.5). En Windows
    # los bits de modo no aplican; el layout definitivo corre en Docker.
    if sys.platform == "win32":
        return
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"Permisos laxos en {key_path} ({oct(mode)}): la clave privada "
            "debe ser 400/600. Corregir con: chmod 400 " + str(key_path)
        )

"""Configuración del facturador.

Regla central (spike.md §2.1.1 punto 1): TODO se deriva de un único flag
``ARCA_ENV``. Las URLs de WSAA/WSFEX y los paths de certificado salen del
mismo valor, por lo que es imposible por construcción usar el certificado de
homologación contra producción o viceversa. No existen overrides por URL.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_ENVS = ("homo", "prod")

WSAA_URLS = {
    "homo": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "prod": "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}

WSFEX_URLS = {
    "homo": "https://wswhomo.afip.gov.ar/wsfexv1/service.asmx",
    "prod": "https://servicios1.afip.gov.ar/wsfexv1/service.asmx",
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Emisor:
    """Datos del emisor que van al PDF y no viajan a ARCA (spike.md §0.1):
    leyenda de IVA, IIBB e inicio de actividades salen de config local."""

    razon_social: str = ""
    domicilio: str = ""
    iibb: str = ""                 # vacío => se imprime el CUIT
    inicio_actividades: str = ""   # texto libre, p.ej. "01/2020"
    condicion_iva: str = "IVA Responsable Inscripto"


@dataclass(frozen=True)
class Config:
    env: str          # "homo" | "prod"
    home: Path        # raíz de datos (secrets/, data/, backups/)
    cuit: int | None  # emisor; None => se extrae del certificado en runtime
    key_passphrase: str | None
    punto_venta: int = 1  # en homo es libre; en prod, el PV RECE exclusivo
    emisor: Emisor = Emisor()

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


def load_config(env_file: str | Path | None = ".env") -> Config:
    """Carga la config desde el entorno (y .env si existe) y la valida."""
    if env_file is not None:
        load_dotenv(env_file)

    env = os.environ.get("ARCA_ENV", "homo").strip().lower()
    if env not in VALID_ENVS:
        raise ConfigError(
            f"ARCA_ENV inválido: {env!r} (valores permitidos: {', '.join(VALID_ENVS)})"
        )

    home = Path(os.environ.get("FACTURADOR_HOME", Path.cwd())).expanduser()

    cuit_raw = os.environ.get("ARCA_CUIT", "").strip()
    cuit: int | None = None
    if cuit_raw:
        if not cuit_raw.isdigit() or len(cuit_raw) != 11:
            raise ConfigError("ARCA_CUIT debe ser 11 dígitos sin guiones")
        cuit = int(cuit_raw)

    punto_venta_raw = os.environ.get("ARCA_PUNTO_VTA", "1").strip()
    if not punto_venta_raw.isdigit() or int(punto_venta_raw) < 1:
        raise ConfigError("ARCA_PUNTO_VTA debe ser un entero >= 1")

    emisor = Emisor(
        razon_social=os.environ.get("EMISOR_RAZON_SOCIAL", "").strip(),
        domicilio=os.environ.get("EMISOR_DOMICILIO", "").strip(),
        iibb=os.environ.get("EMISOR_IIBB", "").strip(),
        inicio_actividades=os.environ.get("EMISOR_INICIO_ACTIVIDADES", "").strip(),
    )

    config = Config(
        env=env,
        home=home,
        cuit=cuit,
        key_passphrase=os.environ.get("ARCA_KEY_PASSPHRASE") or None,
        punto_venta=int(punto_venta_raw),
        emisor=emisor,
    )
    validate_config(config)
    return config


def validate_config(config: Config) -> None:
    """La app se niega a arrancar con secretos ausentes o permisos laxos."""
    for path in (config.cert_path, config.key_path):
        if not path.is_file():
            raise ConfigError(
                f"Falta {path.name} para ARCA_ENV={config.env}: {path}. "
                "El par cert/key debe llamarse <env>.crt / <env>.key."
            )
    _check_key_permissions(config.key_path)
    config.data_dir.mkdir(parents=True, exist_ok=True)


def _check_key_permissions(key_path: Path) -> None:
    # Chequeo POSIX (dentro del contenedor Linux, spike.md §2.5). En Windows
    # los bits de modo no aplican; el layout definitivo corre en Docker.
    if sys.platform == "win32":
        return
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"Permisos laxos en {key_path} ({oct(mode)}): la clave privada "
            "debe ser 400/600. Corregir con: chmod 400 " + str(key_path)
        )

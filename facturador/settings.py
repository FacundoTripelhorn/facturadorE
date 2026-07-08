"""Configuración de dominio: vive en la DB (tabla ``settings``), no en el entorno.

La app es dueña de su configuración: los datos del emisor que van al PDF, el
punto de venta y la config de backups se editan desde la página Configuración
y viajan en el backup cifrado junto con el resto del estado. En el ``.env``
de bootstrap queda SOLO lo que no puede vivir en la DB: ``ARCA_ENV`` (deriva
el pareo cert/URL, design.md §2.1.1 punto 1) y ``FACTURADOR_PORT``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from . import repo

logger = logging.getLogger(__name__)

CONDICION_IVA_DEFAULT = "IVA Responsable Inscripto"
BACKUP_PREFIX_DEFAULT = "facturador"


@dataclass(frozen=True)
class Emisor:
    """Datos del emisor que van al PDF y no viajan a ARCA (design.md §0.1):
    leyenda de IVA, IIBB e inicio de actividades salen de la DB."""

    razon_social: str = ""
    domicilio: str = ""
    iibb: str = ""                 # literal del comprobante, p.ej. "Exento"
    inicio_actividades: str = ""   # DD/MM/AAAA, como lo imprime el comprobante
    condicion_iva: str = CONDICION_IVA_DEFAULT

    @property
    def completo(self) -> bool:
        """Todas las líneas del encabezado del comprobante real salen de acá;
        con alguna vacía el PDF queda con un hueco, así que la UI dirige a
        Configuración antes de permitir emitir. El comprobante imprime el
        texto literal de IIBB (p.ej. "Exento"), nunca el CUIT como reemplazo."""
        return bool(
            self.razon_social
            and self.domicilio
            and self.iibb
            and self.inicio_actividades
        )


@dataclass(frozen=True)
class Settings:
    emisor: Emisor = Emisor()
    punto_venta: int = 1  # en homo es libre; en prod, el PV RECE exclusivo
    backup_s3_bucket: str = ""    # vacío => el backup queda solo local
    backup_s3_prefix: str = BACKUP_PREFIX_DEFAULT


def load_settings(conn: sqlite3.Connection) -> Settings:
    raw = repo.get_settings(conn)
    punto_venta_raw = raw.get("punto_venta", "1")
    return Settings(
        emisor=Emisor(
            razon_social=raw.get("emisor_razon_social", ""),
            domicilio=raw.get("emisor_domicilio", ""),
            iibb=raw.get("emisor_iibb", ""),
            inicio_actividades=raw.get("emisor_inicio_actividades", ""),
            condicion_iva=raw.get("emisor_condicion_iva")
            or CONDICION_IVA_DEFAULT,
        ),
        punto_venta=int(punto_venta_raw) if punto_venta_raw.isdigit() else 1,
        backup_s3_bucket=raw.get("backup_s3_bucket", ""),
        backup_s3_prefix=raw.get("backup_s3_prefix") or BACKUP_PREFIX_DEFAULT,
    )


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    repo.save_settings(
        conn,
        {
            "emisor_razon_social": settings.emisor.razon_social,
            "emisor_domicilio": settings.emisor.domicilio,
            "emisor_iibb": settings.emisor.iibb,
            "emisor_inicio_actividades": settings.emisor.inicio_actividades,
            "emisor_condicion_iva": settings.emisor.condicion_iva,
            "punto_venta": str(settings.punto_venta),
            "backup_s3_bucket": settings.backup_s3_bucket,
            "backup_s3_prefix": settings.backup_s3_prefix,
        },
    )


# Variables legadas del .env → claves de settings. Un usuario que venía del
# esquema anterior (EMISOR_* y compañía en el entorno) no pierde datos: se
# importan a la DB en el primer arranque post-upgrade.
_LEGACY_ENV_KEYS = {
    "EMISOR_RAZON_SOCIAL": "emisor_razon_social",
    "EMISOR_DOMICILIO": "emisor_domicilio",
    "EMISOR_IIBB": "emisor_iibb",
    "EMISOR_INICIO_ACTIVIDADES": "emisor_inicio_actividades",
    "ARCA_PUNTO_VTA": "punto_venta",
    "BACKUP_S3_BUCKET": "backup_s3_bucket",
    "BACKUP_S3_PREFIX": "backup_s3_prefix",
}


def migrate_env_settings(
    conn: sqlite3.Connection, environ: Mapping[str, str]
) -> list[str]:
    """Importa a la DB los settings que sigan viniendo del entorno/.env.

    Solo migra claves que la DB aún no tiene (lo guardado desde la UI gana) y
    valores no vacíos. Devuelve las variables importadas y lo avisa en el log
    para que el usuario sepa que ya puede limpiar su .env.
    """
    existentes = repo.get_settings(conn)
    a_importar = {
        clave_db: environ[var].strip()
        for var, clave_db in _LEGACY_ENV_KEYS.items()
        if clave_db not in existentes and environ.get(var, "").strip()
    }
    if not a_importar:
        return []
    repo.save_settings(conn, a_importar)
    importadas = [
        var for var, clave_db in _LEGACY_ENV_KEYS.items() if clave_db in a_importar
    ]
    logger.info(
        "Config del entorno importada a la DB (%s): estas variables ya no se "
        "leen y pueden borrarse del .env; editar desde la página Configuración.",
        ", ".join(importadas),
    )
    return importadas

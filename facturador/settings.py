"""Configuración de dominio: vive en la DB, no en el entorno.

La app es dueña de su configuración: los datos del emisor que van al PDF,
sus puntos de venta y la config de backups se editan desde la página
Configuración y viajan en el backup cifrado junto con el resto del estado.
En el ``.env`` de bootstrap queda SOLO lo que no puede vivir en la DB:
``ARCA_ENV`` (deriva el pareo cert/URL, design.md §2.1.1 punto 1) y
``FACTURADOR_PORT``.

El emisor es una entidad (tabla ``emisores``) LOCAL al perfil (ADR 0001 /
FAC-26): la DB entera pertenece a un solo ambiente, así que acá no se
selecciona ni filtra por ambiente. Un perfil puede tener varios emisores
(una sola identidad fiscal: el CUIT del certificado del perfil, sellado
en ``settings.fiscal_cuit`` — FAC-39) y la app opera con el activo
explícito (``active_emisor_id`` en settings, una única clave); sin
selección no hay emisor operativo. La config de backups es global al
perfil. El emisor no lleva CUIT ni puede sobrescribir la identidad fiscal.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import repo

# Única clave de selección: sin sufijo de ambiente, el perfil YA es el
# ambiente (FAC-26).
ACTIVE_EMISOR_KEY = "active_emisor_id"
CONDICION_IVA_DEFAULT = "IVA Responsable Inscripto"
BACKUP_PREFIX_DEFAULT = "facturador"


@dataclass(frozen=True)
class Emisor:
    """Un emisor del perfil con sus propios puntos de venta; los datos de
    texto van al PDF y no viajan a ARCA (design.md §0.1)."""

    id: str | None = None
    razon_social: str = ""
    domicilio: str = ""
    iibb: str = ""                 # literal del comprobante, p.ej. "Exento"
    inicio_actividades: str = ""   # DD/MM/AAAA, como lo imprime el comprobante
    condicion_iva: str = CONDICION_IVA_DEFAULT
    # Sello del perfil al crear (FAC-26): dato de contexto, NO elegible por
    # el usuario; el form/API lo pierde en FAC-27.
    ambiente: str = "homo"
    # Habilitados para este emisor. En homo el PV es libre; en prod, el PV
    # RECE exclusivo.
    puntos_venta: tuple[int, ...] = (1,)

    @property
    def punto_venta(self) -> int:
        """Primer PV habilitado; solo para el caso de un único PV."""
        return self.puntos_venta[0]

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
    backup_s3_bucket: str = ""    # vacío => el backup queda solo local
    backup_s3_prefix: str = BACKUP_PREFIX_DEFAULT


def _parse_puntos_venta(raw_value: str) -> tuple[int, ...]:
    try:
        valores = json.loads(raw_value)
    except ValueError:
        return (1,)
    if (
        isinstance(valores, list)
        and valores
        and all(isinstance(v, int) and v >= 1 for v in valores)
    ):
        return tuple(valores)
    return (1,)


def _row_to_emisor(row: sqlite3.Row) -> Emisor:
    return Emisor(
        id=row["id"],
        razon_social=row["razon_social"],
        domicilio=row["domicilio"],
        iibb=row["iibb"],
        inicio_actividades=row["inicio_actividades"],
        condicion_iva=row["condicion_iva"] or CONDICION_IVA_DEFAULT,
        ambiente=row["ambiente"],
        puntos_venta=_parse_puntos_venta(row["puntos_venta"]),
    )


def _active_emisor_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Emisor activo explícito del perfil, o ninguno."""
    active_id = repo.get_settings(conn).get(ACTIVE_EMISOR_KEY)
    if not active_id:
        return None
    return repo.get_emisor(conn, active_id)


def get_active_emisor_id(conn: sqlite3.Connection) -> str | None:
    row = _active_emisor_row(conn)
    return None if row is None else row["id"]


def set_active_emisor(conn: sqlite3.Connection, emisor_id: str) -> None:
    emisor = repo.get_emisor(conn, emisor_id)
    if emisor is None:
        raise ValueError(f"Emisor {emisor_id} no existe")
    repo.save_settings(conn, {ACTIVE_EMISOR_KEY: emisor_id})


def load_emisor(conn: sqlite3.Connection, emisor_id: str) -> Emisor:
    """Emisor persistido en un comprobante (snapshot por id)."""
    row = repo.get_emisor(conn, emisor_id)
    if row is None:
        raise ValueError(f"Emisor {emisor_id} no existe")
    return _row_to_emisor(row)


def load_settings(conn: sqlite3.Connection) -> Settings:
    """Settings con el emisor activo del perfil y la config de backups.

    Sin parámetro de ambiente (FAC-26): la DB es de un solo perfil y la
    selección activa es una única clave.
    """
    row = _active_emisor_row(conn)
    raw = repo.get_settings(conn)
    return Settings(
        emisor=Emisor() if row is None else _row_to_emisor(row),
        backup_s3_bucket=raw.get("backup_s3_bucket", ""),
        backup_s3_prefix=raw.get("backup_s3_prefix") or BACKUP_PREFIX_DEFAULT,
    )


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    """Guarda el emisor (solo por id explícito; sin id, crea uno nuevo) y la
    config de backups como settings globales."""
    emisor = settings.emisor
    if emisor.id:
        repo.update_emisor(
            conn,
            emisor.id,
            {
                "razon_social": emisor.razon_social,
                "domicilio": emisor.domicilio,
                "iibb": emisor.iibb,
                "inicio_actividades": emisor.inicio_actividades,
                "condicion_iva": emisor.condicion_iva,
                "puntos_venta": json.dumps(list(emisor.puntos_venta)),
            },
        )
    else:
        repo.create_emisor(
            conn,
            {
                "razon_social": emisor.razon_social,
                "domicilio": emisor.domicilio,
                "iibb": emisor.iibb,
                "inicio_actividades": emisor.inicio_actividades,
                "condicion_iva": emisor.condicion_iva,
                "ambiente": emisor.ambiente,
                "puntos_venta": json.dumps(list(emisor.puntos_venta)),
            },
        )
    repo.save_settings(
        conn,
        {
            "backup_s3_bucket": settings.backup_s3_bucket,
            "backup_s3_prefix": settings.backup_s3_prefix,
        },
    )

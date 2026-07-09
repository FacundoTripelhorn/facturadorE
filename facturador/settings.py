"""Configuración de dominio: vive en la DB, no en el entorno.

La app es dueña de su configuración: los datos del emisor que van al PDF,
sus puntos de venta y la config de backups se editan desde la página
Configuración y viajan en el backup cifrado junto con el resto del estado.
En el ``.env`` de bootstrap queda SOLO lo que no puede vivir en la DB:
``ARCA_ENV`` (deriva el pareo cert/URL, design.md §2.1.1 punto 1) y
``FACTURADOR_PORT``.

El emisor es una entidad (tabla ``emisores``): cada uno declara con qué
ambiente interactúa y qué puntos de venta tiene habilitados, y un mismo
ambiente puede tener varios. La app opera con el emisor activo del ambiente
corriente (``active_emisor_id`` en settings); si no hay uno válido, usa el
más antiguo del ambiente. La config de backups es global (tabla ``settings``,
clave/valor): el backup cubre la DB entera, no un emisor.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import repo

ACTIVE_EMISOR_KEY = "active_emisor_id"
CONDICION_IVA_DEFAULT = "IVA Responsable Inscripto"
BACKUP_PREFIX_DEFAULT = "facturador"


@dataclass(frozen=True)
class Emisor:
    """Un emisor factura contra UN ambiente (se elige al darlo de alta) con
    sus propios puntos de venta; los datos de texto van al PDF y no viajan a
    ARCA (design.md §0.1)."""

    id: str | None = None
    razon_social: str = ""
    domicilio: str = ""
    iibb: str = ""                 # literal del comprobante, p.ej. "Exento"
    inicio_actividades: str = ""   # DD/MM/AAAA, como lo imprime el comprobante
    condicion_iva: str = CONDICION_IVA_DEFAULT
    ambiente: str = "homo"         # con qué ambiente interactúa este emisor
    # Habilitados para este emisor. En homo el PV es libre; en prod, el PV
    # RECE exclusivo.
    puntos_venta: tuple[int, ...] = (1,)

    @property
    def punto_venta(self) -> int:
        """PV por defecto al emitir: el primero de la lista habilitada."""
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


def _resolve_emisor_row(conn: sqlite3.Connection, env: str) -> sqlite3.Row | None:
    """Emisor activo del ambiente, o el más antiguo si no hay selección."""
    raw = repo.get_settings(conn)
    active_id = raw.get(ACTIVE_EMISOR_KEY)
    if active_id:
        candidato = repo.get_emisor(conn, active_id)
        if candidato is not None and candidato["ambiente"] == env:
            return candidato
    return repo.get_emisor_por_ambiente(conn, env)


def get_active_emisor_id(conn: sqlite3.Connection, env: str) -> str | None:
    row = _resolve_emisor_row(conn, env)
    return None if row is None else row["id"]


def set_active_emisor(conn: sqlite3.Connection, emisor_id: str) -> None:
    repo.save_settings(conn, {ACTIVE_EMISOR_KEY: emisor_id})


def load_settings(conn: sqlite3.Connection, env: str) -> Settings:
    """Settings con el emisor activo del ambiente y la config global de backups."""
    row = _resolve_emisor_row(conn, env)
    raw = repo.get_settings(conn)
    return Settings(
        emisor=Emisor(ambiente=env) if row is None else _row_to_emisor(row),
        backup_s3_bucket=raw.get("backup_s3_bucket", ""),
        backup_s3_prefix=raw.get("backup_s3_prefix") or BACKUP_PREFIX_DEFAULT,
    )


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    """Guarda el emisor (por id si lo tiene, si no upsert del ambiente) y la
    config de backups como settings globales."""
    emisor = settings.emisor
    data = {
        "razon_social": emisor.razon_social,
        "domicilio": emisor.domicilio,
        "iibb": emisor.iibb,
        "inicio_actividades": emisor.inicio_actividades,
        "condicion_iva": emisor.condicion_iva,
        "ambiente": emisor.ambiente,
        "puntos_venta": json.dumps(list(emisor.puntos_venta)),
    }
    if emisor.id:
        repo.update_emisor(conn, emisor.id, data)
    else:
        row = repo.upsert_emisor(conn, data)
        set_active_emisor(conn, row["id"])
    repo.save_settings(
        conn,
        {
            "backup_s3_bucket": settings.backup_s3_bucket,
            "backup_s3_prefix": settings.backup_s3_prefix,
        },
    )

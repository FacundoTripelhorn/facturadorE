"""Configuración de dominio: vive en la DB (tabla ``settings``), no en el entorno.

La app es dueña de su configuración: los datos del emisor que van al PDF, el
punto de venta y la config de backups se editan desde la página Configuración
y viajan en el backup cifrado junto con el resto del estado. En el ``.env``
de bootstrap queda SOLO lo que no puede vivir en la DB: ``ARCA_ENV`` (deriva
el pareo cert/URL, design.md §2.1.1 punto 1) y ``FACTURADOR_PORT``.

El emisor (punto de venta incluido) se guarda POR AMBIENTE: en homo se
factura con un PV libre y datos de prueba sin ensuciar los de producción, y
la numeración local nunca mezcla ambientes. La config de backups es global:
el backup cubre la DB entera, no un ambiente.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass

from . import repo

CONDICION_IVA_DEFAULT = "IVA Responsable Inscripto"
BACKUP_PREFIX_DEFAULT = "facturador"


@dataclass(frozen=True)
class Emisor:
    """Datos del emisor, uno por ambiente. Las líneas de texto van al PDF y
    no viajan a ARCA (design.md §0.1); el punto de venta define la numeración
    de los comprobantes de ese ambiente."""

    razon_social: str = ""
    domicilio: str = ""
    iibb: str = ""                 # literal del comprobante, p.ej. "Exento"
    inicio_actividades: str = ""   # DD/MM/AAAA, como lo imprime el comprobante
    condicion_iva: str = CONDICION_IVA_DEFAULT
    punto_venta: int = 1           # en homo es libre; en prod, el PV RECE exclusivo

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


_EMISOR_FIELDS = tuple(f.name for f in dataclasses.fields(Emisor))


def _emisor_key(env: str, field: str) -> str:
    # Clave por ambiente: "homo.emisor_razon_social", "prod.emisor_punto_venta".
    return f"{env}.emisor_{field}"


def load_settings(conn: sqlite3.Connection, env: str) -> Settings:
    raw = repo.get_settings(conn)
    punto_venta_raw = raw.get(_emisor_key(env, "punto_venta"), "1")
    return Settings(
        emisor=Emisor(
            razon_social=raw.get(_emisor_key(env, "razon_social"), ""),
            domicilio=raw.get(_emisor_key(env, "domicilio"), ""),
            iibb=raw.get(_emisor_key(env, "iibb"), ""),
            inicio_actividades=raw.get(
                _emisor_key(env, "inicio_actividades"), ""
            ),
            condicion_iva=raw.get(_emisor_key(env, "condicion_iva"))
            or CONDICION_IVA_DEFAULT,
            punto_venta=int(punto_venta_raw) if punto_venta_raw.isdigit() else 1,
        ),
        backup_s3_bucket=raw.get("backup_s3_bucket", ""),
        backup_s3_prefix=raw.get("backup_s3_prefix") or BACKUP_PREFIX_DEFAULT,
    )


def save_settings(
    conn: sqlite3.Connection, env: str, settings: Settings
) -> None:
    valores = {
        _emisor_key(env, field): str(getattr(settings.emisor, field))
        for field in _EMISOR_FIELDS
    }
    valores["backup_s3_bucket"] = settings.backup_s3_bucket
    valores["backup_s3_prefix"] = settings.backup_s3_prefix
    repo.save_settings(conn, valores)

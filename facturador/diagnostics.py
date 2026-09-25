"""Diagnóstico del perfil: ¿se puede emitir con este ambiente?

Cada chequeo devuelve un nivel (ok / aviso / bloquea), un detalle y, si
hace falta, qué hacer. Los chequeos locales no tocan ARCA y funcionan con
el onboarding a medio hacer; los de ARCA se corren solo a pedido.

Nunca se muestran claves, tokens, firmas, rutas internas ni credenciales:
solo metadata (CUIT, fechas, conteos, estados). Los mensajes de error que
podrían traer rutas (p.ej. el último error de backup) no se muestran.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

import httpx

from . import repo
from .arca.events import load_events
from .arca.wsaa import WsaaError
from .arca.wsfex import WsfexClient, WsfexError
from .certs import CertificateError, load_certificate_metadata
from .constants import CBTE_TIPO_FACTURA_E, InvoiceStatus
from .fiscal_identity import FiscalIdentityError, resolve_profile_fiscal_cuit
from .migrations import current_version, latest_version
from .profile import EnvironmentProfile
from .seed_backup import recipients_path
from .seed_backup_sync import SeedBackupStatus, load_seed_backup_state
from .settings import get_active_emisor_id, load_settings
from .setup import SetupState, certificate_pair_is_usable, evaluate_setup_state

# Con menos días que esto hasta el vencimiento, el certificado es un aviso.
CERT_AVISO_DIAS = 30

_PASO_SETUP = {
    SetupState.UNINITIALIZED: "sin empezar",
    SetupState.CERTIFICATE_REQUIRED: "falta el certificado",
    SetupState.EMISOR_REQUIRED: "falta el emisor",
    SetupState.POINT_OF_SALE_REQUIRED: "faltan los puntos de venta",
}


class Nivel(StrEnum):
    OK = "ok"
    AVISO = "aviso"
    BLOQUEA = "bloquea"


_ORDEN = {Nivel.OK: 0, Nivel.AVISO: 1, Nivel.BLOQUEA: 2}


@dataclass(frozen=True)
class Accion:
    """Qué hacer. ``enlace`` es una página de la app; ``post`` es un botón
    que dispara una acción existente (siempre la elige el usuario)."""

    texto: str
    enlace: str | None = None
    post: str | None = None


@dataclass(frozen=True)
class Chequeo:
    titulo: str
    nivel: Nivel
    detalle: str
    accion: Accion | None = None


def peor_nivel(chequeos: list[Chequeo]) -> Nivel:
    return max((c.nivel for c in chequeos), key=_ORDEN.__getitem__, default=Nivel.OK)


def _fecha(momento: dt.datetime) -> str:
    return momento.strftime("%d/%m/%Y")


# --- chequeos locales ---


def diagnosticar_perfil(
    profile: EnvironmentProfile,
    conn: sqlite3.Connection,
    *,
    ahora: dt.datetime | None = None,
) -> list[Chequeo]:
    """Chequeos que no llaman a ARCA. Seguros con cualquier estado de setup."""
    ahora = ahora or dt.datetime.now(dt.UTC)
    return [
        _ambiente(profile),
        _certificado(profile, conn, ahora),
        _setup(profile, conn),
        *_emisor_y_puntos_de_venta(profile, conn),
        _base_de_datos(conn),
        _pendientes(conn),
        _backup(profile, conn),
        _avisos_arca(profile),
    ]


def _ambiente(profile: EnvironmentProfile) -> Chequeo:
    return Chequeo("Ambiente", Nivel.OK, profile.display_name)


def _certificado(
    profile: EnvironmentProfile, conn: sqlite3.Connection, ahora: dt.datetime
) -> Chequeo:
    titulo = "Certificado"
    subir = Accion("Subir el certificado y la clave.", enlace="/setup")
    renovar = Accion(
        "Generar uno nuevo en ARCA (Administración de Certificados Digitales) "
        "y subirlo.",
        enlace="/setup",
    )
    try:
        meta = load_certificate_metadata(profile)
    except CertificateError:
        return Chequeo(titulo, Nivel.BLOQUEA, "El certificado instalado no se "
                       "puede leer.", subir)
    if meta is None:
        return Chequeo(titulo, Nivel.BLOQUEA, "No hay certificado instalado.", subir)

    vence = meta.not_valid_after
    if vence <= ahora:
        return Chequeo(
            titulo, Nivel.BLOQUEA,
            f"CUIT {meta.cuit}. Venció el {_fecha(vence)}.", renovar,
        )
    if meta.not_valid_before > ahora:
        return Chequeo(
            titulo, Nivel.BLOQUEA,
            f"CUIT {meta.cuit}. Todavía no es válido (desde el "
            f"{_fecha(meta.not_valid_before)}).",
            subir,
        )
    if not certificate_pair_is_usable(profile):
        return Chequeo(
            titulo, Nivel.BLOQUEA,
            f"CUIT {meta.cuit}. El certificado y la clave no forman un par "
            "válido, o la clave tiene permisos demasiado abiertos.",
            subir,
        )
    try:
        resolve_profile_fiscal_cuit(conn, profile)
    except FiscalIdentityError as exc:
        return Chequeo(titulo, Nivel.BLOQUEA, str(exc), subir)

    dias = (vence - ahora).days
    detalle = f"CUIT {meta.cuit}. Vence el {_fecha(vence)}."
    if dias < CERT_AVISO_DIAS:
        return Chequeo(
            titulo, Nivel.AVISO,
            f"{detalle} Quedan {dias} días.", renovar,
        )
    return Chequeo(titulo, Nivel.OK, detalle)


def _setup(profile: EnvironmentProfile, conn: sqlite3.Connection) -> Chequeo:
    estado = evaluate_setup_state(profile, conn)
    if estado is SetupState.READY:
        return Chequeo("Configuración inicial", Nivel.OK, "Completa.")
    return Chequeo(
        "Configuración inicial",
        Nivel.BLOQUEA,
        f"Incompleta: {_PASO_SETUP[estado]}.",
        Accion("Terminar la configuración inicial.", enlace="/setup"),
    )


def _emisor_y_puntos_de_venta(
    profile: EnvironmentProfile, conn: sqlite3.Connection
) -> list[Chequeo]:
    configurar = Accion(
        "Completar el emisor en Configuración.", enlace="/configuracion"
    )
    if get_active_emisor_id(conn) is None:
        return [Chequeo("Emisor", Nivel.BLOQUEA, "No hay emisor activo.", configurar)]
    emisor = load_settings(conn).emisor
    if emisor.ambiente != profile.environment.value:
        return [Chequeo(
            "Emisor", Nivel.BLOQUEA,
            "El emisor activo es de otro ambiente.", configurar,
        )]
    if not emisor.completo:
        return [Chequeo(
            "Emisor", Nivel.BLOQUEA,
            f"{emisor.razon_social or 'Sin razón social'}: faltan datos del "
            "encabezado del comprobante.",
            configurar,
        )]
    chequeos = [Chequeo("Emisor", Nivel.OK, emisor.razon_social)]
    if not emisor.puntos_venta:
        chequeos.append(Chequeo(
            "Puntos de venta", Nivel.BLOQUEA, "El emisor no tiene puntos de venta.",
            Accion("Agregar un punto de venta en Configuración.",
                   enlace="/configuracion"),
        ))
    else:
        pvs = ", ".join(str(pv) for pv in emisor.puntos_venta)
        chequeos.append(Chequeo("Puntos de venta", Nivel.OK, pvs))
    return chequeos


def _base_de_datos(conn: sqlite3.Connection) -> Chequeo:
    titulo = "Base de datos"
    try:
        integridad = conn.execute("PRAGMA quick_check").fetchone()[0]
        version = current_version(conn)
    except sqlite3.DatabaseError:
        return Chequeo(titulo, Nivel.BLOQUEA, "No se pudo leer la base de datos.")
    esperada = latest_version()
    if integridad != "ok":
        return Chequeo(
            titulo, Nivel.BLOQUEA,
            "La verificación de integridad encontró errores.",
            Accion("Restaurar el perfil desde el backup (ver README, Backups)."),
        )
    if version != esperada:
        return Chequeo(
            titulo, Nivel.BLOQUEA,
            f"Esquema v{version}; esta versión de la app espera v{esperada}.",
            Accion("Usar la versión de la app que corresponde a este perfil."),
        )
    return Chequeo(titulo, Nivel.OK, f"Integridad OK, esquema v{version}.")


def _pendientes(conn: sqlite3.Connection) -> Chequeo:
    por_estado = repo.count_invoices_by_status(conn)
    n = por_estado.get(InvoiceStatus.UNKNOWN, 0) + por_estado.get(
        InvoiceStatus.SUBMITTING, 0
    )
    titulo = "Comprobantes a reconciliar"
    if n:
        return Chequeo(
            titulo, Nivel.AVISO,
            f"{n} sin respuesta concluyente de ARCA.",
            Accion("Abrirlos para reconciliar con ARCA.", enlace="/comprobantes"),
        )
    return Chequeo(titulo, Nivel.OK, "Ninguno.")


def _backup(profile: EnvironmentProfile, conn: sqlite3.Connection) -> Chequeo:
    titulo = "Backup"
    configurar = Accion("Configurar el bucket en Configuración, Backups.",
                        enlace="/configuracion")
    if not load_settings(conn).backup_s3_bucket:
        return Chequeo(
            titulo, Nivel.AVISO,
            "Solo local: sin bucket S3, perder esta máquina obliga a "
            "reconfigurar el perfil a mano.",
            configurar,
        )
    if not recipients_path(profile.paths).is_file():
        return Chequeo(
            titulo, Nivel.AVISO,
            "No hay claves age en recipients.txt: el backup no se puede cifrar.",
            Accion("Agregar las claves públicas (ver README, Backups)."),
        )
    estado = load_seed_backup_state(conn)
    ultimo = f" Último backup: {estado.last_success_at[:10]}." if (
        estado.last_success_at
    ) else ""
    if estado.status is SeedBackupStatus.OK:
        return Chequeo(titulo, Nivel.OK, f"Al día.{ultimo}")
    if estado.status is SeedBackupStatus.FAILED:
        # El texto del error puede traer rutas locales: no se muestra.
        return Chequeo(
            titulo, Nivel.AVISO,
            f"Falló el último intento de subida.{ultimo}",
            Accion("Revisar el log; se reintenta al abrir la app."),
        )
    if estado.status is SeedBackupStatus.PENDING:
        return Chequeo(titulo, Nivel.AVISO, f"Hay cambios sin subir.{ultimo}")
    return Chequeo(
        titulo, Nivel.AVISO,
        "Todavía no se subió ningún backup: se sube al cambiar la configuración.",
    )


def _avisos_arca(profile: EnvironmentProfile) -> Chequeo:
    eventos = load_events(profile.paths.arca_events).events
    if not eventos:
        return Chequeo("Avisos de ARCA", Nivel.OK, "Ninguno vigente.")
    return Chequeo(
        "Avisos de ARCA", Nivel.AVISO,
        " · ".join(f"{e.code}: {e.msg}" for e in eventos),
    )


# --- chequeos contra ARCA (a pedido) ---


def diagnosticar_arca(
    profile: EnvironmentProfile,
    conn: sqlite3.Connection,
    wsfex: WsfexClient,
) -> list[Chequeo]:
    """FEXDummy y, por punto de venta, último número en ARCA vs. local."""
    if evaluate_setup_state(profile, conn) is not SetupState.READY:
        return [Chequeo(
            "ARCA", Nivel.BLOQUEA,
            "No se puede verificar hasta completar la configuración inicial.",
            Accion("Terminar la configuración inicial.", enlace="/setup"),
        )]
    try:
        estado = wsfex.dummy()
    except (WsfexError, httpx.HTTPError) as exc:
        return [Chequeo(
            "Conexión con ARCA", Nivel.BLOQUEA, f"ARCA no responde: {exc}",
            Accion("Reintentar en unos minutos; ver si ARCA anunció mantenimiento."),
        )]
    servidores = ", ".join(f"{k}: {v}" for k, v in sorted(estado.items()))
    if not estado or any(v.upper() != "OK" for v in estado.values()):
        return [Chequeo(
            "Conexión con ARCA", Nivel.BLOQUEA, servidores or "Respuesta vacía.",
            Accion("Reintentar en unos minutos."),
        )]
    chequeos = [Chequeo("Conexión con ARCA", Nivel.OK, servidores)]
    for pv in load_settings(conn).emisor.puntos_venta:
        chequeos.append(_numeracion(conn, wsfex, pv))
    return chequeos


def _numeracion(conn: sqlite3.Connection, wsfex: WsfexClient, pv: int) -> Chequeo:
    titulo = f"Numeración PV {pv}"
    try:
        ultimo_arca = wsfex.get_last_cmp(pv, CBTE_TIPO_FACTURA_E)
    except WsaaError as exc:
        return Chequeo(
            titulo, Nivel.BLOQUEA, f"No se pudo autenticar con ARCA: {exc}",
            Accion("Verificar que el certificado esté asociado al servicio wsfex."),
        )
    except (WsfexError, httpx.HTTPError) as exc:
        return Chequeo(titulo, Nivel.BLOQUEA, f"ARCA no respondió: {exc}")
    local = repo.max_authorized_cbte_nro(conn, pv, CBTE_TIPO_FACTURA_E)
    if ultimo_arca == local:
        return Chequeo(titulo, Nivel.OK, f"Último comprobante {local}, igual en ARCA.")
    if ultimo_arca > local:
        return Chequeo(
            titulo, Nivel.BLOQUEA,
            f"ARCA va por el {ultimo_arca} y el registro local por el {local}: "
            "hay comprobantes emitidos desde otro lado.",
            Accion("Sincronizar desde ARCA", post="/ui/registry/catch-up"),
        )
    return Chequeo(
        titulo, Nivel.BLOQUEA,
        f"El registro local va por el {local} pero ARCA por el {ultimo_arca}: "
        "no emitir hasta investigar.",
    )

"""Configuración: la página y cómo se arma (emisores y backups en la misma
pantalla; settings de dominio en la DB, no en el .env). Las acciones viven en
``emisores.py`` y ``backups.py`` y vuelven a esta página con
``_pagina_configuracion``."""

from __future__ import annotations

import datetime as dt
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import repo
from ...api.deps import ServiceDep
from ...seed_backup_sync import BackupDestination, SeedBackupState, SeedBackupStatus
from ...service import (
    DomainError,
)
from ...settings import (
    get_active_emisor_id,
)
from ..constants import (
    AvisoConfiguracion,
    ErrorConfiguracion,
    EstadoBackupUI,
    Pagina,
)
from ._comun import (
    templates,
)

router = APIRouter()


def _fecha_backup(iso: str | None) -> str | None:
    """Fecha y hora local de un timestamp de backup (se guarda en UTC)."""
    if not iso:
        return None
    try:
        momento = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=dt.UTC)
    return momento.astimezone().strftime("%d/%m/%Y %H:%M")


_DONDE_BACKUP: dict[str, str] = {
    BackupDestination.S3: ", subido a S3",
    BackupDestination.LOCAL: ", guardado solo en esta máquina (sin bucket S3)",
}


def _estado_ui(estado: EstadoBackupUI, detalle: str) -> dict[str, str]:
    return {"badge": estado.badge, "titulo": estado.titulo, "detalle": detalle}


def _estado_backup(estado: SeedBackupState) -> dict[str, str]:
    """Estado del último backup para la UI. El texto del último error no se
    muestra: puede traer rutas locales (queda en el log)."""
    ultimo = _fecha_backup(estado.last_success_at)
    # Un backup anterior a que se guardara el destino no dice dónde quedó:
    # no se adivina.
    donde = _DONDE_BACKUP.get(estado.last_destination or "", "")
    previo = f" Último backup: {ultimo}{donde}." if ultimo else ""
    if estado.status is SeedBackupStatus.OK and ultimo:
        return _estado_ui(EstadoBackupUI.AL_DIA, f"Último backup: {ultimo}{donde}.")
    if estado.status is SeedBackupStatus.FAILED:
        intento = _fecha_backup(estado.last_attempt_at)
        cuando = f" ({intento})" if intento else ""
        return _estado_ui(
            EstadoBackupUI.ERROR,
            f"Falló el último intento{cuando}; el detalle "
            f"quedó en el log de la app.{previo}",
        )
    if estado.status is SeedBackupStatus.PENDING:
        return _estado_ui(
            EstadoBackupUI.PENDIENTE, f"Hay cambios sin respaldar.{previo}"
        )
    return _estado_ui(
        EstadoBackupUI.SIN_BACKUP,
        "Todavía no se hizo ningún backup de este ambiente.",
    )


def _contexto_backups(
    service, *, form_nombre: str = "", form_clave: str = ""
) -> dict[str, object]:
    """Identidad de esta máquina, máquinas con acceso y estado del backup.

    Solo claves públicas: la identidad privada nunca llega a la plantilla.
    """
    error_identidad = error_maquinas = None
    try:
        clave_propia = service.machine_public_key()
    except DomainError as exc:
        clave_propia, error_identidad = None, str(exc)
    try:
        maquinas = service.list_backup_machines()
    except DomainError as exc:
        maquinas, error_maquinas = [], str(exc)
    return {
        "clave_propia": clave_propia,
        "error_identidad": error_identidad,
        "propia_en_lista": any(m.clave == clave_propia for m in maquinas),
        "maquinas": maquinas,
        "error_maquinas": error_maquinas,
        "estado": _estado_backup(service.get_seed_backup_state()),
        # Vacío: casi siempre se pega la clave de OTRA PC, y con el nombre
        # de esta como default era fácil guardarla con el nombre equivocado.
        "form_nombre": form_nombre,
        "form_clave": form_clave,
    }


def _pagina_configuracion(
    request: Request,
    service,
    editando=None,
    error: str | None = None,
    aviso: str | None = None,
    status_code: int = HTTPStatus.OK,
    backup: dict[str, object] | None = None,
):
    activo_id = get_active_emisor_id(service.conn)
    return templates.TemplateResponse(
        request,
        "configuracion.html",
        {
            "s": service.get_settings(),
            "emisores": service.list_emisores(),
            "activo_id": activo_id,
            "editando": editando,
            "error": error,
            "aviso": aviso,
            "backup": backup or _contexto_backups(service),
        },
        status_code=status_code,
    )


_AVISOS_CONFIGURACION: dict[str, str] = {
    AvisoConfiguracion.GUARDADO: "Emisor guardado.",
    AvisoConfiguracion.BACKUP: "Configuración de backups guardada.",
    AvisoConfiguracion.ACTIVADO: "Emisor activo actualizado.",
    AvisoConfiguracion.IDENTIDAD: "Identidad de esta máquina creada.",
    AvisoConfiguracion.MAQUINA: (
        "Máquina agregada. El backup se actualiza en unos segundos."
    ),
    AvisoConfiguracion.MAQUINA_QUITADA: (
        "Máquina quitada. El backup se actualiza en unos segundos."
    ),
    AvisoConfiguracion.BACKUP_HECHO: "Backup hecho.",
}
_ERRORES_CONFIGURACION: dict[str, str] = {
    ErrorConfiguracion.BACKUP: "No se pudo hacer el backup. El detalle quedó en "
    "el log de la app; se reintenta al abrirla.",
}


@router.get(Pagina.CONFIGURACION, response_class=HTMLResponse)
def configuracion(
    request: Request,
    service: ServiceDep,
    edit: str | None = None,
    aviso: str = "",
    error: str = "",
):
    editando = repo.get_emisor(service.conn, edit) if edit else None
    return _pagina_configuracion(
        request,
        service,
        editando=editando,
        aviso=_AVISOS_CONFIGURACION.get(aviso),
        error=_ERRORES_CONFIGURACION.get(error),
    )

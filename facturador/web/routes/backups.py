"""Configuración → Backups: bucket S3, identidad de esta máquina, máquinas
con acceso y backup a demanda."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ...api.deps import ServiceDep
from ...backup_machines import nombre_del_equipo
from ...schemas import (
    BackupSettingsIn,
)
from ...seed_backup_sync import SeedBackupStatus
from ...service import (
    ServiceError,
)
from ...settings import (
    BACKUP_PREFIX_DEFAULT,
)
from ..constants import (
    AvisoConfiguracion,
    ErrorConfiguracion,
    Pagina,
)
from .configuracion import (
    _contexto_backups,
    _pagina_configuracion,
)

router = APIRouter()


@router.post("/ui/configuracion/backup", response_class=HTMLResponse)
def guardar_backup(
    request: Request,
    service: ServiceDep,
    backup_s3_bucket: str = Form(""),
    backup_s3_prefix: str = Form(BACKUP_PREFIX_DEFAULT),
):
    try:
        payload = BackupSettingsIn(
            backup_s3_bucket=backup_s3_bucket,
            backup_s3_prefix=backup_s3_prefix,
        )
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return _pagina_configuracion(
            request,
            service,
            error=f"Datos inválidos: {detalles}",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    service.update_backup_settings(payload)
    return RedirectResponse(
        f"{Pagina.CONFIGURACION}?aviso={AvisoConfiguracion.BACKUP}",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/configuracion/backup/identidad", response_class=HTMLResponse)
def crear_identidad_maquina(request: Request, service: ServiceDep):
    try:
        service.create_machine_identity()
    except ServiceError as exc:
        return _pagina_configuracion(
            request,
            service,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        f"{Pagina.CONFIGURACION}?aviso={AvisoConfiguracion.IDENTIDAD}#backups",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/configuracion/backup/esta-maquina", response_class=HTMLResponse)
def agregar_esta_maquina(request: Request, service: ServiceDep):
    try:
        service.add_this_machine(nombre_del_equipo())
    except ServiceError as exc:
        return _pagina_configuracion(
            request,
            service,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        f"{Pagina.CONFIGURACION}?aviso={AvisoConfiguracion.MAQUINA}#backups",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/configuracion/backup/maquinas", response_class=HTMLResponse)
def agregar_maquina(
    request: Request,
    service: ServiceDep,
    nombre: str = Form(""),
    clave: str = Form(""),
):
    try:
        service.add_backup_machine(nombre, clave)
    except ServiceError as exc:
        return _pagina_configuracion(
            request,
            service,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            backup=_contexto_backups(service, form_nombre=nombre, form_clave=clave),
        )
    return RedirectResponse(
        f"{Pagina.CONFIGURACION}?aviso={AvisoConfiguracion.MAQUINA}#backups",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/configuracion/backup/maquinas/quitar", response_class=HTMLResponse)
def quitar_maquina(request: Request, service: ServiceDep, clave: str = Form("")):
    try:
        service.remove_backup_machine(clave)
    except ServiceError as exc:
        return _pagina_configuracion(
            request,
            service,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        f"{Pagina.CONFIGURACION}?aviso={AvisoConfiguracion.MAQUINA_QUITADA}#backups",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/configuracion/backup/ahora", response_class=HTMLResponse)
def hacer_backup_ahora(request: Request, service: ServiceDep):
    """Lo mismo que ``POST /backup/seed``: corre el backup ya (con S3 si hay
    bucket) y vuelve a Configuración con el resultado."""
    try:
        estado = service.run_seed_backup_now()
    except ServiceError as exc:
        return _pagina_configuracion(
            request, service, error=str(exc), status_code=HTTPStatus.CONFLICT
        )
    if estado.status is SeedBackupStatus.OK:
        return RedirectResponse(
            f"{Pagina.CONFIGURACION}?aviso={AvisoConfiguracion.BACKUP_HECHO}#backups",
            status_code=HTTPStatus.SEE_OTHER,
        )
    return RedirectResponse(
        f"{Pagina.CONFIGURACION}?error={ErrorConfiguracion.BACKUP}#backups",
        status_code=HTTPStatus.SEE_OTHER,
    )

"""Configuración: emisores y backups (settings de dominio en la DB, no en el .env)."""

from __future__ import annotations

import datetime as dt
from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ... import repo
from ...api.deps import ServiceDep
from ...backup_machines import nombre_del_equipo
from ...diagnostics import diagnosticar_arca, diagnosticar_perfil, peor_nivel
from ...schemas import (
    BackupSettingsIn,
    EmisorCreateIn,
    EmisorUpdateIn,
)
from ...seed_backup_sync import BackupDestination, SeedBackupState, SeedBackupStatus
from ...service import (
    DomainError,
    ServiceError,
)
from ...settings import (
    BACKUP_PREFIX_DEFAULT,
    CONDICION_IVA_DEFAULT,
    get_active_emisor_id,
)
from ..constants import (
    AvisoConfiguracion,
    ErrorConfiguracion,
    EstadoBackupUI,
)
from ._comun import (
    _parse_puntos_venta_form,
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


@router.get("/diagnostico", response_class=HTMLResponse)
def diagnostico(request: Request, service: ServiceDep, arca: bool = False):
    """¿Se puede emitir con este perfil? Los chequeos locales corren siempre;
    los de ARCA solo con ``?arca=1`` (botón), porque pueden tardar."""
    profile = request.app.state.profile
    # ARCA primero: cada respuesta actualiza los avisos guardados, que el
    # chequeo local lee después.
    remotos = diagnosticar_arca(profile, service.conn, service.wsfex) if arca else None
    locales = diagnosticar_perfil(profile, service.conn)
    return templates.TemplateResponse(
        request,
        "diagnostico.html",
        {
            "locales": locales,
            "remotos": remotos,
            "arca_verificado": arca,
            "nivel": peor_nivel(locales + (remotos or [])),
        },
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


@router.get("/configuracion", response_class=HTMLResponse)
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


@router.post("/ui/emisores", response_class=HTMLResponse)
async def guardar_emisor(
    request: Request,
    service: ServiceDep,
    emisor_id: str = Form(""),
    razon_social: str = Form(""),
    domicilio: str = Form(""),
    iibb: str = Form(""),
    inicio_actividades: str = Form(""),
    condicion_iva: str = Form(CONDICION_IVA_DEFAULT),
    puntos_venta: str = Form("1"),
):
    existente = repo.get_emisor(service.conn, emisor_id) if emisor_id else None
    form = await request.form()
    if "ambiente" in form:
        return _pagina_configuracion(
            request,
            service,
            editando=existente,
            error="Datos inválidos: el ambiente sale del perfil activo y no se envía",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        pvs = _parse_puntos_venta_form(puntos_venta)
    except ValueError:
        return _pagina_configuracion(
            request,
            service,
            editando=existente,
            error="Datos inválidos: los puntos de venta deben ser números"
            " separados por coma",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        if emisor_id:
            update_payload = EmisorUpdateIn(
                razon_social=razon_social,
                domicilio=domicilio,
                iibb=iibb,
                inicio_actividades=inicio_actividades,
                condicion_iva=condicion_iva,
                puntos_venta=pvs,
            )
        else:
            create_payload = EmisorCreateIn(
                razon_social=razon_social,
                domicilio=domicilio,
                iibb=iibb,
                inicio_actividades=inicio_actividades,
                condicion_iva=condicion_iva,
                puntos_venta=pvs,
            )
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return _pagina_configuracion(
            request,
            service,
            editando=existente,
            error=f"Datos inválidos: {detalles}",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        if emisor_id:
            service.update_emisor(emisor_id, update_payload)
        else:
            service.create_emisor(create_payload)
    except ServiceError as exc:
        return _pagina_configuracion(
            request,
            service,
            editando=existente,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        f"/configuracion?aviso={AvisoConfiguracion.GUARDADO}",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/emisores/{emisor_id}/activar")
def activar_emisor(request: Request, service: ServiceDep, emisor_id: str):
    try:
        service.activate_emisor(emisor_id)
    except ServiceError:
        return RedirectResponse("/configuracion", status_code=HTTPStatus.SEE_OTHER)
    return RedirectResponse(
        f"/configuracion?aviso={AvisoConfiguracion.ACTIVADO}",
        status_code=HTTPStatus.SEE_OTHER,
    )


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
        f"/configuracion?aviso={AvisoConfiguracion.BACKUP}",
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
        f"/configuracion?aviso={AvisoConfiguracion.IDENTIDAD}#backups",
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
        f"/configuracion?aviso={AvisoConfiguracion.MAQUINA}#backups",
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
        f"/configuracion?aviso={AvisoConfiguracion.MAQUINA}#backups",
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
        f"/configuracion?aviso={AvisoConfiguracion.MAQUINA_QUITADA}#backups",
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
            f"/configuracion?aviso={AvisoConfiguracion.BACKUP_HECHO}#backups",
            status_code=HTTPStatus.SEE_OTHER,
        )
    return RedirectResponse(
        f"/configuracion?error={ErrorConfiguracion.BACKUP}#backups",
        status_code=HTTPStatus.SEE_OTHER,
    )

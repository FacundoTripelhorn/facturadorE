"""Setup / onboarding: certificado, emisor y punto de venta."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ... import repo
from ...api.deps import ServiceDep
from ...certs import (
    CertificateError,
    CertificateMetadata,
    load_certificate_metadata,
    store_certificate_pair,
)
from ...fiscal_identity import FiscalIdentityError, get_sealed_fiscal_cuit
from ...schemas import (
    EmisorCreateIn,
    EmisorUpdateIn,
)
from ...service import (
    ServiceError,
)
from ...settings import (
    CONDICION_IVA_DEFAULT,
    get_active_emisor_id,
)
from ...setup import SetupState, reconcile_setup_state
from ..constants import AvisoSetup
from ._comun import (
    _parse_puntos_venta_form,
    templates,
)

router = APIRouter()


def _cert_metadata_seguro(profile) -> CertificateMetadata | None:
    try:
        return load_certificate_metadata(profile)
    except CertificateError:
        return None


def _emisor_onboarding_row(conn, env) -> sqlite3.Row | None:
    """Emisor a retomar en onboarding: activo o el primero del perfil actual."""
    active_id = get_active_emisor_id(conn)
    if active_id:
        row = repo.get_emisor(conn, active_id)
        if row is not None and row["ambiente"] == env:
            return row
    for row in repo.list_emisores(conn):
        if row["ambiente"] == env:
            return row
    return None


def _error_emisor_otro_ambiente(row: sqlite3.Row, env) -> str:
    return (
        f"El emisor {row['id']} pertenece al ambiente {row['ambiente']} "
        f"y este backend corre el perfil {env}: no se puede editar desde "
        "el setup de este perfil."
    )


def _pagina_setup(
    request: Request,
    service,
    *,
    state: SetupState | None = None,
    cert_meta: CertificateMetadata | None = None,
    emisor_editando=None,
    error: str | None = None,
    aviso: AvisoSetup | None = None,
    status_code: int = HTTPStatus.OK,
):
    profile = request.app.state.profile
    if state is None:
        state = reconcile_setup_state(profile, service.conn)
    if cert_meta is None:
        cert_meta = _cert_metadata_seguro(profile)
    if emisor_editando is None and state in (
        SetupState.EMISOR_REQUIRED,
        SetupState.POINT_OF_SALE_REQUIRED,
    ):
        emisor_editando = _emisor_onboarding_row(service.conn, profile.environment)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "state": state.value,
            "cert_meta": cert_meta,
            "emisor_editando": emisor_editando,
            "error": error,
            "aviso": aviso,
        },
        status_code=status_code,
    )


@router.get("/setup", response_class=HTMLResponse)
def setup_pagina(request: Request, service: ServiceDep, aviso: str = ""):
    """Onboarding por perfil: certificado, emisor y punto de venta."""
    return _pagina_setup(request, service, aviso=AvisoSetup.desde_query(aviso))


@router.post("/ui/setup/certificado", response_class=HTMLResponse)
async def instalar_certificado_setup(
    request: Request,
    service: ServiceDep,
    certificado: UploadFile = File(...),  # noqa: B008
    clave: UploadFile = File(...),  # noqa: B008
):
    """Valida y persiste el par cert/key del perfil (vía UI)."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)

    if certificado.filename is None or clave.filename is None:
        return _pagina_setup(
            request,
            service,
            state=state,
            error="Hay que subir el certificado y la clave privada juntos.",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    try:
        cert_bytes = await certificado.read()
        key_bytes = await clave.read()
    except OSError:
        return _pagina_setup(
            request,
            service,
            state=state,
            error="No se pudieron leer los archivos subidos.",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    if not cert_bytes.strip() or not key_bytes.strip():
        return _pagina_setup(
            request,
            service,
            state=state,
            error="El certificado y la clave privada no pueden estar vacíos.",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    try:
        store_certificate_pair(
            profile,
            cert_bytes,
            key_bytes,
            sealed_cuit=get_sealed_fiscal_cuit(service.conn),
        )
    except (CertificateError, FiscalIdentityError) as exc:
        return _pagina_setup(
            request,
            service,
            state=state,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    reconcile_setup_state(profile, service.conn)
    return RedirectResponse(
        f"/setup?aviso={AvisoSetup.INSTALADO}", status_code=HTTPStatus.SEE_OTHER
    )


@router.post("/ui/setup/emisor", response_class=HTMLResponse)
async def guardar_emisor_setup(
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
    """Alta/edición del primer emisor durante onboarding."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)
    if state is not SetupState.EMISOR_REQUIRED:
        return RedirectResponse("/setup", status_code=HTTPStatus.SEE_OTHER)

    onboarding_row = _emisor_onboarding_row(service.conn, service.config.env)
    if onboarding_row is not None:
        if emisor_id and emisor_id != onboarding_row["id"]:
            return _pagina_setup(
                request,
                service,
                state=state,
                emisor_editando=onboarding_row,
                error=(
                    "Datos inválidos: el emisor no coincide con "
                    "el paso de setup en curso"
                ),
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        existente = onboarding_row
        emisor_id = onboarding_row["id"]
    elif emisor_id:
        ajeno = repo.get_emisor(service.conn, emisor_id)
        if ajeno is not None and ajeno["ambiente"] != service.config.env:
            return _pagina_setup(
                request,
                service,
                state=state,
                error=_error_emisor_otro_ambiente(ajeno, service.config.env),
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        return _pagina_setup(
            request,
            service,
            state=state,
            error=(
                "Datos inválidos: el emisor no coincide con el paso de setup en curso"
            ),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    else:
        existente = None

    form = await request.form()
    if "ambiente" in form:
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=existente,
            error="Datos inválidos: el ambiente sale del perfil activo y no se envía",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        pvs = _parse_puntos_venta_form(puntos_venta)
    except ValueError:
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=existente,
            error="Datos inválidos: los puntos de venta deben ser números"
            " separados por coma",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        if emisor_id:
            row = service.update_emisor(
                emisor_id,
                EmisorUpdateIn(
                    razon_social=razon_social,
                    domicilio=domicilio,
                    iibb=iibb,
                    inicio_actividades=inicio_actividades,
                    condicion_iva=condicion_iva,
                    puntos_venta=pvs,
                ),
            )
        else:
            row = service.create_emisor(
                EmisorCreateIn(
                    razon_social=razon_social,
                    domicilio=domicilio,
                    iibb=iibb,
                    inicio_actividades=inicio_actividades,
                    condicion_iva=condicion_iva,
                    puntos_venta=pvs,
                )
            )
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=existente,
            error=f"Datos inválidos: {detalles}",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        service.activate_emisor(row["id"])
    except ServiceError as exc:
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=existente,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    new_state = reconcile_setup_state(profile, service.conn)
    if new_state is SetupState.READY and state is not SetupState.READY:
        # Fin de onboarding (/setup is setup-guard exempt).
        service.notify_seed_backup("onboarding_completed")
    aviso = AvisoSetup.LISTO if new_state is SetupState.READY else AvisoSetup.EMISOR
    return RedirectResponse(f"/setup?aviso={aviso}", status_code=HTTPStatus.SEE_OTHER)


@router.post("/ui/setup/punto-venta", response_class=HTMLResponse)
async def guardar_punto_venta_setup(
    request: Request,
    service: ServiceDep,
    puntos_venta: str = Form(""),
):
    """Habilita puntos de venta del emisor activo durante onboarding."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)
    if state is not SetupState.POINT_OF_SALE_REQUIRED:
        return RedirectResponse("/setup", status_code=HTTPStatus.SEE_OTHER)

    emisor_row = _emisor_onboarding_row(service.conn, service.config.env)
    if emisor_row is None:
        return _pagina_setup(
            request,
            service,
            state=state,
            error="No hay emisor activo para configurar el punto de venta.",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    if emisor_row["ambiente"] != service.config.env:
        return _pagina_setup(
            request,
            service,
            state=state,
            error=_error_emisor_otro_ambiente(emisor_row, service.config.env),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        pvs = _parse_puntos_venta_form(puntos_venta)
    except ValueError:
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=emisor_row,
            error="Datos inválidos: los puntos de venta deben ser números"
            " separados por coma",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        payload = EmisorUpdateIn(
            razon_social=emisor_row["razon_social"],
            domicilio=emisor_row["domicilio"],
            iibb=emisor_row["iibb"],
            inicio_actividades=emisor_row["inicio_actividades"],
            condicion_iva=emisor_row["condicion_iva"] or CONDICION_IVA_DEFAULT,
            puntos_venta=pvs,
        )
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=emisor_row,
            error=f"Datos inválidos: {detalles}",
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        service.update_emisor(emisor_row["id"], payload)
    except ServiceError as exc:
        return _pagina_setup(
            request,
            service,
            state=state,
            emisor_editando=emisor_row,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    new_state = reconcile_setup_state(profile, service.conn)
    if new_state is SetupState.READY and state is not SetupState.READY:
        # Fin de onboarding (/setup is setup-guard exempt).
        service.notify_seed_backup("onboarding_completed")
    aviso = (
        AvisoSetup.LISTO if new_state is SetupState.READY else AvisoSetup.PUNTO_VENTA
    )
    return RedirectResponse(f"/setup?aviso={aviso}", status_code=HTTPStatus.SEE_OTHER)

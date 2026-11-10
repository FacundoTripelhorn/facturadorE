"""Configuración → Emisores: alta, edición y emisor activo."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ... import repo
from ...api.deps import ServiceDep
from ...schemas import (
    EmisorCreateIn,
    EmisorUpdateIn,
)
from ...service import (
    ServiceError,
)
from ...settings import (
    CONDICION_IVA_DEFAULT,
)
from ..constants import (
    AvisoConfiguracion,
)
from ._comun import (
    _parse_puntos_venta_form,
)
from .configuracion import (
    _pagina_configuracion,
)

router = APIRouter()


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

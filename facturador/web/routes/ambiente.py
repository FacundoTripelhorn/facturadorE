"""Cambio de ambiente por reinicio (ADR 0001)."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...api.deps import ServiceDep
from ...launcher.switch import (
    is_launcher_supervised,
    write_change_environment_request,
)
from ..constants import Accion
from ._comun import (
    templates,
)

router = APIRouter()


@router.post(Accion.CAMBIAR_AMBIENTE, response_class=HTMLResponse)
def pedir_cambiar_ambiente(request: Request, service: ServiceDep):
    """Pide al launcher un reinicio: no muta ambiente, clientes ARCA ni DB."""
    profile = request.app.state.profile
    # Cinturón: el servicio y el perfil deben seguir apuntando al mismo
    # ambiente inmutable (no hay hot-switch).
    if service.config.env != profile.environment:
        return templates.TemplateResponse(
            request,
            "cambiar_ambiente.html",
            {
                "error": (
                    "Inconsistencia interna de ambiente; "
                    "reiniciá la app desde el launcher."
                ),
                "pending": False,
            },
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    if not is_launcher_supervised():
        return templates.TemplateResponse(
            request,
            "cambiar_ambiente.html",
            {
                "error": (
                    "El cambio de ambiente solo está disponible cuando "
                    "abrís FacturadorE desde el launcher."
                ),
                "pending": False,
            },
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    write_change_environment_request(profile.paths, profile.environment)
    # El ambiente del proceso NO cambia: solo el pedido en disco.
    assert service.config.env == profile.environment
    return templates.TemplateResponse(
        request,
        "cambiar_ambiente.html",
        {"pending": True, "error": None},
    )

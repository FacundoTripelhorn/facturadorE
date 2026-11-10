"""Clientes."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ... import repo
from ...api.deps import ServiceDep
from ...certs import (
    CertificateError,
)
from ...constants import (
    MONEDA_DOL,
)
from ...schemas import (
    ClientIn,
)
from ...service import (
    ServiceError,
)
from ..constants import Accion, Pagina
from ._comun import (
    templates,
)

router = APIRouter()


def _param_options_by_description(rows: list) -> list:
    """Orden alfabético por descripción (País / CUIT país en los selects)."""
    return sorted(
        rows,
        key=lambda r: (
            str(r["description"] or "").casefold(),
            str(r["code"]),
        ),
    )


def _pagina_clientes(
    request: Request,
    service,
    editando=None,
    error: str | None = None,
    status_code: int = HTTPStatus.OK,
):
    params: dict[str, list] = {}
    try:
        for kind in ("pais", "cuit_pais", "moneda", "idioma"):
            rows = service.get_params(kind)
            if kind in ("pais", "cuit_pais"):
                rows = _param_options_by_description(rows)
            params[kind] = rows
    except (ServiceError, CertificateError, OSError) as exc:
        # Sin certs / par inválido el refresh de params no debe 500:
        # el template muestra el mensaje y el CRUD offline sigue usable
        # cuando el cache ya está sembrado. OSError cubre cert ausente
        # (FileNotFoundError al leer el PEM); CertificateError el par
        # inválido si el refresh pasa por la validación del par cert/key.
        error = error or str(exc)
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "clientes": repo.list_clients(service.conn),
            "editando": editando,
            "params": params,
            "error": error,
        },
        status_code=status_code,
    )


@router.get(Pagina.CLIENTES, response_class=HTMLResponse)
def clientes(request: Request, service: ServiceDep, edit: str | None = None):
    editando = repo.get_client(service.conn, edit) if edit else None
    return _pagina_clientes(request, service, editando=editando)


@router.post(Accion.CLIENTES, response_class=HTMLResponse)
def guardar_cliente(
    request: Request,
    service: ServiceDep,
    client_id: str = Form(""),
    razon_social: str = Form(...),
    domicilio: str = Form(""),
    pais_dst: int = Form(...),
    cuit_pais: int = Form(...),
    id_impositivo: str = Form(""),
    moneda_default: str = Form(MONEDA_DOL),
    idioma_default: int = Form(1),
    forma_pago_default: str = Form("WIRE TRANSFER"),
    descripcion_default: str = Form(""),
    cuenta_recaudadora: str = Form(""),
    is_default: bool = Form(False),
):
    try:
        payload = ClientIn(
            razon_social=razon_social,
            domicilio=domicilio,
            pais_dst=pais_dst,
            cuit_pais=cuit_pais,
            id_impositivo=id_impositivo,
            moneda_default=moneda_default,
            idioma_default=idioma_default,
            forma_pago_default=forma_pago_default,
            descripcion_default=descripcion_default,
            cuenta_recaudadora=cuenta_recaudadora,
            is_default=is_default,
        )
        if client_id:
            service.update_client(client_id, payload)
        else:
            service.create_client(payload)
    except (ValidationError, ServiceError, CertificateError, OSError) as exc:
        # Mismo caso que GET /clientes: sin certs/params cache no debe 500.
        return _pagina_clientes(
            request,
            service,
            error=str(exc),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(Pagina.CLIENTES, status_code=HTTPStatus.SEE_OTHER)

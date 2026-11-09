"""Emitir: el formulario de nueva factura (la única página con inputs de factura)."""

from __future__ import annotations

import datetime as dt
from http import HTTPStatus

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ... import repo
from ...api.deps import ServiceDep
from ...arca.wsfex import WsfexError
from ...constants import (
    MONEDA_DOL,
)
from ...schemas import (
    InvoiceCreate,
)
from ...service import (
    ServiceError,
)
from ..numeros import parse_importe
from ._comun import (
    AVISOS,
    _fecha_iso_a_arca,
    templates,
)

router = APIRouter()


def _contexto_form(service, error: str | None = None, aviso: str | None = None):
    # Sin datos de emisor no hay form: la UI dirige a Configuración (los
    # datos van al PDF y create_invoice los exige).
    settings = service.get_settings()
    emisor_ok = settings.emisor.completo
    clientes = repo.list_clients(service.conn)
    default = next((c for c in clientes if c["is_default"]), None)
    ctz = ctz_fecha = None
    if emisor_ok and clientes:
        try:
            # Cotización ARCA del día, informativa (§0: nunca se carga a mano).
            ctz_dec, ctz_fecha = service.wsfex.get_ctz(MONEDA_DOL)
            ctz = format(ctz_dec, "f")
        except (WsfexError, httpx.HTTPError):
            pass  # sin cotización el form sigue usable; authorize revalida
    return {
        "emisor_ok": emisor_ok,
        "emisor": settings.emisor,
        "puntos_venta": settings.emisor.puntos_venta,
        "clientes": clientes,
        "default": default,
        "ctz": ctz,
        "ctz_fecha": ctz_fecha,
        "hoy": dt.date.today().isoformat(),
        "error": error,
        "aviso": aviso,
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, service: ServiceDep, aviso: str = ""):
    return templates.TemplateResponse(
        request, "home.html", _contexto_form(service, aviso=AVISOS.get(aviso))
    )


def _detalles_validacion(exc: ValidationError) -> str:
    """Mensajes de Pydantic para el form, sin el prefijo "Value error, " que
    Pydantic agrega a los errores de nuestros validadores."""
    return "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())


@router.post("/ui/facturas")
def generar_borrador(
    request: Request,
    service: ServiceDep,
    imp_total: str = Form(...),
    punto_venta: str = Form(""),
    client_id: str = Form(""),
    descripcion: str = Form(""),
    fecha_pago: str = Form(""),
    obs: str = Form(""),
):
    """Crea SOLO el borrador y redirige a su revisión. Nada viaja a ARCA
    hasta el Confirmar explícito de la página de revisión."""
    pv: int | None = None
    if punto_venta.strip():
        try:
            pv = int(punto_venta)
        except ValueError:
            return templates.TemplateResponse(
                request,
                "home.html",
                _contexto_form(service, error="Punto de venta inválido"),
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
    try:
        monto = parse_importe(imp_total)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "home.html",
            _contexto_form(service, error=f"Datos inválidos: monto {exc}"),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    try:
        payload = InvoiceCreate(
            imp_total=monto,
            punto_venta=pv,
            client_id=client_id or None,
            descripcion=descripcion or None,
            fecha_pago=_fecha_iso_a_arca(fecha_pago),
            obs=obs,
        )
        draft = service.create_invoice(payload)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "home.html",
            _contexto_form(
                service, error=f"Datos inválidos: {_detalles_validacion(exc)}"
            ),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    except ServiceError as exc:
        return templates.TemplateResponse(
            request,
            "home.html",
            _contexto_form(service, error=str(exc)),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    return RedirectResponse(
        f"/facturas/{draft['id']}/revisar", status_code=HTTPStatus.SEE_OTHER
    )

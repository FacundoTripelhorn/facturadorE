"""Rutas HTML del frontend. Lógica cero: cada handler arma el contexto con
el InvoiceService/repo y renderiza; las reglas de dominio viven en service.

El caso feliz semanal (§0.1) es UN submit: crea el draft y lo autoriza en el
mismo POST. Los errores de dominio se muestran en el partial de resultado,
nunca como JSON crudo.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import repo
from ..api.deps import ServiceDep
from ..arca.wsfex import WsfexError
from ..constants import MONEDA_DOL, InvoiceStatus
from ..schemas import ClientIn, InvoiceCreate
from ..service import ConflictError, ServiceError

router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)

STATUS_LABELS = {
    InvoiceStatus.DRAFT: "Borrador",
    InvoiceStatus.SUBMITTING: "Enviando",
    InvoiceStatus.AUTHORIZED: "Autorizada",
    InvoiceStatus.REJECTED: "Rechazada",
    InvoiceStatus.UNKNOWN: "A reconciliar",
}
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["AUTHORIZED"] = InvoiceStatus.AUTHORIZED
templates.env.globals["RETRYABLE"] = (InvoiceStatus.DRAFT, InvoiceStatus.UNKNOWN)


def _fecha_iso_a_arca(fecha: str | None) -> str | None:
    """'2026-07-05' del <input type=date> → '20260705' (AAAAMMDD)."""
    return fecha.replace("-", "") if fecha else None


def _contexto_listado(service) -> dict:
    return {"facturas": repo.list_invoices(service.conn, limit=50, offset=0)}


def _render_resultado(
    request: Request,
    service,
    *,
    factura=None,
    error: str | None = None,
    force_invoice_id: str | None = None,
):
    """Partial de resultado + refresh del listado vía evento HTMX."""
    response = templates.TemplateResponse(
        request,
        "_resultado.html",
        {
            "factura": factura,
            "error": error,
            "force_invoice_id": force_invoice_id,
        },
    )
    response.headers["HX-Trigger"] = "facturas-changed"
    return response


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, service: ServiceDep):
    clientes = repo.list_clients(service.conn)
    default = next((c for c in clientes if c["is_default"]), None)

    ctz = ctz_fecha = None
    if clientes:
        try:
            # Cotización ARCA del día, informativa (§0: nunca se carga a mano).
            ctz_dec, ctz_fecha = service.wsfex.get_ctz(MONEDA_DOL)
            ctz = format(ctz_dec, "f")
        except (WsfexError, httpx.HTTPError):
            pass  # sin cotización el form sigue usable; authorize revalida

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "env": service.config.env,
            "clientes": clientes,
            "default": default,
            "ctz": ctz,
            "ctz_fecha": ctz_fecha,
            "hoy": dt.date.today().isoformat(),
            **_contexto_listado(service),
        },
    )


def _pagina_clientes(
    request: Request,
    service,
    editando=None,
    error: str | None = None,
    status_code: int = 200,
):
    params: dict[str, list] = {}
    try:
        for kind in ("pais", "cuit_pais", "moneda", "idioma"):
            params[kind] = service.get_params(kind)
    except ServiceError as exc:
        error = error or str(exc)
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "env": service.config.env,
            "clientes": repo.list_clients(service.conn),
            "editando": editando,
            "params": params,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/clientes", response_class=HTMLResponse)
def clientes(request: Request, service: ServiceDep, edit: str | None = None):
    editando = repo.get_client(service.conn, edit) if edit else None
    return _pagina_clientes(request, service, editando=editando)


# ---------------------------------------------------------------------------
# Acciones HTMX
# ---------------------------------------------------------------------------


@router.post("/ui/facturas", response_class=HTMLResponse)
def crear_y_autorizar(
    request: Request,
    service: ServiceDep,
    imp_total: str = Form(...),
    client_id: str = Form(""),
    descripcion: str = Form(""),
    fecha_pago: str = Form(""),
    obs: str = Form(""),
):
    """El click semanal: draft + authorize en un solo POST (§0 paso 3)."""
    try:
        payload = InvoiceCreate(
            imp_total=Decimal(imp_total),
            client_id=client_id or None,
            descripcion=descripcion or None,
            fecha_pago=_fecha_iso_a_arca(fecha_pago),
            obs=obs,
        )
    except InvalidOperation:
        error = f"Datos inválidos: monto {imp_total!r} no es un número"
        return _render_resultado(request, service, error=error)
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return _render_resultado(request, service, error=f"Datos inválidos: {detalles}")

    try:
        draft = service.create_invoice(payload)
    except ServiceError as exc:
        return _render_resultado(request, service, error=str(exc))

    return _autorizar(request, service, draft["id"], force=False)


@router.post("/ui/facturas/{invoice_id}/authorize", response_class=HTMLResponse)
def autorizar(
    request: Request,
    service: ServiceDep,
    invoice_id: str,
    force: bool = False,
):
    return _autorizar(request, service, invoice_id, force)


def _autorizar(request: Request, service, invoice_id: str, force: bool):
    try:
        factura = service.authorize(invoice_id, force_desync=force)
    except ConflictError as exc:
        # El chequeo de DB desactualizada (§2.5) es forzable a conciencia;
        # el draft ya quedó creado, el botón reintenta solo el authorize.
        force_id = invoice_id if "desactualizado" in str(exc) else None
        return _render_resultado(
            request, service, error=str(exc), force_invoice_id=force_id
        )
    except ServiceError as exc:
        return _render_resultado(request, service, error=str(exc))
    return _render_resultado(request, service, factura=factura)


@router.get("/ui/listado", response_class=HTMLResponse)
def listado(request: Request, service: ServiceDep):
    return templates.TemplateResponse(
        request, "_listado.html", _contexto_listado(service)
    )


@router.post("/ui/clientes", response_class=HTMLResponse)
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
            is_default=is_default,
        )
        if client_id:
            service.update_client(client_id, payload)
        else:
            service.create_client(payload)
    except (ValidationError, ServiceError) as exc:
        return _pagina_clientes(request, service, error=str(exc), status_code=422)
    return RedirectResponse("/clientes", status_code=303)

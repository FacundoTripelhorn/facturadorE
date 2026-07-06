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
from ..constants import MONEDA_DISPLAY, MONEDA_DOL, InvoiceStatus
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
templates.env.globals["DRAFT"] = InvoiceStatus.DRAFT
templates.env.globals["UNKNOWN"] = InvoiceStatus.UNKNOWN
templates.env.globals["RETRYABLE"] = (InvoiceStatus.DRAFT, InvoiceStatus.UNKNOWN)
# Presentación de moneda: DOL → USD etc.; hacia ARCA siempre viaja el código.
templates.env.filters["moneda"] = lambda code: MONEDA_DISPLAY.get(code, code)
templates.env.filters["fecha"] = (
    lambda aaaammdd: f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"
)


def _fecha_iso_a_arca(fecha: str | None) -> str | None:
    """'2026-07-05' del <input type=date> → '20260705' (AAAAMMDD)."""
    return fecha.replace("-", "") if fecha else None


# Tabs de /comprobantes: cada una agrupa los estados que le corresponden.
TAB_FILTERS: dict[str, tuple[InvoiceStatus, ...] | None] = {
    "todas": None,
    "borradores": (InvoiceStatus.DRAFT,),
    "autorizadas": (InvoiceStatus.AUTHORIZED,),
    "atencion": (
        InvoiceStatus.SUBMITTING,
        InvoiceStatus.UNKNOWN,
        InvoiceStatus.REJECTED,
    ),
}

TAB_LABELS = {
    "todas": "Todas",
    "borradores": "Borradores",
    "autorizadas": "Autorizadas",
    "atencion": "Atención",
}


def _contexto_listado(service, estado: str = "todas") -> dict:
    statuses = TAB_FILTERS.get(estado)
    return {
        "estado": estado,
        "facturas": repo.list_invoices(
            service.conn, limit=50, offset=0, statuses=statuses
        ),
    }


def _render_resultado(
    request: Request,
    service,
    *,
    factura=None,
    error: str | None = None,
    aviso: str | None = None,
    force_invoice_id: str | None = None,
):
    """Partial de resultado + refresh del listado vía evento HTMX."""
    response = templates.TemplateResponse(
        request,
        "_resultado.html",
        {
            "factura": factura,
            "error": error,
            "aviso": aviso,
            "force_invoice_id": force_invoice_id,
        },
    )
    response.headers["HX-Trigger"] = "facturas-changed"
    return response


def _render_confirmacion(request: Request, service, draft):
    """Paso de revisión (dos fases): borrador creado, NADA viajó a ARCA
    todavía; se muestra exactamente lo que se va a enviar."""
    response = templates.TemplateResponse(
        request,
        "_confirmacion.html",
        {
            "f": draft,
            "items": repo.get_invoice_items(service.conn, draft["id"]),
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
        },
    )


@router.get("/comprobantes", response_class=HTMLResponse)
def comprobantes(request: Request, service: ServiceDep, estado: str = "todas"):
    if estado not in TAB_FILTERS:
        estado = "todas"
    counts = repo.count_invoices_by_status(service.conn)
    tab_counts = {
        tab: (
            sum(counts.values())
            if statuses is None
            else sum(counts.get(s, 0) for s in statuses)
        )
        for tab, statuses in TAB_FILTERS.items()
    }
    return templates.TemplateResponse(
        request,
        "comprobantes.html",
        {
            "env": service.config.env,
            "tabs": TAB_LABELS,
            "tab_counts": tab_counts,
            **_contexto_listado(service, estado),
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
def generar_borrador(
    request: Request,
    service: ServiceDep,
    imp_total: str = Form(...),
    client_id: str = Form(""),
    descripcion: str = Form(""),
    fecha_pago: str = Form(""),
    obs: str = Form(""),
):
    """Fase 1 del flujo en dos pasos: genera el borrador y lo muestra para
    revisión. Nada viaja a ARCA hasta el Confirmar explícito — un error acá
    se descarta sin consecuencia impositiva."""
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

    return _render_confirmacion(request, service, draft)


@router.get("/ui/facturas/{invoice_id}/confirmar", response_class=HTMLResponse)
def confirmar(request: Request, service: ServiceDep, invoice_id: str):
    """Reabre la revisión de un borrador existente desde el listado."""
    try:
        inv = service.get_invoice(invoice_id, reconcile=False)
    except ServiceError as exc:
        return _render_resultado(request, service, error=str(exc))
    if inv["status"] != InvoiceStatus.DRAFT:
        return _render_resultado(
            request, service,
            error=f"Solo los borradores se revisan (estado: {inv['status']})",
        )
    return _render_confirmacion(request, service, inv)


@router.post("/ui/facturas/{invoice_id}/descartar", response_class=HTMLResponse)
def descartar(request: Request, service: ServiceDep, invoice_id: str):
    try:
        service.delete_draft(invoice_id)
    except ServiceError as exc:
        return _render_resultado(request, service, error=str(exc))
    return _render_resultado(
        request, service, aviso="Borrador descartado; no se envió nada a ARCA."
    )


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
def listado(request: Request, service: ServiceDep, estado: str = "todas"):
    return templates.TemplateResponse(
        request, "_listado.html", _contexto_listado(service, estado)
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

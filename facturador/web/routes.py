"""Rutas HTML del frontend. Lógica cero: cada handler arma el contexto con
el InvoiceService/repo y renderiza; las reglas de dominio viven en service.

Flujo de emisión en páginas separadas (patrón Post/Redirect/Get):

    GET  /                        form de nueva factura (solo acá hay inputs)
    POST /ui/facturas             crea el borrador → 303 a la revisión
    GET  /facturas/{id}/revisar   página de revisión: QUÉ se va a enviar
    POST /ui/facturas/{id}/authorize  confirma → 303 al detalle
    GET  /facturas/{id}           detalle read-only: qué se envió, estado,
                                  CAE y PDF; sin ningún input
    POST /ui/facturas/{id}/descartar  borra el borrador → 303 al form

Nada viaja a ARCA sin pasar por la revisión; el detalle es el registro.
"""

from __future__ import annotations

import datetime as dt
import json
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
from ..schemas import (
    BackupSettingsIn,
    ClientIn,
    EmisorCreateIn,
    EmisorUpdateIn,
    InvoiceCreate,
)
from ..service import NotFoundError, ServiceError, StaleRegistryError
from ..settings import (
    BACKUP_PREFIX_DEFAULT,
    CONDICION_IVA_DEFAULT,
    get_active_emisor_id,
)

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
# Presentación de moneda: DOL → USD etc.; hacia ARCA siempre viaja el código.
templates.env.filters["moneda"] = lambda code: MONEDA_DISPLAY.get(code, code)
templates.env.filters["fecha"] = (
    lambda aaaammdd: f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"
)


def _pvs_display(raw: str) -> str:
    try:
        valores = json.loads(raw)
        if isinstance(valores, list):
            return ", ".join(str(v) for v in valores)
    except (ValueError, TypeError):
        pass
    return raw


templates.env.filters["pvs_display"] = _pvs_display


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

AVISOS = {
    "descartado": "Borrador descartado; no se envió nada a ARCA.",
}


# ---------------------------------------------------------------------------
# Emitir (la única página con inputs de factura)
# ---------------------------------------------------------------------------


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
        "env": service.config.env,
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
                status_code=422,
            )
    try:
        payload = InvoiceCreate(
            imp_total=Decimal(imp_total),
            punto_venta=pv,
            client_id=client_id or None,
            descripcion=descripcion or None,
            fecha_pago=_fecha_iso_a_arca(fecha_pago),
            obs=obs,
        )
        draft = service.create_invoice(payload)
    except InvalidOperation:
        error = f"Datos inválidos: monto {imp_total!r} no es un número"
        return templates.TemplateResponse(
            request, "home.html", _contexto_form(service, error=error),
            status_code=422,
        )
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return templates.TemplateResponse(
            request, "home.html",
            _contexto_form(service, error=f"Datos inválidos: {detalles}"),
            status_code=422,
        )
    except ServiceError as exc:
        return templates.TemplateResponse(
            request, "home.html", _contexto_form(service, error=str(exc)),
            status_code=422,
        )
    return RedirectResponse(f"/facturas/{draft['id']}/revisar", status_code=303)


# ---------------------------------------------------------------------------
# Revisión y detalle (read-only: acá no hay inputs)
# ---------------------------------------------------------------------------


def _factura_o_redirect(service, invoice_id: str, reconcile: bool = False):
    try:
        return service.get_invoice(invoice_id, reconcile=reconcile)
    except NotFoundError:
        return None


@router.get("/facturas/{invoice_id}/revisar", response_class=HTMLResponse)
def revisar(request: Request, service: ServiceDep, invoice_id: str):
    """Qué se va a enviar a ARCA. Solo para borradores: cualquier otro
    estado ya tiene historia y se ve en el detalle."""
    inv = _factura_o_redirect(service, invoice_id)
    if inv is None:
        return RedirectResponse("/comprobantes", status_code=303)
    if inv["status"] != InvoiceStatus.DRAFT:
        return RedirectResponse(f"/facturas/{invoice_id}", status_code=303)
    return _pagina_revisar(request, service, inv)


def _pagina_revisar(
    request: Request, service, inv, error: str | None = None, force: bool = False
):
    return templates.TemplateResponse(
        request,
        "revisar.html",
        {
            "env": service.config.env,
            "f": inv,
            "items": repo.get_invoice_items(service.conn, inv["id"]),
            "error": error,
            "ofrecer_force": force,
        },
        status_code=409 if error else 200,
    )


@router.get("/facturas/{invoice_id}", response_class=HTMLResponse)
def detalle(request: Request, service: ServiceDep, invoice_id: str):
    """Registro read-only del comprobante: qué se envió, estado, CAE, PDF.
    Reconcilia 'unknown' al mirarlo (lazy, igual que la API JSON)."""
    inv = _factura_o_redirect(service, invoice_id, reconcile=True)
    if inv is None:
        return RedirectResponse("/comprobantes", status_code=303)
    if inv["status"] == InvoiceStatus.DRAFT:
        return RedirectResponse(f"/facturas/{invoice_id}/revisar", status_code=303)
    return templates.TemplateResponse(
        request,
        "detalle.html",
        {
            "env": service.config.env,
            "f": inv,
            "items": repo.get_invoice_items(service.conn, inv["id"]),
        },
    )


@router.post("/ui/facturas/{invoice_id}/authorize")
def autorizar(
    request: Request,
    service: ServiceDep,
    invoice_id: str,
    force: bool = False,
):
    try:
        service.authorize(invoice_id, force_desync=force)
    except NotFoundError:
        return RedirectResponse("/comprobantes", status_code=303)
    except ServiceError as exc:
        inv = _factura_o_redirect(service, invoice_id)
        if inv is not None and inv["status"] == InvoiceStatus.DRAFT:
            # Solo el desajuste de registro (§2.5) es forzable a conciencia,
            # señalado por su tipo propio, no por el texto del mensaje;
            # cualquier otro conflicto se muestra sin botón de force.
            return _pagina_revisar(
                request, service, inv,
                error=str(exc),
                force=isinstance(exc, StaleRegistryError),
            )
        return RedirectResponse(f"/facturas/{invoice_id}", status_code=303)
    return RedirectResponse(f"/facturas/{invoice_id}", status_code=303)


@router.post("/ui/facturas/{invoice_id}/descartar")
def descartar(request: Request, service: ServiceDep, invoice_id: str):
    try:
        service.delete_draft(invoice_id)
    except NotFoundError:
        return RedirectResponse("/comprobantes", status_code=303)
    except ServiceError:
        # Enviada/no descartable: el detalle explica el estado real.
        return RedirectResponse(f"/facturas/{invoice_id}", status_code=303)
    return RedirectResponse("/?aviso=descartado", status_code=303)


# ---------------------------------------------------------------------------
# Registro de comprobantes
# ---------------------------------------------------------------------------


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
            "estado": estado,
            "facturas": repo.list_invoices(
                service.conn, limit=50, offset=0, statuses=TAB_FILTERS[estado]
            ),
        },
    )


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Configuración (settings de dominio: viven en la DB, no en el .env)
# ---------------------------------------------------------------------------


def _pagina_configuracion(
    request: Request,
    service,
    editando=None,
    error: str | None = None,
    aviso: str | None = None,
    status_code: int = 200,
):
    activo_id = get_active_emisor_id(service.conn, service.config.env)
    return templates.TemplateResponse(
        request,
        "configuracion.html",
        {
            "env": service.config.env,
            "s": service.get_settings(),
            "emisores": service.list_emisores(),
            "activo_id": activo_id,
            "editando": editando,
            "error": error,
            "aviso": aviso,
        },
        status_code=status_code,
    )


@router.get("/configuracion", response_class=HTMLResponse)
def configuracion(request: Request, service: ServiceDep, edit: str | None = None,
                  aviso: str = ""):
    editando = repo.get_emisor(service.conn, edit) if edit else None
    avisos = {
        "guardado": "Emisor guardado.",
        "backup": "Configuración de backups guardada.",
        "activado": "Emisor activo actualizado.",
    }
    return _pagina_configuracion(
        request,
        service,
        editando=editando,
        aviso=avisos.get(aviso),
    )


def _parse_puntos_venta_form(puntos_venta: str) -> list[int]:
    return [int(v) for v in puntos_venta.split(",") if v.strip()]


@router.post("/ui/emisores", response_class=HTMLResponse)
def guardar_emisor(
    request: Request,
    service: ServiceDep,
    emisor_id: str = Form(""),
    razon_social: str = Form(""),
    domicilio: str = Form(""),
    iibb: str = Form(""),
    inicio_actividades: str = Form(""),
    condicion_iva: str = Form(CONDICION_IVA_DEFAULT),
    ambiente: str = Form("homo"),
    puntos_venta: str = Form("1"),
):
    existente = repo.get_emisor(service.conn, emisor_id) if emisor_id else None
    try:
        pvs = _parse_puntos_venta_form(puntos_venta)
    except ValueError:
        return _pagina_configuracion(
            request,
            service,
            editando=existente,
            error="Datos inválidos: los puntos de venta deben ser números"
            " separados por coma",
            status_code=422,
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
                ambiente=ambiente,
                puntos_venta=pvs,
            )
    except ValidationError as exc:
        detalles = "; ".join(e["msg"] for e in exc.errors())
        return _pagina_configuracion(
            request,
            service,
            editando=existente,
            error=f"Datos inválidos: {detalles}",
            status_code=422,
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
            status_code=422,
        )
    return RedirectResponse("/configuracion?aviso=guardado", status_code=303)


@router.post("/ui/emisores/{emisor_id}/activar")
def activar_emisor(request: Request, service: ServiceDep, emisor_id: str):
    try:
        service.activate_emisor(emisor_id)
    except ServiceError:
        return RedirectResponse("/configuracion", status_code=303)
    return RedirectResponse("/configuracion?aviso=activado", status_code=303)


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
            request, service, error=f"Datos inválidos: {detalles}", status_code=422
        )
    service.update_backup_settings(payload)
    return RedirectResponse("/configuracion?aviso=backup", status_code=303)

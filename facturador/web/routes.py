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
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from .. import repo
from ..api.csrf import (
    CSRF_JSON_DETAIL,
    CSRF_UI_MESSAGE,
    CsrfRejected,
    csrf_token_for_request,
    enforce_csrf,
)
from ..api.deps import ServiceDep
from ..arca.wsfex import WsfexError
from ..certs import (
    CertificateError,
    CertificateMetadata,
    load_certificate_metadata,
    store_certificate_pair,
)
from ..constants import MONEDA_DISPLAY, MONEDA_DOL, InvoiceStatus
from ..fiscal_identity import FiscalIdentityError, get_sealed_fiscal_cuit
from ..launcher.switch import (
    is_launcher_supervised,
    write_change_environment_request,
)
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
from ..setup import SetupState, reconcile_setup_state

router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(enforce_csrf)],
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _ambiente_en_contexto(request: Request) -> dict[str, object]:
    """Identidad de ambiente visible en toda la UI (ADR 0001 / FAC-31).

    Solo lenguaje de negocio (Homologación / Producción) y el código corto;
    nunca paths de perfil, certificados ni secretos. FAC-32: ``launcher``
    indica si el proceso está supervisado y puede pedir cambio por reinicio.
    """
    profile = request.app.state.profile
    return {
        "env": profile.environment,
        "env_label": profile.display_name,
        "launcher_supervised": is_launcher_supervised(),
    }


def _csrf_en_contexto(request: Request) -> dict[str, object]:
    """Token CSRF para campos ocultos de formularios (FAC-42)."""
    return {"csrf_token": csrf_token_for_request(request)}


templates = Jinja2Templates(
    directory=TEMPLATES_DIR,
    context_processors=[_ambiente_en_contexto, _csrf_en_contexto],
)


def csrf_recovery_href(request: Request) -> str:
    """GET que vuelve a pintar el formulario con un token CSRF fresco.

    ``location.reload()`` sobre el 403 del POST reenviaría el mismo body
    con el token viejo. Preferimos el ``Referer`` same-origin (si no es
    ``/ui/``) y, si falta, un mapeo estático del path del POST.
    """
    referer = request.headers.get("referer")
    if referer:
        parsed = urlparse(referer)
        if (
            parsed.scheme in ("", "http")
            and parsed.path
            and not parsed.path.startswith("/ui/")
            and parsed.netloc in ("", request.url.netloc)
        ):
            href = parsed.path
            if parsed.query:
                href = f"{href}?{parsed.query}"
            return href
    return _recovery_from_ui_path(request.url.path)


def _recovery_from_ui_path(path: str) -> str:
    if path == "/ui/facturas":
        return "/"
    if path == "/ui/clientes":
        return "/clientes"
    if path.startswith("/ui/setup/"):
        return "/setup"
    if path.startswith("/ui/emisores") or path.startswith("/ui/configuracion/"):
        return "/configuracion"
    if path == "/ui/cambiar-ambiente":
        return "/"
    if path == "/ui/registry/catch-up":
        return "/comprobantes"
    m = re.fullmatch(r"/ui/facturas/([^/]+)/(?:authorize|descartar)", path)
    if m:
        return f"/facturas/{m.group(1)}/revisar"
    return "/"


async def csrf_rejected_handler(
    request: Request, exc: Exception
) -> HTMLResponse | JSONResponse:
    """FAC-62: HTML con guía de recuperación en ``/ui/``; JSON genérico fuera.

    El mensaje es fijo: nunca ecoa cookie ni el valor enviado en el form.
    La firma acepta ``Exception`` por el contrato de Starlette
    ``ExceptionHandler``; solo se registra para :class:`CsrfRejected`.
    """
    if not isinstance(exc, CsrfRejected):
        raise exc
    if request.url.path.startswith("/ui/"):
        return templates.TemplateResponse(
            request,
            "csrf_error.html",
            {
                "message": CSRF_UI_MESSAGE,
                "recovery_href": csrf_recovery_href(request),
            },
            status_code=403,
        )
    return JSONResponse(
        status_code=403, content={"detail": CSRF_JSON_DETAIL}
    )


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
def revisar(
    request: Request,
    service: ServiceDep,
    invoice_id: str,
    aviso: str = "",
    n: str = "",
):
    """Qué se va a enviar a ARCA. Solo para borradores: cualquier otro
    estado ya tiene historia y se ve en el detalle."""
    inv = _factura_o_redirect(service, invoice_id)
    if inv is None:
        return RedirectResponse("/comprobantes", status_code=303)
    if inv["status"] != InvoiceStatus.DRAFT:
        return RedirectResponse(f"/facturas/{invoice_id}", status_code=303)
    return _pagina_revisar(
        request,
        service,
        inv,
        aviso=_catchup_aviso(aviso, n),
    )


def _catchup_aviso(aviso: str, n: str) -> str | None:
    if aviso != "catchup":
        return None
    if n:
        return f"Registro sincronizado desde ARCA: {n} comprobantes."
    return "Registro sincronizado desde ARCA."


def _pagina_revisar(
    request: Request,
    service,
    inv,
    error: str | None = None,
    force: bool = False,
    aviso: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "revisar.html",
        {
            "f": inv,
            "items": repo.get_invoice_items(service.conn, inv["id"]),
            "error": error,
            "aviso": aviso,
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


@router.post("/ui/registry/catch-up")
def catch_up_ui(
    request: Request,
    service: ServiceDep,
    invoice_id: str = Form(""),
):
    """FAC-65: remediación del guard FAC-48 — catch-up desde ARCA."""
    try:
        report = service.catch_up_from_arca()
    except ServiceError as exc:
        if invoice_id:
            inv = _factura_o_redirect(service, invoice_id)
            if inv is not None and inv["status"] == InvoiceStatus.DRAFT:
                return _pagina_revisar(
                    request, service, inv,
                    error=str(exc),
                    force=True,
                )
        # Sin invoice_id: PRG al listado (error en query; GET /comprobantes lo muestra).
        from urllib.parse import quote

        return RedirectResponse(
            f"/comprobantes?error={quote(str(exc), safe='')}",
            status_code=303,
        )
    if invoice_id:
        return RedirectResponse(
            f"/facturas/{invoice_id}/revisar?aviso=catchup&n={report.inserted}",
            status_code=303,
        )
    return RedirectResponse(
        f"/comprobantes?aviso=catchup&n={report.inserted}", status_code=303
    )


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
def comprobantes(
    request: Request,
    service: ServiceDep,
    estado: str = "todas",
    aviso: str = "",
    n: str = "",
    error: str = "",
):
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
            "tabs": TAB_LABELS,
            "tab_counts": tab_counts,
            "estado": estado,
            "aviso": _catchup_aviso(aviso, n),
            "error": error or None,
            "facturas": repo.list_invoices(
                service.conn, limit=50, offset=0, statuses=TAB_FILTERS[estado]
            ),
        },
    )


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------


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
    status_code: int = 200,
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
        # inválido si el refresh pasa por validación FAC-36.
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
    except (ValidationError, ServiceError, CertificateError, OSError) as exc:
        # Mismo caso que GET /clientes: sin certs/params cache no debe 500.
        return _pagina_clientes(request, service, error=str(exc), status_code=422)
    return RedirectResponse("/clientes", status_code=303)


# ---------------------------------------------------------------------------
# Setup / onboarding (FAC-37)
# ---------------------------------------------------------------------------


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
    aviso: str | None = None,
    status_code: int = 200,
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
    """Onboarding por perfil: certificado, emisor y punto de venta (FAC-37/38)."""
    avisos = {
        "instalado": "instalado",
        "emisor": "emisor",
        "punto_venta": "punto_venta",
        "listo": "listo",
    }
    return _pagina_setup(request, service, aviso=avisos.get(aviso, aviso or None))


@router.post("/ui/setup/certificado", response_class=HTMLResponse)
async def instalar_certificado_setup(
    request: Request,
    service: ServiceDep,
    certificado: UploadFile = File(...),  # noqa: B008
    clave: UploadFile = File(...),  # noqa: B008
):
    """Valida y persiste el par cert/key del perfil (FAC-36 vía UI)."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)

    if certificado.filename is None or clave.filename is None:
        return _pagina_setup(
            request,
            service,
            state=state,
            error="Hay que subir el certificado y la clave privada juntos.",
            status_code=422,
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
            status_code=422,
        )

    if not cert_bytes.strip() or not key_bytes.strip():
        return _pagina_setup(
            request,
            service,
            state=state,
            error="El certificado y la clave privada no pueden estar vacíos.",
            status_code=422,
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
            status_code=422,
        )

    reconcile_setup_state(profile, service.conn)
    return RedirectResponse("/setup?aviso=instalado", status_code=303)


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
    """Alta/edición del primer emisor durante onboarding (FAC-38)."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)
    if state is not SetupState.EMISOR_REQUIRED:
        return RedirectResponse("/setup", status_code=303)

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
                status_code=422,
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
                status_code=422,
            )
        return _pagina_setup(
            request,
            service,
            state=state,
            error=(
                "Datos inválidos: el emisor no coincide con "
                "el paso de setup en curso"
            ),
            status_code=422,
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
            status_code=422,
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
            status_code=422,
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
            status_code=422,
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
            status_code=422,
        )

    new_state = reconcile_setup_state(profile, service.conn)
    if new_state is SetupState.READY and state is not SetupState.READY:
        # FAC-47: fin de onboarding (/setup is setup-guard exempt).
        service.notify_seed_backup("onboarding_completed")
    aviso = "listo" if new_state is SetupState.READY else "emisor"
    return RedirectResponse(f"/setup?aviso={aviso}", status_code=303)


@router.post("/ui/setup/punto-venta", response_class=HTMLResponse)
async def guardar_punto_venta_setup(
    request: Request,
    service: ServiceDep,
    puntos_venta: str = Form(""),
):
    """Habilita puntos de venta del emisor activo durante onboarding (FAC-38)."""
    profile = request.app.state.profile
    state = reconcile_setup_state(profile, service.conn)
    if state is not SetupState.POINT_OF_SALE_REQUIRED:
        return RedirectResponse("/setup", status_code=303)

    emisor_row = _emisor_onboarding_row(service.conn, service.config.env)
    if emisor_row is None:
        return _pagina_setup(
            request,
            service,
            state=state,
            error="No hay emisor activo para configurar el punto de venta.",
            status_code=422,
        )
    if emisor_row["ambiente"] != service.config.env:
        return _pagina_setup(
            request,
            service,
            state=state,
            error=_error_emisor_otro_ambiente(emisor_row, service.config.env),
            status_code=422,
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
            status_code=422,
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
            status_code=422,
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
            status_code=422,
        )

    new_state = reconcile_setup_state(profile, service.conn)
    if new_state is SetupState.READY and state is not SetupState.READY:
        # FAC-47: fin de onboarding (/setup is setup-guard exempt).
        service.notify_seed_backup("onboarding_completed")
    aviso = "listo" if new_state is SetupState.READY else "punto_venta"
    return RedirectResponse(f"/setup?aviso={aviso}", status_code=303)


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
            status_code=422,
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


# ---------------------------------------------------------------------------
# Cambio de ambiente por reinicio (FAC-32 / ADR 0001)
# ---------------------------------------------------------------------------


@router.post("/ui/cambiar-ambiente", response_class=HTMLResponse)
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
            status_code=500,
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
            status_code=422,
        )
    write_change_environment_request(profile.paths, profile.environment)
    # El ambiente del proceso NO cambia: solo el pedido en disco.
    assert service.config.env == profile.environment
    return templates.TemplateResponse(
        request,
        "cambiar_ambiente.html",
        {"pending": True, "error": None},
    )

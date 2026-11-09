"""Revisión y detalle de una factura (read-only: acá no hay inputs)."""

from __future__ import annotations

import json
import sqlite3
from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import repo
from ...api.deps import ServiceDep
from ...constants import (
    InvoiceStatus,
)
from ...service import (
    NotFoundError,
    ServiceError,
    StaleRegistryError,
)
from ._comun import (
    _catchup_aviso,
    templates,
)

router = APIRouter()


def _factura_o_redirect(service, invoice_id: str, reconcile: bool = False):
    try:
        return service.get_invoice(invoice_id, reconcile=reconcile)
    except NotFoundError:
        return None


def _ir_a_factura(inv: sqlite3.Row | None, sufijo: str = "") -> RedirectResponse:
    """Redirect al detalle (o ``/revisar``) de una factura que existe.

    El id sale de la fila de la base, no del path: el destino es siempre
    una factura real de este registro, y si no hay fila se va al listado.
    """
    if inv is None:
        return RedirectResponse("/comprobantes", status_code=HTTPStatus.SEE_OTHER)
    return RedirectResponse(
        f"/facturas/{inv['id']}{sufijo}", status_code=HTTPStatus.SEE_OTHER
    )


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
        return RedirectResponse("/comprobantes", status_code=HTTPStatus.SEE_OTHER)
    if inv["status"] != InvoiceStatus.DRAFT:
        return _ir_a_factura(inv)
    return _pagina_revisar(
        request,
        service,
        inv,
        aviso=_catchup_aviso(aviso, n),
    )


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
        status_code=HTTPStatus.CONFLICT if error else 200,
    )


@router.get("/facturas/{invoice_id}", response_class=HTMLResponse)
def detalle(request: Request, service: ServiceDep, invoice_id: str):
    """Registro read-only del comprobante: qué se envió, estado, CAE, PDF.
    Reconcilia 'unknown' al mirarlo (lazy, igual que la API JSON)."""
    inv = _factura_o_redirect(service, invoice_id, reconcile=True)
    if inv is None:
        return RedirectResponse("/comprobantes", status_code=HTTPStatus.SEE_OTHER)
    if inv["status"] == InvoiceStatus.DRAFT:
        return _ir_a_factura(inv, "/revisar")
    return templates.TemplateResponse(
        request,
        "detalle.html",
        {
            "f": inv,
            "items": repo.get_invoice_items(service.conn, inv["id"]),
            "motivos_obs": _motivos_obs(inv),
        },
    )


def _motivos_obs(inv: sqlite3.Row) -> str | None:
    """Observaciones que ARCA devolvió al autorizar (CAE válido igual)."""
    if inv["status"] != InvoiceStatus.AUTHORIZED or not inv["raw_response"]:
        return None
    try:
        raw = json.loads(inv["raw_response"])
    except (TypeError, ValueError):
        return None
    obs = raw.get("motivos_obs") if isinstance(raw, dict) else None
    texto = str(obs).strip() if obs else ""
    return texto or None


@router.post("/ui/facturas/{invoice_id}/authorize")
def autorizar(
    request: Request,
    service: ServiceDep,
    invoice_id: str,
    force: bool = False,
):
    try:
        autorizada = service.authorize(invoice_id, force_desync=force)
    except NotFoundError:
        return RedirectResponse("/comprobantes", status_code=HTTPStatus.SEE_OTHER)
    except ServiceError as exc:
        inv = _factura_o_redirect(service, invoice_id)
        if inv is not None and inv["status"] == InvoiceStatus.DRAFT:
            # Solo el desajuste de registro (§2.5) es forzable a conciencia,
            # señalado por su tipo propio, no por el texto del mensaje;
            # cualquier otro conflicto se muestra sin botón de force.
            return _pagina_revisar(
                request,
                service,
                inv,
                error=str(exc),
                force=isinstance(exc, StaleRegistryError),
            )
        return _ir_a_factura(inv)
    return _ir_a_factura(autorizada)


@router.post("/ui/registry/catch-up")
def catch_up_ui(
    request: Request,
    service: ServiceDep,
    invoice_id: str = Form(""),
):
    """Remediación del guard de registro desactualizado — catch-up desde ARCA."""
    try:
        report = service.catch_up_from_arca()
    except ServiceError as exc:
        if invoice_id:
            inv = _factura_o_redirect(service, invoice_id)
            if inv is not None and inv["status"] == InvoiceStatus.DRAFT:
                return _pagina_revisar(
                    request,
                    service,
                    inv,
                    error=str(exc),
                    force=True,
                )
        # Sin invoice_id: PRG al listado (error en query; GET /comprobantes lo muestra).
        from urllib.parse import quote

        return RedirectResponse(
            f"/comprobantes?error={quote(str(exc), safe='')}",
            status_code=HTTPStatus.SEE_OTHER,
        )
    inv = _factura_o_redirect(service, invoice_id) if invoice_id else None
    if inv is not None:
        return _ir_a_factura(inv, f"/revisar?aviso=catchup&n={report.inserted}")
    return RedirectResponse(
        f"/comprobantes?aviso=catchup&n={report.inserted}",
        status_code=HTTPStatus.SEE_OTHER,
    )


@router.post("/ui/facturas/{invoice_id}/descartar")
def descartar(request: Request, service: ServiceDep, invoice_id: str):
    try:
        service.delete_draft(invoice_id)
    except NotFoundError:
        return RedirectResponse("/comprobantes", status_code=HTTPStatus.SEE_OTHER)
    except ServiceError:
        # Enviada/no descartable: el detalle explica el estado real.
        return _ir_a_factura(_factura_o_redirect(service, invoice_id))
    return RedirectResponse("/?aviso=descartado", status_code=HTTPStatus.SEE_OTHER)

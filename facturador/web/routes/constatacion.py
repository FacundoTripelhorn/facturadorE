"""Constatación de CAE (WSCDC): menú propio, botón explícito."""

from __future__ import annotations

import datetime as dt
import sqlite3
from http import HTTPStatus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ... import repo
from ...api.deps import ServiceDep
from ...constants import (
    CBTE_MODO_CAE,
    TIPO_DOC_CUIT,
    InvoiceStatus,
)
from ...service import (
    ArcaUnavailableError,
    ConflictError,
    DomainError,
    NotFoundError,
    ServiceError,
)
from ..constants import Pagina
from ..numeros import parse_importe
from ._comun import (
    templates,
)

router = APIRouter()


def _preview_constatacion(inv: sqlite3.Row) -> dict[str, object]:
    """Campos que viajan a WSCDC (misma forma que el portal)."""
    return {
        "cuit_emisor": inv["cuit_emisor"] or "—",
        "cbte_tipo": inv["cbte_tipo"],
        "punto_venta": inv["punto_venta"],
        "cbte_nro": inv["cbte_nro"],
        "fecha_cbte": inv["fecha_cbte"],
        "imp_total": inv["imp_total"],
        "cae": inv["cae"],
        "doc_tipo_receptor": TIPO_DOC_CUIT,
        "doc_nro_receptor": inv["cuit_pais_cliente"],
        "cbte_modo": CBTE_MODO_CAE,
    }


def _pagina_constatacion(
    request: Request,
    service: ServiceDep,
    *,
    invoice_id: str | None = None,
    modo_manual: bool = False,
    manual: dict[str, str] | None = None,
    resultado=None,
    error: str | None = None,
    status_code: int = HTTPStatus.OK,
):
    facturas = repo.list_invoices(
        service.conn,
        limit=100,
        offset=0,
        statuses=(InvoiceStatus.AUTHORIZED,),
    )
    selected = None
    if not modo_manual:
        if invoice_id:
            selected = next((f for f in facturas if f["id"] == invoice_id), None)
        if selected is None and facturas:
            selected = facturas[0]
    # Sin registro local el modo manual es la única vía (comprobante externo).
    modo_manual = modo_manual or not facturas
    return templates.TemplateResponse(
        request,
        "constatacion.html",
        {
            "facturas": facturas,
            "selected": selected if not modo_manual else None,
            "preview": _preview_constatacion(selected)
            if selected and not modo_manual
            else None,
            "modo_manual": modo_manual,
            "manual": manual or {},
            "resultado": resultado,
            "error": error,
        },
        status_code=status_code,
    )


@router.get(Pagina.CONSTATACION, response_class=HTMLResponse)
def constatacion(
    request: Request, service: ServiceDep, invoice_id: str = "", modo: str = ""
):
    return _pagina_constatacion(
        request,
        service,
        invoice_id=invoice_id or None,
        modo_manual=modo == "manual",
    )


@router.post("/ui/constatacion", response_class=HTMLResponse)
def ui_constatar(
    request: Request,
    service: ServiceDep,
    invoice_id: str = Form(""),
    punto_venta: str = Form(""),
    cbte_nro: str = Form(""),
    fecha_cbte: str = Form(""),
    imp_total: str = Form(""),
    cae: str = Form(""),
    doc_nro_receptor: str = Form(""),
):
    """Botón Constatar: única vía que llama a WSCDC (nunca post-authorize).

    Dos modos: con ``invoice_id`` constata un comprobante
    del registro local; sin ``invoice_id``, los campos manuales describen un
    comprobante externo (no emitido por la app).
    """
    if invoice_id:
        try:
            resultado = service.constatar_cae(invoice_id)
        except NotFoundError:
            return _pagina_constatacion(
                request,
                service,
                invoice_id=invoice_id,
                error="La factura no existe.",
                status_code=HTTPStatus.NOT_FOUND,
            )
        except (DomainError, ArcaUnavailableError, ServiceError) as exc:
            return _pagina_constatacion(
                request,
                service,
                invoice_id=invoice_id,
                error=str(exc),
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY
                if isinstance(exc, DomainError)
                else 503,
            )
        return _pagina_constatacion(
            request, service, invoice_id=invoice_id, resultado=resultado
        )
    return _constatar_manual(
        request,
        service,
        punto_venta=punto_venta,
        cbte_nro=cbte_nro,
        fecha_cbte=fecha_cbte,
        imp_total=imp_total,
        cae=cae,
        doc_nro_receptor=doc_nro_receptor,
    )


def _constatar_manual(
    request: Request,
    service: ServiceDep,
    *,
    punto_venta: str,
    cbte_nro: str,
    fecha_cbte: str,
    imp_total: str,
    cae: str,
    doc_nro_receptor: str,
):
    """Constatación de un comprobante externo tipeado a mano (portal-like)."""
    manual = {
        "punto_venta": punto_venta,
        "cbte_nro": cbte_nro,
        "fecha_cbte": fecha_cbte,
        "imp_total": imp_total,
        "cae": cae,
        "doc_nro_receptor": doc_nro_receptor,
    }

    def fail(mensaje: str, status_code: int = HTTPStatus.UNPROCESSABLE_ENTITY):
        return _pagina_constatacion(
            request,
            service,
            modo_manual=True,
            manual=manual,
            error=mensaje,
            status_code=status_code,
        )

    try:
        pv = int(punto_venta)
        nro = int(cbte_nro)
    except ValueError:
        return fail("Punto de venta, número e importe deben ser numéricos.")
    try:
        total = parse_importe(imp_total)
    except ValueError as exc:
        return fail(f"Importe: {exc}")
    fecha_raw = fecha_cbte.strip()
    if "-" in fecha_raw:  # <input type=date> ISO → AAAAMMDD del portal/WS
        try:
            fecha_raw = dt.datetime.strptime(fecha_raw, "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError:
            return fail("Fecha inválida.")
    try:
        resultado = service.constatar_cae_manual(
            punto_venta=pv,
            cbte_nro=nro,
            fecha_cbte=fecha_raw,
            imp_total=total,
            cae=cae.strip(),
            doc_nro_receptor=doc_nro_receptor.strip(),
        )
    except (DomainError, ArcaUnavailableError, ServiceError) as exc:
        if isinstance(exc, ConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(exc, DomainError):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
        else:
            status = HTTPStatus.SERVICE_UNAVAILABLE
        return fail(str(exc), status)
    return _pagina_constatacion(
        request, service, modo_manual=True, manual=manual, resultado=resultado
    )

"""Rutas de facturas: creación de drafts, authorize, consulta y PDF."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query, Response

from .. import repo
from ..constants import InvoiceStatus
from ..pdf import get_or_render_invoice_pdf, invoice_pdf_filename
from ..schemas import InvoiceCreate, InvoiceOut, ItemOut
from ..service import ConflictError
from .deps import ServiceDep

router = APIRouter(prefix="/invoices", tags=["invoices"])


def _invoice_out(conn: sqlite3.Connection, row: sqlite3.Row) -> InvoiceOut:
    items = [ItemOut(**dict(i)) for i in repo.get_invoice_items(conn, row["id"])]
    campos = {k: row[k] for k in row.keys() if k in InvoiceOut.model_fields}
    return InvoiceOut(**campos, items=items)


@router.post("", response_model=InvoiceOut, status_code=201)
def create_invoice(payload: InvoiceCreate, service: ServiceDep):
    return _invoice_out(service.conn, service.create_invoice(payload))


@router.post("/{invoice_id}/authorize", response_model=InvoiceOut)
def authorize(
    invoice_id: str, service: ServiceDep, force_desync: bool = Query(default=False)
):
    return _invoice_out(service.conn, service.authorize(invoice_id, force_desync))


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, service: ServiceDep):
    return _invoice_out(service.conn, service.get_invoice(invoice_id))


@router.get("", response_model=list[InvoiceOut])
def list_invoices(
    service: ServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return [
        _invoice_out(service.conn, r)
        for r in repo.list_invoices(service.conn, limit, offset)
    ]


@router.get("/{invoice_id}/pdf")
def invoice_pdf(invoice_id: str, service: ServiceDep):
    inv = service.get_invoice(invoice_id)  # 404 si no existe; reconcilia unknown
    if inv["status"] != InvoiceStatus.AUTHORIZED:
        raise ConflictError(
            f"El PDF existe solo para facturas autorizadas "
            f"(estado actual: {inv['status']})"
        )
    items = repo.get_invoice_items(service.conn, invoice_id)
    # FAC-52/53: render solo desde snapshot; cache local opcional bajo
    # pdf_dir (descartable; regenera si falta).
    try:
        pdf = get_or_render_invoice_pdf(
            inv, items, service.config.paths.pdf_dir
        )
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    except RuntimeError as exc:
        # WeasyPrint sin Pango/GTK (típico en Windows nativo): 503 accionable.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    filename = invoice_pdf_filename(inv)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )

"""Rutas de facturas: creación de drafts, authorize, consulta y PDF."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Query, Response

from .. import repo
from ..constants import CBTE_TIPO_FACTURA_E, InvoiceStatus
from ..pdf import get_or_render_invoice_pdf, invoice_pdf_filename
from ..schemas import ArcaInvoicesOut, InvoiceCreate, InvoiceOut, ItemOut
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


@router.get("/arca", response_model=ArcaInvoicesOut)
def list_arca_invoices(
    service: ServiceDep,
    punto_venta: int | None = Query(default=None, ge=1),
    cbte_tipo: int = Query(default=CBTE_TIPO_FACTURA_E, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cbte_nro: int | None = Query(
        default=None,
        ge=1,
        description="Si se indica, consulta solo ese número vía FEXGetCMP",
    ),
):
    """Peek de solo lectura del registro ARCA (FAC-68).

    No escribe en la DB local. Usa ``FEXGetLast_CMP`` + ``FEXGetCMP``.
    ``offset`` cuenta desde el comprobante más reciente.
    """
    return service.list_arca_invoices(
        punto_venta=punto_venta,
        cbte_tipo=cbte_tipo,
        limit=limit,
        offset=offset,
        cbte_nro=cbte_nro,
    )


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
    # pdf_dir (descartable; regenera si falta). ValueError cubre snapshot
    # incompleto, versión desconocida y PdfLayoutError (FAC-88: no entra
    # en una página A4).
    try:
        pdf = get_or_render_invoice_pdf(
            inv, items, service.config.paths.pdf_dir
        )
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    filename = invoice_pdf_filename(inv)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )

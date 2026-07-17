"""Rutas de facturas: creación de drafts, authorize, consulta y PDF."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Query, Response

from .. import repo
from ..constants import InvoiceStatus
from ..pdf import invoice_pdf_filename, render_invoice_pdf
from ..schemas import InvoiceCreate, InvoiceOut, ItemOut
from ..service import ConflictError
from ..settings import emisor_from_invoice_snapshot
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


def _param_ds(conn: sqlite3.Connection, kind: str, code: object) -> str:
    """Descripción de un código desde el cache de params (mejor esfuerzo):
    si falta, el PDF imprime solo el código."""
    for row in repo.get_params(conn, kind):
        if row["code"] == str(code):
            return row["description"] or ""
    return ""


@router.get("/{invoice_id}/pdf")
def invoice_pdf(invoice_id: str, service: ServiceDep):
    inv = service.get_invoice(invoice_id)  # 404 si no existe; reconcilia unknown
    if inv["status"] != InvoiceStatus.AUTHORIZED:
        raise ConflictError(
            f"El PDF existe solo para facturas autorizadas "
            f"(estado actual: {inv['status']})"
        )
    if not inv["emisor_id"]:
        raise ConflictError(
            "La factura no tiene emisor asociado; no se puede generar el PDF."
        )
    try:
        emisor = emisor_from_invoice_snapshot(inv)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    if not emisor.completo:
        raise ConflictError(
            "La factura no tiene snapshot completo del emisor; "
            "no se puede generar el PDF."
        )
    # Preferir el snapshot de la factura (FAC-39); filas viejas sin columna
    # caen al CUIT del certificado vivo del perfil.
    raw_cuit = inv["cuit_emisor"] if "cuit_emisor" in inv.keys() else None
    cuit_emisor = int(raw_cuit) if raw_cuit else service.wsfex.cuit
    pdf = render_invoice_pdf(
        inv,
        repo.get_invoice_items(service.conn, invoice_id),
        emisor,
        cuit_emisor,
        pais_ds=_param_ds(service.conn, "pais", inv["dst_cmp"]),
        cuit_pais_ds=_param_ds(
            service.conn, "cuit_pais", inv["cuit_pais_cliente"]
        ),
        moneda_ds=_param_ds(service.conn, "moneda", inv["moneda_id"]),
    )
    filename = invoice_pdf_filename(inv)
    # Copia persistida en el pdfs/ del perfil (FAC-25); la respuesta no
    # depende del archivo, se sirve siempre el render fresco.
    service.config.paths.pdf_dir.mkdir(parents=True, exist_ok=True)
    (service.config.paths.pdf_dir / filename).write_bytes(pdf)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )

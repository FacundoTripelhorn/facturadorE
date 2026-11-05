"""Pantalla "Orden de pago": une las Facturas E y los invoices de la cuenta
recaudadora que pide el banco para ingresar un cobro.

    GET  /orden-de-pago?ids=…                 facturas elegidas, suma y carga
    GET  /orden-de-pago/facturas-e?ids=…      PDF con las Facturas E unidas
    POST /ui/orden-de-pago/recaudadora        PDF con los invoices subidos

Las dos descargas son independientes. Los errores de las descargas salen
como JSON ``{"detail": …}`` (handlers de ``api/app.py``) para que el script
de ``_descarga_pdf.html`` los muestre arriba de la página. Los invoices
subidos se leen en memoria y se descartan: no se guardan en la base ni en
disco.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from .. import repo
from ..api.csrf import enforce_csrf
from ..api.deps import ServiceDep
from ..orden_pago import MAX_PDF_BYTES, PdfSubido
from ..service import ConflictError, DomainError, NotFoundError, ServiceError
from .routes import templates

router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(enforce_csrf)],
)

_STATUS_ERROR: dict[type[ServiceError], int] = {
    NotFoundError: 404,
    ConflictError: 409,
    DomainError: 422,
}


def _totales_por_moneda(facturas: list[sqlite3.Row]) -> list[tuple[str, Decimal]]:
    """Suma de las facturas por moneda (normalmente una sola: dólares)."""
    totales: dict[str, Decimal] = defaultdict(Decimal)
    for f in facturas:
        totales[f["moneda_id"]] += Decimal(f["imp_total"])
    return list(totales.items())


def _cuenta_del_cliente(conn: sqlite3.Connection, factura: sqlite3.Row) -> str:
    if not factura["client_id"]:
        return ""
    cliente = repo.get_client(conn, factura["client_id"])
    if cliente is None:
        return ""
    return cliente["cuenta_recaudadora"] or ""


def _pdf(nombre: str, contenido: bytes) -> Response:
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.get("/orden-de-pago", response_class=HTMLResponse)
def orden_de_pago(
    request: Request,
    service: ServiceDep,
    ids: Annotated[list[str] | None, Query()] = None,
):
    error: str | None = None
    status_code = 200
    facturas: list[sqlite3.Row] = []
    try:
        facturas = service.facturas_para_orden_pago(ids or [])
    except ServiceError as exc:
        error = str(exc)
        status_code = _STATUS_ERROR.get(type(exc), 422)
    return templates.TemplateResponse(
        request,
        "orden_pago.html",
        {
            "facturas": facturas,
            "ids": [f["id"] for f in facturas],
            "cliente": facturas[0]["cliente"] if facturas else "",
            "totales": _totales_por_moneda(facturas),
            "cuenta": (
                _cuenta_del_cliente(service.conn, facturas[0]) if facturas else ""
            ),
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/orden-de-pago/facturas-e")
def descargar_facturas_e(
    service: ServiceDep,
    ids: Annotated[list[str] | None, Query()] = None,
):
    nombre, contenido = service.pdf_facturas_orden_pago(ids or [])
    return _pdf(nombre, contenido)


@router.post("/ui/orden-de-pago/recaudadora")
async def descargar_recaudadora(
    service: ServiceDep,
    ids: Annotated[list[str] | None, Form()] = None,
    cuenta: str = Form(""),
    numeros: Annotated[list[str] | None, Form()] = None,
    archivos: Annotated[list[UploadFile] | None, File()] = None,
):
    subidos: list[PdfSubido] = []
    for archivo in archivos or []:
        nombre = archivo.filename or ""
        # Un input de archivo vacío llega como una parte sin nombre ni bytes.
        if not nombre and not archivo.size:
            continue
        # Uno de más alcanza para saber que se pasó del tope.
        contenido = await archivo.read(MAX_PDF_BYTES + 1)
        subidos.append(
            PdfSubido(nombre=nombre or "archivo sin nombre", contenido=contenido)
        )
    nombre, contenido = service.pdf_recaudadora_orden_pago(
        ids or [], cuenta, subidos, numeros or []
    )
    return _pdf(nombre, contenido)


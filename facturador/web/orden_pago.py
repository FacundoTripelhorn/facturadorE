"""Pantalla "Orden de pago": une las Facturas E y los invoices de la cuenta
recaudadora que pide el banco para ingresar un cobro.

    GET  /orden-de-pago/nueva?cliente=…       elegir cliente y sus facturas
    GET  /orden-de-pago?ids=…                 facturas elegidas, suma y carga
    GET  /orden-de-pago/facturas-e?ids=…      PDF con las Facturas E unidas
    POST /ui/orden-de-pago/recaudadora        PDF con los invoices subidos

La orden se arma en dos pasos: primero el cliente y después sus facturas
autorizadas, así que la pantalla no mezcla clientes; el backend igual lo
valida en cada pedido. Las dos descargas son independientes. Los errores de
las descargas salen como JSON ``{"detail": …}`` (handlers de ``api/app.py``)
para que el script de ``_descarga_pdf.html`` los muestre arriba de la
página. Los invoices subidos se leen en memoria y se descartan: no se
guardan en la base ni en disco.
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
from ..orden_pago import (
    MAX_PDF_BYTES,
    PdfSubido,
    RutaOrdenPago,
    facturas_del_cliente,
    identidad_cliente,
    identidad_receptor,
)
from ..service import (
    MAX_FACTURAS_ORDEN_PAGO,
    ConflictError,
    DomainError,
    NotFoundError,
    ServiceError,
)
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


def _cuenta_del_cliente(conn: sqlite3.Connection, facturas: list[sqlite3.Row]) -> str:
    """Cuenta recaudadora del cliente de la orden. Las facturas
    reconstruidas desde ARCA no tienen ``client_id``: vale cualquiera que sí."""
    client_id = next((f["client_id"] for f in facturas if f["client_id"]), None)
    if client_id is None:
        return ""
    cliente = repo.get_client(conn, client_id)
    if cliente is None:
        return ""
    return cliente["cuenta_recaudadora"] or ""


# Mensaje fijo: el valor de ``cliente`` viene de la URL y no se muestra.
_ERROR_CLIENTE_INEXISTENTE = "El cliente elegido no existe. Elegí uno de la lista."


def _id_de_cliente(conn: sqlite3.Connection, facturas: list[sqlite3.Row]) -> str:
    """Cliente al que vuelve "Cambiar las facturas de la orden".

    Sale del ``client_id`` de las facturas. Si todas se reconstruyeron desde
    ARCA no tienen ninguno: ``mismo_cliente`` garantiza que comparten
    receptor, así que se busca el cliente con esa identidad. Devuelve "" si
    no hay ninguno (por ejemplo, si cambió su ID impositivo después).
    """
    propio = next((f["client_id"] for f in facturas if f["client_id"]), "")
    if propio or not facturas:
        return propio
    receptor = identidad_receptor(facturas[0])
    return next(
        (c["id"] for c in repo.list_clients(conn) if identidad_cliente(c) == receptor),
        "",
    )


def _pdf(nombre: str, contenido: bytes) -> Response:
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.get(RutaOrdenPago.NUEVA, response_class=HTMLResponse)
def orden_de_pago_nueva(
    request: Request,
    service: ServiceDep,
    cliente: str = "",
    ids: Annotated[list[str] | None, Query()] = None,
):
    """Paso 1: elegir el cliente. Paso 2: sus facturas autorizadas.

    ``cliente`` es entrada no confiable: solo vale si es el id de un cliente
    que existe (se busca en la lista, no se consulta con el texto recibido);
    si no, se pide elegir de nuevo con un mensaje fijo. ``ids`` marca de
    antemano las facturas de la selección anterior: solo cuentan las que
    figuran en la lista del cliente.
    """
    clientes = repo.list_clients(service.conn)
    elegido = next((c for c in clientes if c["id"] == cliente), None)
    error: str | None = None
    status_code = 200
    if cliente and elegido is None:
        error = _ERROR_CLIENTE_INEXISTENTE
        status_code = 422
    # Sin cliente elegido, el selector llega con el cliente por defecto ya
    # cargado; las facturas aparecen recién cuando se aprieta "Ver facturas".
    por_defecto = next((c for c in clientes if c["is_default"]), None)
    preseleccionado = elegido or por_defecto
    facturas: list[sqlite3.Row] = []
    if elegido is not None:
        facturas = facturas_del_cliente(
            elegido, repo.list_authorized_for_client(service.conn, elegido["id"])
        )
    return templates.TemplateResponse(
        request,
        "orden_pago_nueva.html",
        {
            "clientes": clientes,
            "cliente": elegido,
            "preseleccionado_id": preseleccionado["id"] if preseleccionado else "",
            "facturas": facturas,
            "receptores": {f["id"]: identidad_receptor(f) for f in facturas},
            "marcadas": {f["id"] for f in facturas} & set(ids or []),
            "max_facturas": MAX_FACTURAS_ORDEN_PAGO,
            "error": error,
        },
        status_code=status_code,
    )


@router.get(RutaOrdenPago.PANTALLA, response_class=HTMLResponse)
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
            "cliente_id": _id_de_cliente(service.conn, facturas),
            "totales": _totales_por_moneda(facturas),
            "cuenta": _cuenta_del_cliente(service.conn, facturas),
            "error": error,
        },
        status_code=status_code,
    )


@router.get(RutaOrdenPago.FACTURAS_E)
def descargar_facturas_e(
    service: ServiceDep,
    ids: Annotated[list[str] | None, Query()] = None,
):
    nombre, contenido = service.pdf_facturas_orden_pago(ids or [])
    return _pdf(nombre, contenido)


@router.post(RutaOrdenPago.RECAUDADORA)
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

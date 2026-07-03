"""API REST (contrato spike.md §2.3), servida solo en localhost (§2.5)."""

from __future__ import annotations

import logging
import sqlite3

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import db, repo
from .config import Config, load_config
from .schemas import (
    ClientIn,
    ClientOut,
    HealthOut,
    InvoiceCreate,
    InvoiceOut,
    ItemOut,
    ParamOut,
    RateOut,
)
from .service import (
    ArcaUnavailableError,
    ConflictError,
    DomainError,
    InvoiceService,
    NotFoundError,
)
from .wsfex import PARAM_METHODS, WsfexClient, WsfexError

logger = logging.getLogger(__name__)

_ERROR_STATUS = {
    NotFoundError: 404,
    ConflictError: 409,
    DomainError: 422,
    ArcaUnavailableError: 503,
}


def _invoice_out(conn: sqlite3.Connection, row: sqlite3.Row) -> InvoiceOut:
    items = [ItemOut(**dict(i)) for i in repo.get_invoice_items(conn, row["id"])]
    campos = {k: row[k] for k in row.keys() if k in InvoiceOut.model_fields}
    return InvoiceOut(**campos, items=items)


def create_app(
    config: Config | None = None,
    conn: sqlite3.Connection | None = None,
    wsfex: WsfexClient | None = None,
) -> FastAPI:
    config = config or load_config()
    conn = conn or db.connect(config.data_dir / "facturador.db")
    wsfex = wsfex or WsfexClient(config)
    service = InvoiceService(config, conn, wsfex)

    app = FastAPI(title="facturador", version="0.1.0")
    app.state.service = service

    def _handler_for(status: int):
        async def handler(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    for tipo, status in _ERROR_STATUS.items():
        app.add_exception_handler(tipo, _handler_for(status))

    # --- facturas ---

    @app.post("/invoices", response_model=InvoiceOut, status_code=201)
    def create_invoice(payload: InvoiceCreate):
        return _invoice_out(conn, service.create_invoice(payload))

    @app.post("/invoices/{invoice_id}/authorize", response_model=InvoiceOut)
    def authorize(invoice_id: str, force_desync: bool = Query(default=False)):
        return _invoice_out(conn, service.authorize(invoice_id, force_desync))

    @app.get("/invoices/{invoice_id}", response_model=InvoiceOut)
    def get_invoice(invoice_id: str):
        return _invoice_out(conn, service.get_invoice(invoice_id))

    @app.get("/invoices", response_model=list[InvoiceOut])
    def list_invoices(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return [_invoice_out(conn, r) for r in repo.list_invoices(conn, limit, offset)]

    @app.get("/invoices/{invoice_id}/pdf")
    def invoice_pdf(invoice_id: str):
        service.get_invoice(invoice_id, reconcile=False)  # 404 si no existe
        raise HTTPException(status_code=501, detail="PDF: fase 5 del spike")

    # --- clientes ---

    @app.post("/clients", response_model=ClientOut, status_code=201)
    def create_client(payload: ClientIn):
        return ClientOut(**dict(service.create_client(payload)))

    @app.put("/clients/{client_id}", response_model=ClientOut)
    def update_client(client_id: str, payload: ClientIn):
        return ClientOut(**dict(service.update_client(client_id, payload)))

    @app.get("/clients", response_model=list[ClientOut])
    def list_clients():
        return [ClientOut(**dict(r)) for r in repo.list_clients(conn)]

    # --- parámetros y salud ---

    @app.get("/params/currency/{moneda_id}/rate", response_model=RateOut)
    def currency_rate(moneda_id: str, date: str | None = Query(default=None)):
        try:
            ctz, fecha = wsfex.get_ctz(moneda_id, date)
        except WsfexError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"ARCA no disponible: {exc}")
        return RateOut(moneda_id=moneda_id, fecha=fecha, cotizacion=format(ctz, "f"))

    @app.get("/params/{kind}", response_model=list[ParamOut])
    def get_params(kind: str):
        if kind not in PARAM_METHODS:
            raise HTTPException(
                status_code=404,
                detail=f"kind inválido; usar uno de: {', '.join(PARAM_METHODS)}",
            )
        return [
            ParamOut(
                code=r["code"],
                description=r["description"],
                valid_from=r["valid_from"],
                valid_to=r["valid_to"],
            )
            for r in service.get_params(kind)
        ]

    @app.get("/health/arca", response_model=HealthOut)
    def health_arca():
        try:
            estado = wsfex.dummy()
        except (WsfexError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=503, detail=f"FEXDummy falló: {exc}")
        return HealthOut(environment=config.env, **estado)

    return app

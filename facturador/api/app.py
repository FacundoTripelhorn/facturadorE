"""Ensamblado de la app FastAPI: wiring de dependencias, manejo de errores
de dominio y registro de routers (API JSON + frontend HTML §2.4).
Servida solo en localhost (spike.md §2.5)."""

from __future__ import annotations

import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db, web
from ..arca.wsfex import WsfexClient
from ..config import Config, load_config
from ..service import (
    ArcaUnavailableError,
    ConflictError,
    DomainError,
    InvoiceService,
    NotFoundError,
)
from . import clients, health, invoices, params

_ERROR_STATUS = {
    NotFoundError: 404,
    ConflictError: 409,
    DomainError: 422,
    ArcaUnavailableError: 503,
}


def create_app(
    config: Config | None = None,
    conn: sqlite3.Connection | None = None,
    wsfex: WsfexClient | None = None,
) -> FastAPI:
    config = config or load_config()
    conn = conn or db.connect(config.data_dir / "facturador.db")
    wsfex = wsfex or WsfexClient(config)

    app = FastAPI(title="facturador", version="0.1.0")
    app.state.service = InvoiceService(config, conn, wsfex)

    def _handler_for(status: int):
        async def handler(request: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    for tipo, status in _ERROR_STATUS.items():
        app.add_exception_handler(tipo, _handler_for(status))

    app.include_router(invoices.router)
    app.include_router(clients.router)
    app.include_router(params.router)
    app.include_router(health.router)

    # Frontend HTML (§2.4): mismas dependencias vía app.state.service. Los
    # errores de dominio del frontend se renderizan en partials, no acá.
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")

    return app

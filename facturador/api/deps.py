"""Dependencias de los routers: todo sale del InvoiceService en app.state
(el service ya carga config, conexión y cliente WSFEX)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..service import InvoiceService


def _get_service(request: Request) -> InvoiceService:
    return request.app.state.service


ServiceDep = Annotated[InvoiceService, Depends(_get_service)]

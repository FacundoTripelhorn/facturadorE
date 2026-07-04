"""Rutas de clientes."""

from __future__ import annotations

from fastapi import APIRouter

from .. import repo
from ..schemas import ClientIn, ClientOut
from .deps import ServiceDep

router = APIRouter(prefix="/clients", tags=["clients"])


@router.post("", response_model=ClientOut, status_code=201)
def create_client(payload: ClientIn, service: ServiceDep):
    return ClientOut(**dict(service.create_client(payload)))


@router.put("/{client_id}", response_model=ClientOut)
def update_client(client_id: str, payload: ClientIn, service: ServiceDep):
    return ClientOut(**dict(service.update_client(client_id, payload)))


@router.get("", response_model=list[ClientOut])
def list_clients(service: ServiceDep):
    return [ClientOut(**dict(r)) for r in repo.list_clients(service.conn)]

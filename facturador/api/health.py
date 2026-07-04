"""Salud de la integración con ARCA (FEXDummy)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from ..arca.wsfex import WsfexError
from ..schemas import HealthOut
from .deps import ServiceDep

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/arca", response_model=HealthOut)
def health_arca(service: ServiceDep):
    try:
        estado = service.wsfex.dummy()
    except (WsfexError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail=f"FEXDummy falló: {exc}") from exc
    return HealthOut(environment=service.config.env, **estado)

"""Salud: liveness local (para healthcheck/launcher) y estado de ARCA."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from ..arca.wsfex import WsfexError
from ..schemas import HealthOut
from .deps import ServiceDep

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health(service: ServiceDep) -> dict[str, str]:
    """Liveness sin tocar ARCA: la usan el HEALTHCHECK de Docker y el
    launcher, que corren cada pocos segundos — FEXDummy acá sería spam."""
    return {"status": "ok", "environment": service.config.env}


@router.get("/arca", response_model=HealthOut)
def health_arca(service: ServiceDep):
    try:
        estado = service.wsfex.dummy()
    except (WsfexError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail=f"FEXDummy falló: {exc}") from exc
    return HealthOut(environment=service.config.env, **estado)

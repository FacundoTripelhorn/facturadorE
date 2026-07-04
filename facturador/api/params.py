"""Rutas de tablas de parámetros ARCA (cache) y cotización de moneda."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..arca.wsfex import PARAM_METHODS, WsfexError
from ..schemas import ParamOut, RateOut
from .deps import ServiceDep

router = APIRouter(prefix="/params", tags=["params"])


@router.get("/currency/{moneda_id}/rate", response_model=RateOut)
def currency_rate(
    moneda_id: str, service: ServiceDep, date: str | None = Query(default=None)
):
    try:
        ctz, fecha = service.wsfex.get_ctz(moneda_id, date)
    except WsfexError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"ARCA no disponible: {exc}"
        ) from exc
    return RateOut(moneda_id=moneda_id, fecha=fecha, cotizacion=format(ctz, "f"))


@router.get("/{kind}", response_model=list[ParamOut])
def get_params(kind: str, service: ServiceDep):
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

"""Catch-up del registro local desde ARCA."""

from __future__ import annotations

from fastapi import APIRouter

from .deps import ServiceDep

router = APIRouter(prefix="/registry", tags=["registry"])


@router.post("/catch-up")
def catch_up(service: ServiceDep) -> dict:
    """Sincroniza N_local+1..N_arca por cada (PV, tipo) del emisor activo."""
    report = service.catch_up_from_arca()
    return {
        "mode": report.mode.value,
        "inserted": report.inserted,
        "gaps": [
            {
                "punto_venta": g.punto_venta,
                "cbte_tipo": g.cbte_tipo,
                "cbte_nro": g.cbte_nro,
            }
            for g in report.gaps
        ],
        "last_cmp": {
            f"{pv}:{tipo}": n for (pv, tipo), n in report.last_cmp.items()
        },
    }

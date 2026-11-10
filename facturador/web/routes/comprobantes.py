"""Registro de comprobantes y resumen anual."""

from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import repo
from ...api.deps import ServiceDep
from ...resumen import MESES, resumir
from ..constants import TabComprobantes
from ..grafico import grafico_por_mes
from ._comun import (
    TAB_FILTERS,
    TAB_LABELS,
    _catchup_aviso,
    templates,
)

router = APIRouter()


@router.get("/comprobantes", response_class=HTMLResponse)
def comprobantes(
    request: Request,
    service: ServiceDep,
    estado: str = TabComprobantes.TODAS,
    aviso: str = "",
    n: str = "",
    error: str = "",
):
    tab = TabComprobantes.desde_query(estado)
    counts = repo.count_invoices_by_status(service.conn)
    tab_counts = {
        tab: (
            sum(counts.values())
            if statuses is None
            else sum(counts.get(s, 0) for s in statuses)
        )
        for tab, statuses in TAB_FILTERS.items()
    }
    return templates.TemplateResponse(
        request,
        "comprobantes.html",
        {
            "tabs": TAB_LABELS,
            "tab_counts": tab_counts,
            "estado": tab,
            "aviso": _catchup_aviso(aviso, n),
            "error": error or None,
            # Las facturas de una orden de pago se eligen en esta solapa.
            "seleccion_orden": tab is TabComprobantes.AUTORIZADAS,
            "facturas": repo.list_invoices(
                service.conn, limit=50, offset=0, statuses=TAB_FILTERS[tab]
            ),
        },
    )


@router.get("/comprobantes/resumen", response_class=HTMLResponse)
def comprobantes_resumen(request: Request, service: ServiceDep, anio: str = ""):
    """Totales del año en la moneda original de cada comprobante autorizado."""
    env = service.config.env
    anio_actual = dt.date.today().year
    # Solo dígitos ASCII: str.isdigit() acepta "²" y otros que int() rechaza.
    elegido = int(anio) if re.fullmatch(r"[0-9]{4}", anio) else anio_actual
    anios = repo.authorized_years(service.conn, env)
    # El selector ofrece los años con comprobantes; el elegido figura aunque
    # no tenga ninguno (el actual, por defecto) para no mostrar otro.
    opciones = sorted({*anios, elegido}, reverse=True)
    resumen = resumir(elegido, repo.list_authorized_in_year(service.conn, env, elegido))
    return templates.TemplateResponse(
        request,
        "resumen.html",
        {
            "resumen": resumen,
            "anios": opciones,
            "meses": MESES,
            "graficos": [
                (m.moneda, grafico_por_mes(m.por_mes)) for m in resumen.monedas
            ],
        },
    )

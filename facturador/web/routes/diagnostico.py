"""Diagnóstico: ¿se puede emitir con este perfil?"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...api.deps import ServiceDep
from ...diagnostics import diagnosticar_arca, diagnosticar_perfil, peor_nivel
from ._comun import (
    templates,
)

router = APIRouter()


@router.get("/diagnostico", response_class=HTMLResponse)
def diagnostico(request: Request, service: ServiceDep, arca: bool = False):
    """¿Se puede emitir con este perfil? Los chequeos locales corren siempre;
    los de ARCA solo con ``?arca=1`` (botón), porque pueden tardar."""
    profile = request.app.state.profile
    # ARCA primero: cada respuesta actualiza los avisos guardados, que el
    # chequeo local lee después.
    remotos = diagnosticar_arca(profile, service.conn, service.wsfex) if arca else None
    locales = diagnosticar_perfil(profile, service.conn)
    return templates.TemplateResponse(
        request,
        "diagnostico.html",
        {
            "locales": locales,
            "remotos": remotos,
            "arca_verificado": arca,
            "nivel": peor_nivel(locales + (remotos or [])),
        },
    )

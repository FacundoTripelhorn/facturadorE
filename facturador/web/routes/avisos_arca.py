"""Avisos de ARCA."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ...arca.events import mark_read
from ..constants import Pagina
from ._comun import (
    _pagina_de_origen,
)

router = APIRouter()


@router.post("/ui/avisos-arca/{event_id}/leido")
def marcar_aviso_arca_leido(request: Request, event_id: str):
    """Oculta el aviso del banner; sigue en el desplegable mientras ARCA lo
    mande. Vuelve a la página desde la que se marcó."""
    mark_read(request.app.state.profile.paths.arca_events, event_id)
    return RedirectResponse(
        _pagina_de_origen(request) or Pagina.INICIO, status_code=HTTPStatus.SEE_OTHER
    )

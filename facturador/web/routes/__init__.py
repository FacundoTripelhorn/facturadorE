"""Rutas HTML del frontend. Lógica cero: cada handler arma el contexto con
el InvoiceService/repo y renderiza; las reglas de dominio viven en service.

Flujo de emisión en páginas separadas (patrón Post/Redirect/Get):

    GET  /                        form de nueva factura (solo acá hay inputs)
    POST /ui/facturas             crea el borrador → 303 a la revisión
    GET  /facturas/{id}/revisar   página de revisión: QUÉ se va a enviar
    POST /ui/facturas/{id}/authorize  confirma → 303 al detalle
    GET  /facturas/{id}           detalle read-only: qué se envió, estado,
                                  CAE y PDF; sin ningún input
    POST /ui/facturas/{id}/descartar  borra el borrador → 303 al form

Nada viaja a ARCA sin pasar por la revisión; el detalle es el registro.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...api.csrf import enforce_csrf
from . import (
    ambiente,
    avisos_arca,
    backups,
    clientes,
    comprobantes,
    configuracion,
    constatacion,
    diagnostico,
    emisores,
    emitir,
    facturas,
    setup,
)
from ._comun import (
    STATIC_DIR,
    TEMPLATES_DIR,
    csrf_recovery_href,
    csrf_rejected_handler,
    templates,
)

router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(enforce_csrf)],
)
# Mismo orden que tenían en el routes.py único.
for _area in (
    emitir,
    facturas,
    constatacion,
    comprobantes,
    clientes,
    setup,
    diagnostico,
    configuracion,
    emisores,
    backups,
    avisos_arca,
    ambiente,
):
    router.include_router(_area.router)

__all__ = [
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "csrf_recovery_href",
    "csrf_rejected_handler",
    "router",
    "templates",
]

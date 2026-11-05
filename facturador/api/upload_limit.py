"""Tope de tamaño para las subidas de la orden de pago.

Los invoices de la cuenta recaudadora se procesan en memoria y no deben
tocar el disco. El parser multipart de Starlette guarda cada archivo en un
``SpooledTemporaryFile`` que pasa a disco al superar ``spool_max_size``;
FastAPI parsea el cuerpo antes de correr cualquier dependency, así que el
tope va acá, antes de leer el cuerpo: un pedido más grande que
``MAX_UPLOAD_BYTES`` (o sin ``Content-Length``) se rechaza con 413 sin
leerlo, y todo lo que pasa entra en el umbral de memoria del parser.
"""

from __future__ import annotations

from starlette.formparsers import MultiPartParser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..orden_pago import MAX_UPLOAD_BYTES

UPLOAD_PREFIX = "/ui/orden-de-pago/"

# Nada de lo que acepta la app pasa a un archivo temporal en disco.
MultiPartParser.spool_max_size = MAX_UPLOAD_BYTES


class UploadLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].startswith(UPLOAD_PREFIX)
            and not _dentro_del_tope(scope)
        ):
            mb = MAX_UPLOAD_BYTES // (1024 * 1024)
            respuesta = JSONResponse(
                status_code=413,
                content={"detail": f"Los PDFs subidos pesan más de {mb} MB."},
            )
            await respuesta(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _dentro_del_tope(scope: Scope) -> bool:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return 0 <= int(value) <= MAX_UPLOAD_BYTES
            except ValueError:
                return False
    return False

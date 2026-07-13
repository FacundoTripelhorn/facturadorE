"""Guardia de setup: bloquea facturación y ARCA hasta que el perfil esté listo.

FAC-35: mientras el estado del perfil no sea ``ready``, las operaciones de
factura y las que tocan ARCA responden 503. Quedan libres el liveness
(``GET /health``), las rutas de setup (``/setup``) y los estáticos; la
configuración de emisor sigue accesible para completar el onboarding
(FAC-38) sin circular dependency.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..setup import SetupState, SetupStateProvider, is_ready


def is_setup_exempt(method: str, path: str) -> bool:
    """Rutas permitidas aunque el perfil no esté ``ready``."""
    if path == "/health" or path == "/health/":
        return True
    if path == "/setup" or path.startswith("/setup/"):
        return True
    if path.startswith("/ui/setup"):
        return True
    if path.startswith("/static/"):
        return True
    # Emisor / PV / backups: necesarios para avanzar el setup (FAC-38).
    if path == "/configuracion" or path.startswith("/configuracion/"):
        return True
    if path.startswith("/ui/emisores") or path.startswith("/ui/configuracion"):
        return True
    if path == "/ui/cambiar-ambiente":
        return True
    return False


def requires_ready_profile(method: str, path: str) -> bool:
    """True si la ruta es factura/ARCA (o el form de emisión) y exige ready."""
    if is_setup_exempt(method, path):
        return False
    if path.startswith("/invoices"):
        return True
    # HTML de facturas: el detalle reconcilia UNKNOWN vía FEXGetCMP (ARCA).
    if path.startswith("/facturas") or path.startswith("/ui/facturas"):
        return True
    if path.startswith("/params"):
        return True
    if path == "/health/arca" or path.startswith("/health/arca/"):
        return True
    # Home: el form pide cotización ARCA; sin setup listo no es usable.
    if method == "GET" and path in ("/", ""):
        return True
    return False


def setup_blocked_detail(state: SetupState) -> dict[str, str]:
    return {
        "detail": (
            "El perfil aún no completó el setup; "
            f"paso actual: {state.value}"
        ),
        "setup_state": state.value,
    }


class SetupGuardMiddleware:
    """Bloquea facturación/ARCA hasta ``ready``; setup y health quedan libres."""

    def __init__(self, app: ASGIApp, *, get_state: SetupStateProvider) -> None:
        self.app = app
        self.get_state = get_state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        method = request.method.upper()
        path = request.url.path
        if not requires_ready_profile(method, path):
            await self.app(scope, receive, send)
            return

        state = self.get_state()
        if is_ready(state):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            setup_blocked_detail(state),
            status_code=503,
        )
        await response(scope, receive, send)

"""Guardia de setup: bloquea facturación y ARCA hasta que el perfil esté listo.

Mientras el estado del perfil no sea ``ready``, las operaciones de
factura y las que tocan ARCA quedan bloqueadas. Las rutas HTML de la UI
redirigen a ``/setup`` para que el usuario vea el paso pendiente;
las APIs JSON siguen respondiendo 503 con ``setup_state``. Quedan libres el
liveness (``GET /health``), las rutas de setup (``/setup``), estáticos,
configuración de emisor, clientes (CRUD offline con params cacheados) y el
diagnóstico del perfil.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..setup import SetupState, SetupStateProvider, is_ready

# Destino del onboarding. Query opcional para diagnóstico.
_SETUP_REDIRECT = "/setup"


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
    # Emisor / PV / backups: necesarios para avanzar el setup.
    if path == "/configuracion" or path.startswith("/configuracion/"):
        return True
    if path.startswith("/ui/emisores") or path.startswith("/ui/configuracion"):
        return True
    if path == "/ui/cambiar-ambiente":
        return True
    # Clientes: CRUD offline con arca_params cacheados (AGENTS.md).
    if path == "/clientes" or path.startswith("/clientes/"):
        return True
    if path.startswith("/ui/clientes"):
        return True
    # Diagnóstico: justamente explica qué falta; los chequeos de ARCA se
    # cortan solos si el perfil no está listo.
    if path == "/diagnostico":
        return True
    # Seed backup status / manual trigger: no depende de ARCA.
    if path == "/backup" or path.startswith("/backup/"):
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
    # Constatación WSCDC: necesita cert + TA ``wscdc``.
    if path.startswith("/constatacion") or path.startswith("/ui/constatacion"):
        return True
    if path.startswith("/params"):
        return True
    if path == "/registry" or path.startswith("/registry/"):
        return True
    if path.startswith("/ui/registry"):
        return True
    if path == "/health/arca" or path.startswith("/health/arca/"):
        return True
    # Home: el form pide cotización ARCA; sin setup listo no es usable.
    if method == "GET" and path in ("/", ""):
        return True
    return False


def is_html_ui_route(method: str, path: str) -> bool:
    """Rutas de UI del browser: redirigir a ``/setup`` en vez de JSON 503."""
    if method == "GET" and path in ("/", ""):
        return True
    if path.startswith("/facturas") or path.startswith("/ui/facturas"):
        return True
    if path.startswith("/constatacion") or path.startswith("/ui/constatacion"):
        return True
    if path.startswith("/ui/registry"):
        return True
    return False


def setup_blocked_detail(state: SetupState) -> dict[str, str]:
    return {
        "detail": (
            "El perfil aún no completó el setup; "
            f"paso actual: {state.value}. Completar en /setup."
        ),
        "setup_state": state.value,
        "setup_url": _SETUP_REDIRECT,
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

        if is_html_ui_route(method, path):
            # 303: el launcher abre ``/``; el usuario termina en el onboarding
            # con el paso pendiente (certificado, emisor, PV) en pantalla.
            target = f"{_SETUP_REDIRECT}?desde={state.value}"
            response: RedirectResponse | JSONResponse = RedirectResponse(
                url=target, status_code=303
            )
        else:
            response = JSONResponse(
                setup_blocked_detail(state),
                status_code=503,
            )
        await response(scope, receive, send)

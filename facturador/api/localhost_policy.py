"""Política estricta de Host y Origin para el servidor local (FAC-41).

La app solo acepta tráfico del browser local. Validar Host mitiga DNS
rebinding; validar Origin/Referer en métodos que cambian estado mitiga
peticiones cross-origin inesperadas. No hay CORS permisivo ni ``*``.

Formas de loopback aceptadas (puerto = ``FACTURADOR_PORT`` / launcher):

* Host: ``127.0.0.1:<port>``, ``localhost:<port>``, ``[::1]:<port>``
* Origin: ``http://127.0.0.1:<port>``, ``http://localhost:<port>``,
  ``http://[::1]:<port>``

Sin TLS (design.md §2.5). Los clientes no-browser (API, health del
launcher) que no envían Origin/Referer siguen permitidos en este issue;
FAC-42 cubre tokens CSRF en formularios del browser.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..constants import DEFAULT_PORT

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def resolve_listen_port(port: int | None = None) -> int:
    """Puerto de escucha: argumento explícito o ``FACTURADOR_PORT``."""
    if port is not None:
        if not 1 <= port <= 65535:
            raise ValueError(f"Puerto inválido para la política local: {port}")
        return port
    raw = os.environ.get("FACTURADOR_PORT", str(DEFAULT_PORT))
    try:
        resolved = int(raw)
    except ValueError as exc:
        raise ValueError(f"FACTURADOR_PORT inválido: {raw!r}") from exc
    if not 1 <= resolved <= 65535:
        raise ValueError(f"FACTURADOR_PORT fuera de rango: {resolved}")
    return resolved


def test_client_base_url(port: int | None = None) -> str:
    """Origen ``http://127.0.0.1:<port>`` para TestClient (Host válido)."""
    return f"http://127.0.0.1:{resolve_listen_port(port)}"


def allowed_hosts(port: int) -> frozenset[str]:
    """Valores de ``Host`` permitidos para el puerto del launcher."""
    return frozenset(
        {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        }
    )


def allowed_origins(port: int) -> frozenset[str]:
    """Orígenes http de loopback permitidos (sin ``*``, sin https)."""
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
    )


def is_allowed_host(host: str, port: int) -> bool:
    """True si el header Host es exactamente una forma de loopback + puerto."""
    return host.strip().lower() in {h.lower() for h in allowed_hosts(port)}


def origin_from_referer(referer: str) -> str | None:
    """Extrae ``scheme://host[:port]`` del Referer; None si no es parseable."""
    parsed = urlparse(referer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def is_allowed_origin(origin: str, port: int) -> bool:
    """True si Origin (o el origen derivado del Referer) está en la allowlist."""
    return origin.strip().lower() in {o.lower() for o in allowed_origins(port)}


def check_host_header(host: str | None, port: int) -> str | None:
    """Devuelve mensaje de error si Host es inválido; None si pasa."""
    if not host:
        return "Host header requerido"
    if not is_allowed_host(host, port):
        return "Host no permitido"
    return None


def check_origin_headers(
    method: str,
    origin: str | None,
    referer: str | None,
    port: int,
) -> str | None:
    """Valida Origin/Referer en métodos que cambian estado.

    - Origin presente e inesperado → rechazo.
    - Sin Origin pero Referer con origen inesperado → rechazo.
    - Sin Origin ni Referer → permitido (clientes no-browser; ver FAC-42).
    """
    if method.upper() not in _UNSAFE_METHODS:
        return None

    if origin is not None and origin != "":
        if origin.lower() == "null" or not is_allowed_origin(origin, port):
            return "Origin no permitido"
        return None

    if referer:
        derived = origin_from_referer(referer)
        if derived is None or not is_allowed_origin(derived, port):
            return "Referer no permitido"
    return None


class LocalhostPolicyMiddleware:
    """ASGI middleware: Host siempre; Origin/Referer en métodos unsafe."""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.port = resolve_listen_port(port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host_error = check_host_header(headers.get("host"), self.port)
        if host_error is not None:
            response = JSONResponse({"detail": host_error}, status_code=400)
            await response(scope, receive, send)
            return

        origin_error = check_origin_headers(
            method=scope.get("method", "GET"),
            origin=headers.get("origin"),
            referer=headers.get("referer"),
            port=self.port,
        )
        if origin_error is not None:
            response = JSONResponse({"detail": origin_error}, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

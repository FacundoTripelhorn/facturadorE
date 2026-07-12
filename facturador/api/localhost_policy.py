"""Política estricta de Host y Origin para el servidor local (FAC-41).

La app solo acepta tráfico del browser local. Validar Host mitiga DNS
rebinding; validar Origin/Referer en métodos que cambian estado mitiga
peticiones cross-origin inesperadas. No hay CORS permisivo ni ``*``.

Formas de loopback aceptadas (puerto = listen y, si aplica, público):

* Host: ``127.0.0.1:<port>``, ``localhost:<port>``, ``[::1]:<port>``
* Origin: ``http://127.0.0.1:<port>``, ``http://localhost:<port>``,
  ``http://[::1]:<port>``

Puertos de la allowlist:

* Nativo / launcher: ``FACTURADOR_PORT`` (bind = Host del browser).
* Docker: bind interno fijo en 8399; el publish del host puede diferir
  (``127.0.0.1:${FACTURADOR_PORT}:8399``). En ese caso compose inyecta
  ``FACTURADOR_PUBLIC_PORT`` con el puerto del host; la allowlist acepta
  ambos (healthcheck interno + browser en el puerto publicado).

Sin TLS (design.md §2.5). Los clientes no-browser (API, health del
launcher) que no envían Origin/Referer siguen permitidos en este issue;
FAC-42 cubre tokens CSRF en formularios del browser.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..constants import DEFAULT_PORT

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

PolicyPorts = int | Iterable[int]


def _parse_port(raw: str, *, label: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} inválido: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} fuera de rango: {port}")
    return port


def resolve_listen_port(port: int | None = None) -> int:
    """Puerto de bind uvicorn: argumento explícito o ``FACTURADOR_PORT``."""
    if port is not None:
        return _parse_port(str(port), label="Puerto de escucha")
    return _parse_port(
        os.environ.get("FACTURADOR_PORT", str(DEFAULT_PORT)),
        label="FACTURADOR_PORT",
    )


def resolve_public_port(public_port: int | None = None) -> int | None:
    """Puerto que ve el browser (Docker host publish), o None si no aplica.

    En nativo no hace falta: coincide con el listen. En Docker, compose
    inyecta ``FACTURADOR_PUBLIC_PORT`` con el lado host del mapping.
    """
    if public_port is not None:
        return _parse_port(str(public_port), label="Puerto público")
    raw = os.environ.get("FACTURADOR_PUBLIC_PORT")
    if raw is None or raw.strip() == "":
        return None
    return _parse_port(raw, label="FACTURADOR_PUBLIC_PORT")


def resolve_policy_ports(
    listen_port: int | None = None,
    public_port: int | None = None,
) -> frozenset[int]:
    """Puertos permitidos en Host/Origin: listen ∪ public (si distinto)."""
    listen = resolve_listen_port(listen_port)
    ports = {listen}
    resolved_public = resolve_public_port(public_port)
    if resolved_public is not None:
        ports.add(resolved_public)
    return frozenset(ports)


def normalize_policy_ports(ports: PolicyPorts) -> frozenset[int]:
    """Normaliza un puerto o iterable a frozenset validado."""
    if isinstance(ports, int):
        return frozenset({_parse_port(str(ports), label="Puerto de política")})
    normalized = {_parse_port(str(p), label="Puerto de política") for p in ports}
    if not normalized:
        raise ValueError("La allowlist Host/Origin requiere al menos un puerto")
    return frozenset(normalized)


def loopback_base_url(port: int | None = None) -> str:
    """Origen ``http://127.0.0.1:<port>`` para TestClient (Host válido).

    Named without a ``test_`` prefix so pytest does not collect it.
    """
    return f"http://127.0.0.1:{resolve_listen_port(port)}"


def allowed_hosts(ports: PolicyPorts) -> frozenset[str]:
    """Valores de ``Host`` permitidos para los puertos de la política."""
    hosts: set[str] = set()
    for port in normalize_policy_ports(ports):
        hosts.update(
            {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
                f"[::1]:{port}",
            }
        )
    return frozenset(hosts)


def allowed_origins(ports: PolicyPorts) -> frozenset[str]:
    """Orígenes http de loopback permitidos (sin ``*``, sin https)."""
    origins: set[str] = set()
    for port in normalize_policy_ports(ports):
        origins.update(
            {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
            }
        )
    return frozenset(origins)


def is_allowed_host(host: str, ports: PolicyPorts) -> bool:
    """True si el header Host es exactamente una forma de loopback + puerto."""
    return host.strip().lower() in {h.lower() for h in allowed_hosts(ports)}


def origin_from_referer(referer: str) -> str | None:
    """Extrae ``scheme://host[:port]`` del Referer; None si no es parseable."""
    parsed = urlparse(referer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def is_allowed_origin(origin: str, ports: PolicyPorts) -> bool:
    """True si Origin (o el origen derivado del Referer) está en la allowlist."""
    return origin.strip().lower() in {o.lower() for o in allowed_origins(ports)}


def check_host_header(host: str | None, ports: PolicyPorts) -> str | None:
    """Devuelve mensaje de error si Host es inválido; None si pasa."""
    if not host:
        return "Host header requerido"
    if not is_allowed_host(host, ports):
        return "Host no permitido"
    return None


def check_origin_headers(
    method: str,
    origin: str | None,
    referer: str | None,
    ports: PolicyPorts,
) -> str | None:
    """Valida Origin/Referer en métodos que cambian estado.

    - Origin presente e inesperado → rechazo.
    - Sin Origin pero Referer con origen inesperado → rechazo.
    - Sin Origin ni Referer → permitido (clientes no-browser; ver FAC-42).
    """
    if method.upper() not in _UNSAFE_METHODS:
        return None

    if origin is not None and origin != "":
        if origin.lower() == "null" or not is_allowed_origin(origin, ports):
            return "Origin no permitido"
        return None

    if referer:
        derived = origin_from_referer(referer)
        if derived is None or not is_allowed_origin(derived, ports):
            return "Referer no permitido"
    return None


class LocalhostPolicyMiddleware:
    """ASGI middleware: Host siempre; Origin/Referer en métodos unsafe."""

    def __init__(self, app: ASGIApp, *, ports: PolicyPorts) -> None:
        self.app = app
        self.ports = normalize_policy_ports(ports)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host_error = check_host_header(headers.get("host"), self.ports)
        if host_error is not None:
            response = JSONResponse({"detail": host_error}, status_code=400)
            await response(scope, receive, send)
            return

        origin_error = check_origin_headers(
            method=scope.get("method", "GET"),
            origin=headers.get("origin"),
            referer=headers.get("referer"),
            ports=self.ports,
        )
        if origin_error is not None:
            response = JSONResponse({"detail": origin_error}, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

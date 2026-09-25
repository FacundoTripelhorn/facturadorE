"""Protección CSRF para formularios del browser.

Patrón double-submit cookie, apto para una app localhost sin login:

* Cookie ``facturador_csrf`` (HttpOnly, SameSite=Strict, Path=/; sin Secure
  porque no hay TLS local — design.md §2.5).
* Campo oculto ``csrf_token`` (o header ``X-CSRF-Token``) en POSTs a ``/ui/``.
* Validación solo en métodos que cambian estado bajo ``/ui/``; GET y la API
  JSON quedan fuera (clientes no-browser / scripts).
* Rechazo bajo ``/ui/`` → HTML de sesión expirada; fuera de ``/ui/``
  el handler responde JSON genérico.

El material del token no se loguea ni se incluye en mensajes de error.
"""

from __future__ import annotations

import secrets
from collections.abc import MutableMapping
from typing import Final

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE_NAME: Final = "facturador_csrf"
CSRF_FORM_FIELD: Final = "csrf_token"
CSRF_HEADER_NAME: Final = "x-csrf-token"
CSRF_TOKEN_BYTES: Final = 32

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_UI_PREFIX = "/ui/"

# Mensajes genéricos: nunca incluyen el valor del token.
CSRF_JSON_DETAIL: Final = "CSRF token ausente o inválido"
CSRF_UI_MESSAGE: Final = (
    "La sesión expiró — recargá la página e intentá de nuevo."
)


class CsrfRejected(Exception):
    """CSRF ausente o inválido; el handler de app elige HTML (/ui/) o JSON."""


def generate_csrf_token() -> str:
    """Token opaco URL-safe; no loguear ni devolver en errores."""
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def tokens_match(expected: str | None, submitted: str | None) -> bool:
    """Comparación en tiempo constante; False si falta cualquiera."""
    if not expected or not submitted:
        return False
    if len(expected) != len(submitted):
        # compare_digest exige igual longitud; mismatch → inválido.
        return False
    return secrets.compare_digest(expected, submitted)


def csrf_token_for_request(request: Request) -> str:
    """Token ya fijado por el middleware (cookie existente o recién emitido)."""
    token = getattr(request.state, "csrf_token", None)
    if isinstance(token, str) and token:
        return token
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if cookie:
        return cookie
    # Defensa: el middleware debería haber poblado state antes del handler.
    return generate_csrf_token()


def _is_ui_mutating(method: str, path: str) -> bool:
    return method.upper() in _UNSAFE_METHODS and path.startswith(_UI_PREFIX)


async def enforce_csrf(request: Request) -> None:
    """Dependency del router HTML: exige cookie + form/header en ``/ui/`` POST.

    ``await request.form()`` es cacheado por Starlette, así que los handlers
    que declaran ``Form(...)`` siguen funcionando.
    """
    if not _is_ui_mutating(request.method, request.url.path):
        return

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    form_token: str | None = None

    content_type = request.headers.get("content-type", "")
    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        form = await request.form()
        raw = form.get(CSRF_FORM_FIELD)
        if isinstance(raw, str):
            form_token = raw

    submitted = header_token or form_token
    if not cookie_token or not submitted:
        raise CsrfRejected()
    if not tokens_match(cookie_token, submitted):
        raise CsrfRejected()


def _set_cookie_header(token: str) -> str:
    # Sin Secure: HTTP localhost. HttpOnly + SameSite=Strict mitigan XSS/CSRF.
    return (
        f"{CSRF_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict"
    )


class CsrfCookieMiddleware:
    """Emite la cookie CSRF si falta; expone el token en ``request.state``.

    La validación vive en :func:`enforce_csrf` (dependency del router ``/ui/``).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: MutableMapping[str, object] = scope.setdefault("state", {})
        headers = MutableHeaders(scope=scope)
        cookie_header = headers.get("cookie") or ""
        existing = _cookie_value(cookie_header, CSRF_COOKIE_NAME)

        issued = False
        if existing:
            token = existing
        else:
            token = generate_csrf_token()
            issued = True

        state["csrf_token"] = token

        async def send_wrapper(message: Message) -> None:
            if issued and message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                raw_headers.append(
                    (b"set-cookie", _set_cookie_header(token).encode("latin-1"))
                )
                message = {**message, "headers": raw_headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _cookie_value(cookie_header: str, name: str) -> str | None:
    """Parseo mínimo de Cookie (sin dependencias); ignora atributos."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None

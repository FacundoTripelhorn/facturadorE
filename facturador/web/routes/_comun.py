"""Piezas compartidas por las rutas HTML: plantillas Jinja (globals y
filtros), manejo del rechazo CSRF y helpers usados por más de un área."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ...api.csrf import (
    CSRF_JSON_DETAIL,
    CSRF_UI_MESSAGE,
    CsrfRejected,
    csrf_token_for_request,
)
from ...arca.events import load_events
from ...constants import (
    MONEDA_DISPLAY,
    InvoiceStatus,
)
from ...diagnostics import Nivel
from ...launcher.switch import (
    is_launcher_supervised,
)
from ...orden_pago import identidad_receptor
from ..numeros import formato_importe

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
STATIC_DIR = Path(__file__).parent.parent / "static"


def _ambiente_en_contexto(request: Request) -> dict[str, object]:
    """Identidad de ambiente visible en toda la UI (ADR 0001).

    Solo lenguaje de negocio (Homologación / Producción) y el código corto;
    nunca paths de perfil, certificados ni secretos. Cambio de ambiente: ``launcher``
    indica si el proceso está supervisado y puede pedir cambio por reinicio.
    """
    profile = request.app.state.profile
    return {
        "env": profile.environment,
        "env_label": profile.display_name,
        "launcher_supervised": is_launcher_supervised(),
    }


def _csrf_en_contexto(request: Request) -> dict[str, object]:
    """Token CSRF para campos ocultos de formularios."""
    return {"csrf_token": csrf_token_for_request(request)}


def _avisos_arca_en_contexto(request: Request) -> dict[str, object]:
    """Avisos de ARCA (``FEXEvents``) vigentes: los no leídos van al banner de
    toda la UI; todos, al desplegable del header."""
    profile = getattr(request.app.state, "profile", None)
    if profile is None:
        return {"avisos_arca": [], "avisos_arca_no_leidos": []}
    avisos = load_events(profile.paths.arca_events)
    return {"avisos_arca": avisos.events, "avisos_arca_no_leidos": avisos.unread}


templates = Jinja2Templates(
    directory=TEMPLATES_DIR,
    context_processors=[
        _ambiente_en_contexto,
        _csrf_en_contexto,
        _avisos_arca_en_contexto,
    ],
)


def csrf_recovery_href(request: Request) -> str:
    """GET que vuelve a pintar el formulario con un token CSRF fresco.

    ``location.reload()`` sobre el 403 del POST reenviaría el mismo body
    con el token viejo. Preferimos el ``Referer`` same-origin (si no es
    ``/ui/``) y, si falta, un mapeo estático del path del POST.
    """
    return _pagina_de_origen(request) or _recovery_from_ui_path(request.url.path)


def _pagina_de_origen(request: Request) -> str | None:
    """Página same-origin desde la que vino el POST (``Referer``), si es
    una página y no otro ``/ui/``."""
    referer = request.headers.get("referer")
    if not referer:
        return None
    parsed = urlparse(referer)
    if (
        parsed.scheme in ("", "http")
        and parsed.path
        and not parsed.path.startswith("/ui/")
        and parsed.netloc in ("", request.url.netloc)
    ):
        href = parsed.path
        if parsed.query:
            href = f"{href}?{parsed.query}"
        return href
    return None


def _recovery_from_ui_path(path: str) -> str:
    if path == "/ui/facturas":
        return "/"
    if path == "/ui/clientes":
        return "/clientes"
    if path.startswith("/ui/setup/"):
        return "/setup"
    if path.startswith("/ui/emisores") or path.startswith("/ui/configuracion/"):
        return "/configuracion"
    if path == "/ui/cambiar-ambiente":
        return "/"
    if path == "/ui/registry/catch-up":
        return "/comprobantes"
    m = re.fullmatch(r"/ui/facturas/([^/]+)/(?:authorize|descartar)", path)
    if m:
        return f"/facturas/{m.group(1)}/revisar"
    return "/"


async def csrf_rejected_handler(
    request: Request, exc: Exception
) -> HTMLResponse | JSONResponse:
    """HTML con guía de recuperación en ``/ui/``; JSON genérico fuera.

    El mensaje es fijo: nunca ecoa cookie ni el valor enviado en el form.
    La firma acepta ``Exception`` por el contrato de Starlette
    ``ExceptionHandler``; solo se registra para :class:`CsrfRejected`.
    """
    if not isinstance(exc, CsrfRejected):
        raise exc
    if request.url.path.startswith("/ui/"):
        return templates.TemplateResponse(
            request,
            "csrf_error.html",
            {
                "message": CSRF_UI_MESSAGE,
                "recovery_href": csrf_recovery_href(request),
            },
            status_code=HTTPStatus.FORBIDDEN,
        )
    return JSONResponse(
        status_code=HTTPStatus.FORBIDDEN, content={"detail": CSRF_JSON_DETAIL}
    )


STATUS_LABELS = {
    InvoiceStatus.DRAFT: "Borrador",
    InvoiceStatus.SUBMITTING: "Enviando",
    InvoiceStatus.AUTHORIZED: "Autorizada",
    InvoiceStatus.REJECTED: "Rechazada",
    InvoiceStatus.UNKNOWN: "A reconciliar",
}
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["NIVELES"] = {
    Nivel.OK: "OK",
    Nivel.AVISO: "Aviso",
    Nivel.BLOQUEA: "Bloquea",
}
# Variante del badge compartido (ok / aviso / error / neutro) por estado.
templates.env.globals["STATUS_BADGE"] = {
    InvoiceStatus.DRAFT: "neutro",
    InvoiceStatus.SUBMITTING: "neutro",
    InvoiceStatus.AUTHORIZED: "ok",
    InvoiceStatus.REJECTED: "error",
    InvoiceStatus.UNKNOWN: "aviso",
}
templates.env.globals["NIVEL_BADGE"] = {
    Nivel.OK: "ok",
    Nivel.AVISO: "aviso",
    Nivel.BLOQUEA: "error",
}
templates.env.globals["AUTHORIZED"] = InvoiceStatus.AUTHORIZED
templates.env.globals["identidad_receptor"] = identidad_receptor
templates.env.globals["DRAFT"] = InvoiceStatus.DRAFT
templates.env.globals["UNKNOWN"] = InvoiceStatus.UNKNOWN
# Presentación de moneda: DOL → USD etc.; hacia ARCA siempre viaja el código.
templates.env.filters["moneda"] = lambda code: MONEDA_DISPLAY.get(code, code)
# Importes en formato argentino (1.500,00); la API y ARCA siguen con punto.
templates.env.filters["importe"] = formato_importe
templates.env.filters["fecha"] = lambda aaaammdd: (
    f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"
)


def _pvs_display(raw: str) -> str:
    try:
        valores = json.loads(raw)
        if isinstance(valores, list):
            return ", ".join(str(v) for v in valores)
    except (ValueError, TypeError):
        pass
    return raw


templates.env.filters["pvs_display"] = _pvs_display


def _fecha_iso_a_arca(fecha: str | None) -> str | None:
    """'2026-07-05' del <input type=date> → '20260705' (AAAAMMDD)."""
    return fecha.replace("-", "") if fecha else None


# Tabs de /comprobantes: cada una agrupa los estados que le corresponden.
TAB_FILTERS: dict[str, tuple[InvoiceStatus, ...] | None] = {
    "todas": None,
    "borradores": (InvoiceStatus.DRAFT,),
    "autorizadas": (InvoiceStatus.AUTHORIZED,),
    "atencion": (
        InvoiceStatus.SUBMITTING,
        InvoiceStatus.UNKNOWN,
        InvoiceStatus.REJECTED,
    ),
}

TAB_LABELS = {
    "todas": "Todas",
    "borradores": "Borradores",
    "autorizadas": "Autorizadas",
    "atencion": "Atención",
}

AVISOS = {
    "descartado": "Borrador descartado; no se envió nada a ARCA.",
}


def _catchup_aviso(aviso: str, n: str) -> str | None:
    if aviso != "catchup":
        return None
    if n:
        return f"Registro sincronizado desde ARCA: {n} comprobantes."
    return "Registro sincronizado desde ARCA."


def _parse_puntos_venta_form(puntos_venta: str) -> list[int]:
    return [int(v) for v in puntos_venta.split(",") if v.strip()]

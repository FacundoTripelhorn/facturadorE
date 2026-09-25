"""Último aviso de ARCA (``FEXEvents``) para mostrarlo en toda la UI.

WSFEX usa ``FEXEvents`` para anunciar mantenimientos y cambios normativos
antes de que empiecen a rechazar comprobantes. Cada respuesta de WSFEX pisa
este archivo del perfil con los eventos que trajo (o con ninguno), así el
aviso desaparece solo cuando ARCA deja de mandarlo. El código ``0`` es el
"sin novedades" de ARCA y no se muestra.

Es un cache de conveniencia, como el del TA de WSAA: si no se puede leer o
escribir, la emisión sigue igual y solo se pierde el aviso.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SIN_NOVEDADES = {"", "0"}


@dataclass(frozen=True)
class ArcaEvent:
    code: str
    msg: str


@dataclass(frozen=True)
class ArcaEvents:
    events: list[ArcaEvent]
    seen_at: dt.datetime | None


def record_events(path: Path, events: list[tuple[str, str]]) -> None:
    """Guarda los eventos de la última respuesta, siempre pisando el anterior."""
    relevantes = [
        {"code": code, "msg": msg}
        for code, msg in events
        if (code or "").strip() not in _SIN_NOVEDADES
    ]
    payload = {
        "events": relevantes,
        "seen_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        logger.warning(
            "No se pudo guardar el aviso de ARCA en %s", path.name, exc_info=True
        )


def load_events(path: Path) -> ArcaEvents:
    """Eventos vigentes; vacío si no hay archivo o está corrupto."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        events = [
            ArcaEvent(code=str(e.get("code", "")), msg=str(e.get("msg", "")))
            for e in raw.get("events", [])
            if isinstance(e, dict)
        ]
        seen_raw = raw.get("seen_at")
        seen_at = dt.datetime.fromisoformat(seen_raw) if seen_raw else None
    except (OSError, ValueError, TypeError, AttributeError):
        return ArcaEvents(events=[], seen_at=None)
    return ArcaEvents(events=events, seen_at=seen_at)

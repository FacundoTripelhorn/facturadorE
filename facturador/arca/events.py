"""Avisos de ARCA (``FEXEvents``) vigentes y cuáles ya se leyeron.

WSFEX usa ``FEXEvents`` para anunciar mantenimientos y cambios normativos
antes de que empiecen a rechazar comprobantes. Cada respuesta de WSFEX pisa
este archivo del perfil con los eventos que trajo (o con ninguno), así el
aviso desaparece solo cuando ARCA deja de mandarlo. El código ``0`` es el
"sin novedades" de ARCA y no se muestra.

Cada aviso vigente guarda cuándo se vio por primera vez y si ya se marcó
como leído. Un aviso se identifica por código + texto: si ARCA cambia el
texto, vuelve a ser nuevo. Cuando ARCA deja de mandarlo se descarta junto
con su estado de leído, así que si vuelve cuenta como nuevo.

Es un cache de conveniencia, como el del TA de WSAA: si no se puede leer o
escribir, la emisión sigue igual y solo se pierde el aviso (o su estado de
leído: ante la duda, todo cuenta como no leído).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SIN_NOVEDADES = {"", "0"}

# Registrar eventos (tras cada llamada a WSFEX) y marcar leído (desde la UI)
# leen y reescriben el mismo archivo: sin el lock, uno pisaría al otro.
_LOCK = threading.Lock()


@dataclass(frozen=True)
class ArcaEvent:
    code: str
    msg: str
    first_seen: dt.datetime | None = None
    read: bool = False

    @property
    def id(self) -> str:
        """Identificador estable para la UI: código + texto."""
        return _event_id(self.code, self.msg)


@dataclass(frozen=True)
class ArcaEvents:
    events: list[ArcaEvent]
    seen_at: dt.datetime | None

    @property
    def unread(self) -> list[ArcaEvent]:
        return [e for e in self.events if not e.read]


def _event_id(code: str, msg: str) -> str:
    return hashlib.sha256(f"{code}\0{msg}".encode()).hexdigest()[:16]


def record_events(path: Path, events: list[tuple[str, str]]) -> None:
    """Guarda los eventos de la última respuesta, siempre pisando el anterior.

    Un aviso que ya estaba conserva su fecha de primera vista y su estado de
    leído; los que ARCA ya no manda se descartan.
    """
    ahora = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with _LOCK:
        previos = {e["id"]: e for e in _leer(path)[0]}
        vigentes: list[dict[str, Any]] = []
        for code, msg in events:
            code, msg = code or "", msg or ""
            if code.strip() in _SIN_NOVEDADES:
                continue
            event_id = _event_id(code, msg)
            if any(e["id"] == event_id for e in vigentes):
                continue
            previo = previos.get(event_id)
            vigentes.append({
                "id": event_id,
                "code": code,
                "msg": msg,
                "first_seen": (previo or {}).get("first_seen") or ahora,
                "read": bool(previo and previo["read"]),
            })
        _write(path, vigentes, ahora)


def mark_read(path: Path, event_id: str) -> bool:
    """Marca como leído un aviso vigente. False si ya no está vigente o no
    se pudo guardar."""
    with _LOCK:
        entries, seen_at = _leer(path)
        encontrado = False
        for entry in entries:
            if entry["id"] == event_id and not entry["read"]:
                entry["read"] = True
                encontrado = True
        if encontrado:
            return _write(path, entries, seen_at)
        return any(e["id"] == event_id for e in entries)


def load_events(path: Path) -> ArcaEvents:
    """Eventos vigentes; vacío si no hay archivo o está corrupto."""
    entries, seen_raw = _leer(path)
    seen_at = _fecha(seen_raw)
    events = [
        ArcaEvent(
            code=e["code"],
            msg=e["msg"],
            first_seen=_fecha(e["first_seen"]) or seen_at,
            read=e["read"],
        )
        for e in entries
    ]
    return ArcaEvents(events=events, seen_at=seen_at)


def _leer(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Entradas bien formadas (con su ``id``) y ``seen_at`` del archivo.

    Archivo ausente o corrupto: sin entradas. Una entrada rara se ignora.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        return [], None
    seen_at = raw.get("seen_at")
    entries = []
    for e in raw["events"]:
        if not isinstance(e, dict):
            continue
        code, msg = str(e.get("code", "")), str(e.get("msg", ""))
        if code.strip() in _SIN_NOVEDADES:
            continue
        first_seen = e.get("first_seen")
        entries.append({
            "id": _event_id(code, msg),
            "code": code,
            "msg": msg,
            "first_seen": first_seen if isinstance(first_seen, str) else None,
            "read": e.get("read") is True,
        })
    return entries, seen_at if isinstance(seen_at, str) else None


def _fecha(valor: object) -> dt.datetime | None:
    """Fecha guardada; None si no es una fecha razonable. Un año extremo
    (archivo editado a mano) haría fallar el pasaje a hora local al mostrarla."""
    if not isinstance(valor, str):
        return None
    try:
        fecha = dt.datetime.fromisoformat(valor)
    except ValueError:
        return None
    return fecha if 2000 <= fecha.year <= 9000 else None


def _write(path: Path, entries: list[dict[str, Any]], seen_at: str | None) -> bool:
    payload = {
        "events": [
            {k: e[k] for k in ("code", "msg", "first_seen", "read")}
            for e in entries
        ],
        "seen_at": seen_at,
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
        return False
    return True

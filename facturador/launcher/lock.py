"""Lock de perfil del launcher (FAC-30, ADR 0001).

Evita dos launchers (y por tanto dos backends) sobre el mismo perfil SQLite.
El lock es por perfil: Homologación y Producción pueden coexistir.

Comportamiento:
- Adquisición no bloqueante con ``fcntl.flock`` / ``msvcrt.locking``.
- El SO libera el flock al morir el proceso → locks stale no bloquean el
  arranque (se reescribe el metadata al adquirir).
- El archivo ``launcher.lock`` **no se borra** al soltar: un unlink tras el
  unlock abre una ventana donde otro proceso puede tomar el flock sobre un
  inode que luego desaparece, y un tercero crea un archivo nuevo en el mismo
  path (dos dueños aparentes del perfil).
- Si el lock está tomado, el segundo launcher lee puerto/ambiente y puede
  reutilizar la sesión sana (abrir el browser) o fallar con mensaje claro.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

_LOCK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LockHolder:
    """Quién tiene el lock de un perfil (metadata en el archivo)."""

    pid: int
    port: int
    environment: str

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"


class ProfileLockHeld(RuntimeError):
    """Otro launcher ya tiene el perfil; ``holder`` describe la sesión."""

    def __init__(self, message: str, *, holder: LockHolder) -> None:
        super().__init__(message)
        self.holder = holder


class ProfileLockError(RuntimeError):
    """Fallo al adquirir/liberar el lock de perfil."""


@dataclass
class ProfileLock:
    """Lock exclusivo sobre ``path`` (típicamente ``ProfilePaths.launcher_lock``)."""

    path: Path
    _fh: TextIO | None = None
    _holder: LockHolder | None = None

    @property
    def is_held(self) -> bool:
        return self._fh is not None

    @property
    def holder(self) -> LockHolder | None:
        return self._holder

    def acquire(self, *, port: int, environment: str) -> LockHolder:
        """Toma el lock o lanza ``ProfileLockHeld`` si otro proceso lo tiene.

        Locks stale (proceso muerto): el flock ya no está tomado; se adquiere
        y se reescribe el metadata. No bloquea el arranque de forma permanente.
        """
        if self.is_held:
            assert self._holder is not None
            return self._holder
        if port < 1 or port > 65535:
            raise ProfileLockError(f"Puerto inválido para el lock: {port}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" crea el archivo si no existe y permite leer metadata ajeno
        # antes de fallar; el flock exclusivo es la autoridad.
        fh = self.path.open("a+", encoding="utf-8")
        try:
            if not _try_lock(fh):
                holder = read_lock_holder(self.path) or LockHolder(
                    pid=0,
                    port=port,
                    environment=environment,
                )
                fh.close()
                raise ProfileLockHeld(
                    _held_message(holder),
                    holder=holder,
                )
            holder = LockHolder(
                pid=os.getpid(),
                port=port,
                environment=environment,
            )
            _write_holder(fh, holder)
            self._fh = fh
            self._holder = holder
            return holder
        except Exception:
            if self._fh is not fh:
                fh.close()
            raise

    def release(self) -> None:
        """Suelta el flock; deja el archivo en disco (el flock es la autoridad).

        No hacemos ``unlink``: entre unlock y unlink otro proceso puede adquirir
        el flock sobre el mismo inode, y el unlink dejaría ese flock sobre un
        archivo huérfano mientras un tercero crea un ``launcher.lock`` nuevo.
        """
        fh = self._fh
        self._fh = None
        self._holder = None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()

    def __enter__(self) -> ProfileLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_lock_holder(path: Path) -> LockHolder | None:
    """Lee el metadata del lock sin tomarlo. ``None`` si falta o es inválido."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _holder_from_payload(payload)


def is_process_alive(pid: int) -> bool:
    """True si ``pid`` existe (best-effort; no implica que sea nuestro launcher)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Existe pero no nos deja señalarlo: contarlo como vivo.
        return True
    except OSError:
        return False
    return True


def _held_message(holder: LockHolder) -> str:
    env_label = {
        "homo": "Homologación",
        "prod": "Producción",
    }.get(holder.environment, holder.environment)
    return (
        f"FacturadorE ({env_label}) ya está en marcha "
        f"en {holder.base_url}/."
    )


def _holder_from_payload(payload: Any) -> LockHolder | None:
    if not isinstance(payload, dict):
        return None
    try:
        pid = int(payload["pid"])
        port = int(payload["port"])
        environment = str(payload["environment"])
    except (KeyError, TypeError, ValueError):
        return None
    if port < 1 or port > 65535 or not environment:
        return None
    return LockHolder(pid=pid, port=port, environment=environment)


def _write_holder(fh: TextIO, holder: LockHolder) -> None:
    payload = {
        "v": _LOCK_SCHEMA_VERSION,
        "pid": holder.pid,
        "port": holder.port,
        "environment": holder.environment,
    }
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps(payload, ensure_ascii=True))
    fh.write("\n")
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:
        pass


def _try_lock(fh: TextIO) -> bool:
    """Lock exclusivo no bloqueante. True si se tomó."""
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            # msvcrt.locking exige al menos un byte en el archivo.
            if fh.read(1) == "":
                fh.write("\0")
                fh.flush()
                fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return False


def _unlock(fh: TextIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            return
        return
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        return

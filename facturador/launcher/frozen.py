"""Entrada del ejecutable empaquetado (PyInstaller) de Windows.

Un solo ``FacturadorE.exe`` hace de launcher y, con ``--backend``, de
backend supervisado: en el build no hay ``python -m facturador`` que el
supervisor pueda lanzar, así que se relanza el mismo exe en ese modo.

El exe es una app de ventana (sin consola): ``sys.stdout``/``sys.stderr``
llegan en ``None``. Acá se redirigen a ``launcher.log`` en el app-data para
que los prints y el logging del launcher no se pierdan ni fallen. El hijo
backend escribe a la pipe que drena el supervisor y a su log de perfil.

``age.exe`` / ``age-keygen.exe`` viajan dentro del bundle (``bin/``); se
agregan al ``PATH`` del proceso (y por herencia al del backend) para que
el backup cifrado funcione sin instalar nada.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

BACKEND_FLAG = "--backend"
LAUNCHER_LOG = "launcher.log"
ICON_NAME = "facturadore.ico"
# La escribe build.ps1: el tag del release o dev-<sha>.
VERSION_NAME = "version.txt"
# El log del launcher se recorta al abrir si pasó este tamaño.
_LOG_MAX_BYTES = 1_000_000


def stdin_is_tty() -> bool:
    """True si hay una terminal para un menú de texto. En el exe de ventana
    ``sys.stdin`` es ``None``."""
    stdin = sys.stdin
    try:
        return bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError):
        return False


def has_interactive_terminal() -> bool:
    """True si hay terminal para leer un error. El exe de ventana no tiene
    stdin/stderr (``None``) o los tiene redirigidos a un archivo."""
    stderr = sys.stderr
    try:
        return stdin_is_tty() and bool(stderr is not None and stderr.isatty())
    except (AttributeError, ValueError):
        return False


def is_frozen() -> bool:
    """True dentro del ejecutable de PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path | None:
    """Carpeta de recursos del bundle (``_internal``); None fuera del exe."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if is_frozen() and meipass else None


def app_icon_path() -> Path | None:
    """Ícono de la app para las ventanas tkinter; None si no hay."""
    base = bundle_dir()
    if base is None:
        return None
    icon = base / ICON_NAME
    return icon if icon.is_file() else None


def build_version() -> str | None:
    """Versión con la que se armó el exe; None fuera del exe o si falta."""
    base = bundle_dir()
    if base is None:
        return None
    try:
        version = (base / VERSION_NAME).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return version or None


def set_window_icon(root: object) -> None:
    """Ícono de la app en una ventana tkinter (y sus diálogos); no-op si no
    hay ícono o tkinter lo rechaza."""
    icon = app_icon_path()
    if icon is None:
        return
    try:
        root.iconbitmap(default=str(icon))  # type: ignore[attr-defined]
    except Exception:
        return


def add_bundled_tools_to_path(environ: dict[str, str] | None = None) -> None:
    """Antepone ``<bundle>/bin`` al PATH (age.exe, age-keygen.exe)."""
    base = bundle_dir()
    if base is None:
        return
    tools = base / "bin"
    if not tools.is_dir():
        return
    env = os.environ if environ is None else environ
    current = env.get("PATH", "")
    if str(tools) not in current.split(os.pathsep):
        env["PATH"] = os.pathsep.join(p for p in (str(tools), current) if p)


def redirect_missing_streams(log_dir: Path) -> Path | None:
    """Si stdout/stderr son None (exe de ventana), los manda a un archivo.

    Devuelve la ruta del log si redirigió. Si no se puede abrir el
    archivo, usa ``os.devnull``: mejor perder la salida que fallar.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = log_dir / LAUNCHER_LOG
    stream: TextIO
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _recortar(path)
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
        path = Path(os.devnull)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return path


def _recortar(path: Path) -> None:
    """Empieza el log de cero si pasó el tope. En Windows no se puede
    borrar mientras otro launcher lo tiene abierto: ahí se sigue agregando."""
    try:
        if path.is_file() and path.stat().st_size > _LOG_MAX_BYTES:
            path.unlink()
    except OSError:
        return


def _log_dir() -> Path:
    from ..profile import ProfileError, resolve_app_data_root

    try:
        return resolve_app_data_root()
    except ProfileError:
        return Path.home()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    redirect_missing_streams(_log_dir())
    add_bundled_tools_to_path()
    if args[:1] == [BACKEND_FLAG]:
        from ..__main__ import main as backend_main

        backend_main()
        return 0
    from .__main__ import main as launcher_main

    return launcher_main(args)

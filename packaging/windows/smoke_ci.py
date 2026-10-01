"""Smoke headless del exe empaquetado (CI en Windows, sin pantalla).

Cubre lo que no necesita ventana: el exe arranca el backend de
Homologación en un app-data temporal, ``/health`` responde, un segundo
launch reusa la sesión sin crear otro backend, el launcher deja su log
(el exe no tiene consola), age viaja en el bundle y un puerto ocupado da el
mensaje claro (la salida del backend llega a la pipe del supervisor). El
chooser, la ventana, el cambio de ambiente y el cierre se prueban a mano:
docs/windows-smoke-test.md.

Uso: ``python packaging/windows/smoke_ci.py packaging/windows/dist/FacturadorE``
"""

from __future__ import annotations

import csv
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8411
PORT_OCUPADO = 8412
EXE_NAME = "FacturadorE.exe"


def _health(port: int, timeout: float) -> dict[str, object]:
    fin = time.monotonic() + timeout
    ultimo: Exception | None = None
    while time.monotonic() < fin:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            ultimo = exc
            time.sleep(1)
    raise AssertionError(f"/health no respondió en {timeout:.0f} s: {ultimo}")


def _procesos(nombre: str) -> int:
    salida = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {nombre}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sum(1 for fila in csv.reader(io.StringIO(salida)) if fila[:1] == [nombre])


def _puerto_ocupado(exe: Path) -> None:
    """Otro programa en el puerto: el launcher sale con el mensaje claro.

    Prueba el camino real del exe: el backend (mismo exe, modo --backend,
    sin consola) escribe el error de bind en la pipe que lee el supervisor.
    """
    ocupante = socket.socket()
    ocupante.bind(("127.0.0.1", PORT_OCUPADO))
    ocupante.listen()
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = dict(
                os.environ, FACTURADOR_APP_DATA=tmp, FACTURADOR_NO_DIALOGS="1"
            )
            argv = [
                str(exe), "--env", "homo", "--no-browser",
                "--port", str(PORT_OCUPADO), "--timeout", "60",
            ]
            resultado = subprocess.run(argv, env=env, timeout=120)
            assert resultado.returncode != 0, resultado.returncode
            texto = (Path(tmp) / "launcher.log").read_text(encoding="utf-8")
            esperado = f"el puerto {PORT_OCUPADO} ya lo está usando otro programa"
            assert esperado in texto, texto
    finally:
        ocupante.close()
    time.sleep(1)
    assert _procesos(EXE_NAME) == 0, "quedó un proceso tras el puerto ocupado"
    print("puerto ocupado: mensaje claro")


def main(carpeta: Path) -> int:
    exe = carpeta / EXE_NAME
    assert exe.is_file(), f"no está {exe}"
    # El chooser y los diálogos usan tkinter: si PyInstaller no encontró
    # Tcl/Tk lo excluye en silencio y el exe abre sin selector.
    for datos in ("_tcl_data", "_tk_data"):
        assert (carpeta / "_internal" / datos).is_dir(), f"falta {datos} (tkinter)"
    for herramienta in ("age.exe", "age-keygen.exe"):
        ruta = carpeta / "_internal" / "bin" / herramienta
        version = subprocess.run(
            [str(ruta), "--version"], capture_output=True, text=True, check=True
        )
        print(f"{herramienta}: {version.stdout.strip()}")

    _puerto_ocupado(exe)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = dict(os.environ, FACTURADOR_APP_DATA=tmp, FACTURADOR_NO_DIALOGS="1")
        argv = [str(exe), "--env", "homo", "--no-browser", "--port", str(PORT)]
        launcher = subprocess.Popen(argv, env=env)
        try:
            salud = _health(PORT, timeout=120)
            assert salud.get("environment") == "homo", salud
            print(f"/health: {salud}")

            # Launcher + backend (el mismo exe en modo --backend).
            assert _procesos(EXE_NAME) == 2, _procesos(EXE_NAME)

            segundo = subprocess.run(argv, env=env, timeout=60)
            assert segundo.returncode == 0, segundo.returncode
            assert _procesos(EXE_NAME) == 2, "el segundo launch creó otro backend"
            print("segundo launch: reusó la sesión")

            log = Path(tmp) / "launcher.log"
            texto = log.read_text(encoding="utf-8")
            assert "listo en http://127.0.0.1" in texto, texto
            assert "ya estaba en marcha" in texto, texto
            print("launcher.log: ok")
        finally:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(launcher.pid)],
                capture_output=True,
            )
            launcher.wait(timeout=30)
        # Sin huérfanos: el backend cae con el árbol del launcher.
        time.sleep(2)
        assert _procesos(EXE_NAME) == 0, "quedó un backend huérfano"
    print("smoke OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))

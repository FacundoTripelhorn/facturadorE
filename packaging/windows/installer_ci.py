"""Prueba del instalador en CI (Windows, sin pantalla).

Simula la descarga desde el navegador y recorre el ciclo completo:

1. Le escribe la marca de internet (``Zone.Identifier``) al instalador.
2. Instala en silencio en la ubicación por defecto.
3. Afirma que ningún archivo instalado conserva la marca: con ella,
   .NET no carga los ensamblados de pythonnet y la ventana de la app no abre.
4. Corre el smoke headless (``smoke_ci.py``) sobre la carpeta instalada.
5. Instala encima (actualización) y afirma que ``_internal`` se reemplazó
   completo.
6. Desinstala en silencio y afirma que los datos de
   ``%LOCALAPPDATA%\\FacturadorE`` quedaron intactos.

Instala y desinstala de verdad en el usuario actual: está pensado para el
runner de CI. Si FacturadorE ya está instalado, no hace nada y falla.

Uso: ``python packaging/windows/installer_ci.py <FacturadorE-Setup-*.exe>``
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

import smoke_ci

APP = "FacturadorE"
# AppId de FacturadorE.iss: la clave de desinstalación es "<AppId>_is1".
UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{0BE8B74A-F02D-4187-ACB8-CD7795DC2F84}_is1"
)
SILENCIO = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
MARCA = "[ZoneTransfer]\r\nZoneId=3\r\n"


def _marca(archivo: Path) -> str | None:
    """Contenido del stream ``Zone.Identifier`` del archivo; None si no hay."""
    try:
        with open(f"{archivo}:Zone.Identifier", encoding="ascii", newline="") as f:
            return f.read()
    except OSError:
        return None


def _foto(carpeta: Path) -> dict[str, tuple[int, int]]:
    """Archivos de la carpeta con tamaño y fecha de modificación."""
    foto = {}
    for archivo in sorted(p for p in carpeta.rglob("*") if p.is_file()):
        st = archivo.stat()
        foto[str(archivo.relative_to(carpeta))] = (st.st_size, st.st_mtime_ns)
    return foto


def _en_aplicaciones() -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY))
    except OSError:
        return False
    return True


def _instalar(instalador: Path, log: Path) -> None:
    subprocess.run(
        [str(instalador), *SILENCIO, "/SP-", f"/LOG={log}"], check=True, timeout=600
    )


def main(instalador: Path) -> int:
    assert instalador.is_file(), f"no está {instalador}"
    local = Path(os.environ["LOCALAPPDATA"])
    destino = local / "Programs" / APP
    datos = local / APP
    acceso = (
        Path(os.environ["APPDATA"])
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP}.lnk"
    )
    assert not destino.exists(), f"{destino} ya existe: no se pisa una instalación"

    # Datos de un usuario que ya usó la app: tienen que sobrevivir a todo.
    if not datos.exists():
        (datos / "homo" / "secrets").mkdir(parents=True)
        (datos / "homo" / "secrets" / "cert.key").write_text("centinela\n")
        (datos / "launcher.log").write_text("centinela\n")
    antes = _foto(datos)
    assert antes, f"{datos} está vacío"

    with open(f"{instalador}:Zone.Identifier", "w", encoding="ascii", newline="") as f:
        f.write(MARCA)
    assert _marca(instalador) == MARCA, "no se pudo marcar el instalador"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _instalar(instalador, Path(tmp) / "instalar.log")
        instalados = [p for p in destino.rglob("*") if p.is_file()]
        assert (destino / smoke_ci.EXE_NAME) in instalados, instalados[:20]
        marcados = [str(p) for p in instalados if _marca(p) is not None]
        assert not marcados, f"conservan la marca de internet: {marcados[:20]}"
        print(f"instalado: {len(instalados)} archivos, ninguno con la marca")
        assert acceso.is_file(), f"falta el acceso directo {acceso}"
        assert _en_aplicaciones(), "falta la entrada en Aplicaciones"

        smoke_ci.main(destino)

        viejo = destino / "_internal" / "de_la_version_anterior.dll"
        viejo.write_bytes(b"")
        _instalar(instalador, Path(tmp) / "actualizar.log")
        assert not viejo.exists(), "la actualización dejó archivos viejos"
        assert (destino / smoke_ci.EXE_NAME).is_file()
        print("actualización: _internal reemplazado")

    desinstalador = destino / "unins000.exe"
    assert desinstalador.is_file(), f"no está {desinstalador}"
    subprocess.run([str(desinstalador), *SILENCIO], check=True, timeout=300)
    # El desinstalador termina de borrarse a sí mismo desde una copia temporal.
    fin = time.monotonic() + 60
    while destino.exists() and time.monotonic() < fin:
        time.sleep(1)
    assert not destino.exists(), f"quedó {destino}: {list(destino.rglob('*'))[:20]}"
    assert not acceso.exists(), "quedó el acceso directo"
    assert not _en_aplicaciones(), "quedó la entrada en Aplicaciones"
    assert _foto(datos) == antes, "la desinstalación tocó los datos"
    print("desinstalación: programa borrado, datos intactos")
    print("instalador OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))

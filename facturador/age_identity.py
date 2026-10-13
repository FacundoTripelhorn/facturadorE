"""Identidad ``age`` de esta máquina (una sola para los dos ambientes).

Vive en el app-data, fuera de los perfiles: ``<app-data>/age/key.txt``
(``%LOCALAPPDATA%\\FacturadorE\\age\\key.txt`` en Windows). Con ella esta
máquina puede abrir el seed de cualquier perfil que la tenga en su
``recipients.txt``.

La parte privada nunca sale del archivo: este módulo no la lee. La crea
``age-keygen -o`` y la clave pública se obtiene con ``age-keygen -y``; Python
solo ve la clave pública. No entra al seed, no sube a S3, no se loguea ni se
muestra.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .profile import ProfilePaths, machine_identity_path

KEYGEN_BIN = "age-keygen"

# Clave pública X25519 nativa: "age1" + 58 caracteres bech32.
_PUBLIC_KEY = re.compile(r"age1[02-9ac-hj-np-z]{58}")


class AgeIdentityError(RuntimeError):
    """No se pudo crear o leer la identidad. El mensaje nunca trae la clave
    privada ni la salida de ``age-keygen``."""


def is_public_key(value: str) -> bool:
    """¿Es una clave pública ``age1…`` nativa (X25519)?"""
    return _PUBLIC_KEY.fullmatch(value.strip().lower()) is not None


def _keygen(keygen_bin: str) -> str:
    found = shutil.which(keygen_bin)
    if found is None:
        raise AgeIdentityError(
            "No se encontró age-keygen. Viene con el instalador de "
            "FacturadorE; fuera del instalador hay que instalar age."
        )
    return found


class MachineIdentity:
    """Identidad ``age`` de la máquina en un path fijo."""

    def __init__(self, path: Path, *, keygen_bin: str = KEYGEN_BIN) -> None:
        self._path = path
        self._keygen_bin = keygen_bin

    @classmethod
    def for_profile(cls, paths: ProfilePaths) -> MachineIdentity:
        return cls(machine_identity_path(paths))

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def public_key(self) -> str | None:
        """La clave pública, o None si la máquina todavía no tiene identidad."""
        if not self.exists():
            return None
        try:
            completed = subprocess.run(
                [_keygen(self._keygen_bin), "-y", str(self._path)],
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError) as exc:
            raise AgeIdentityError(
                "No se pudo leer la identidad de esta máquina."
            ) from exc
        lines = completed.stdout.decode("ascii", errors="replace").split()
        if len(lines) != 1 or not is_public_key(lines[0]):
            raise AgeIdentityError(
                "La identidad de esta máquina no tiene el formato esperado."
            )
        return lines[0].lower()

    def create(self) -> str:
        """Crea la identidad si no existe y devuelve la clave pública.

        Si ya existe no se pisa (ni aunque otro proceso la cree a la vez):
        se genera en un temporal del mismo directorio y se enlaza al destino
        con ``os.link``, que falla si el destino ya está.
        """
        keygen = _keygen(self._keygen_bin)
        folder = self._path.parent
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            folder.chmod(0o700)
        if not self.exists():
            with tempfile.TemporaryDirectory(dir=folder) as tmp:
                tmp_key = Path(tmp) / self._path.name
                try:
                    # -o escribe la identidad con permisos 600; la clave
                    # pública sale por stderr, que se descarta.
                    subprocess.run(
                        [keygen, "-o", str(tmp_key)],
                        check=True,
                        capture_output=True,
                        stdin=subprocess.DEVNULL,
                        timeout=30,
                    )
                except (subprocess.CalledProcessError,
                        subprocess.TimeoutExpired, OSError) as exc:
                    raise AgeIdentityError(
                        "age-keygen no pudo crear la identidad de esta máquina."
                    ) from exc
                if sys.platform != "win32":
                    tmp_key.chmod(0o600)
                try:
                    os.link(tmp_key, self._path)
                except FileExistsError:
                    pass  # otro proceso la creó primero: queda la suya
                except OSError as exc:
                    raise AgeIdentityError(
                        "No se pudo guardar la identidad de esta máquina."
                    ) from exc
        key = self.public_key()
        if key is None:
            raise AgeIdentityError(
                "No se pudo guardar la identidad de esta máquina."
            )
        return key

    def __repr__(self) -> str:
        return "MachineIdentity(...)"

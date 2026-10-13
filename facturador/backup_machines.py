"""Máquinas con acceso al backup: lectura y edición de ``recipients.txt``.

Cada máquina es una clave pública ``age1…``; su nombre es la línea
``# nombre`` que está justo arriba de la clave (si no hay, "Sin nombre").
Agregar y quitar devuelven el texto nuevo con el resto del archivo tal
cual (comentarios incluidos); quien llama lo escribe con
``SeedBackupCoordinator.replace_recipients``, que dispara un solo backup.

La lista local es la que manda: el upload pisa la de S3 con esta.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

from .age_identity import is_public_key
from .seed_backup import parse_recipients

NOMBRE_MAX = 80


class MaquinasError(ValueError):
    """Alta o baja inválida; el mensaje es para mostrar al usuario."""


@dataclass(frozen=True)
class Maquina:
    clave: str
    nombre: str | None

    @property
    def clave_corta(self) -> str:
        return f"{self.clave[:12]}…{self.clave[-6:]}"


def _clave_de_linea(linea: str) -> str | None:
    """La clave de una línea de recipients (sin comentario al final)."""
    texto = linea.strip()
    if not texto or texto.startswith("#"):
        return None
    return texto.split("#", 1)[0].strip() or None


def _nombre_de_comentario(linea: str) -> str | None:
    texto = linea.strip()
    if not texto.startswith("#"):
        return None
    return limpiar_nombre(texto.lstrip("#")) or None


def limpiar_nombre(nombre: str) -> str:
    """Una sola línea, sin espacios de más, con tope de largo."""
    return " ".join(nombre.split())[:NOMBRE_MAX]


def nombre_del_equipo() -> str:
    return limpiar_nombre(platform.node())


def listar_maquinas(texto: str) -> list[Maquina]:
    """Las claves del archivo, en orden y sin repetir, con su nombre.

    Valida igual que el backup (``parse_recipients``): un archivo que el
    backup no puede usar levanta ``SeedBackupError``.
    """
    parse_recipients(texto)
    lineas = texto.splitlines()
    maquinas: list[Maquina] = []
    vistas: set[str] = set()
    for i, linea in enumerate(lineas):
        clave = _clave_de_linea(linea)
        if clave is None or clave in vistas:
            continue
        vistas.add(clave)
        nombre = _nombre_de_comentario(lineas[i - 1]) if i > 0 else None
        if nombre is None and "#" in linea:
            nombre = limpiar_nombre(linea.split("#", 1)[1]) or None
        maquinas.append(Maquina(clave=clave, nombre=nombre))
    return maquinas


def agregar_maquina(texto: str, *, nombre: str, clave: str) -> str:
    """Texto nuevo con la clave al final y su ``# nombre`` arriba."""
    clave = clave.strip()
    if not is_public_key(clave):
        raise MaquinasError(
            "La clave tiene que ser una clave pública age: empieza con "
            "age1 y tiene 62 caracteres."
        )
    clave = clave.lower()
    if clave in parse_recipients(texto):
        raise MaquinasError("Esa clave ya está en la lista.")
    nombre = limpiar_nombre(nombre)
    base = texto if texto == "" or texto.endswith("\n") else texto + "\n"
    entrada = f"# {nombre}\n{clave}\n" if nombre else f"{clave}\n"
    return base + entrada


def quitar_maquina(texto: str, *, clave: str) -> str:
    """Texto nuevo sin la clave ni su ``# nombre``; el resto queda igual."""
    clave = clave.strip()
    claves = parse_recipients(texto)
    if clave not in claves:
        raise MaquinasError("Esa máquina ya no está en la lista.")
    if len(claves) == 1:
        raise MaquinasError(
            "No se puede quitar la última máquina: sin ninguna clave el "
            "backup no se puede cifrar."
        )
    lineas = texto.splitlines(keepends=True)
    quitar: set[int] = set()
    for i, linea in enumerate(lineas):
        if _clave_de_linea(linea) != clave:
            continue
        quitar.add(i)
        if i > 0 and _nombre_de_comentario(lineas[i - 1]) is not None:
            quitar.add(i - 1)
    return "".join(linea for i, linea in enumerate(lineas) if i not in quitar)

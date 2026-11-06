"""Orden de pago del banco: une PDFs para ingresar al país un cobro.

El banco pide dos PDFs por orden: las Facturas E que se liquidan y los
invoices de la cuenta recaudadora (la plataforma por la que paga el
cliente). Este módulo arma los nombres de archivo y une PDFs con pypdf
(fpdf2 no puede abrir PDFs existentes). Todo pasa en memoria: los invoices
subidos no se escriben a disco ni a la base; se unen, se devuelven y se
descartan.

Los PDFs externos son hostiles: solo se aceptan bytes que empiecen con
``%PDF``; uno encriptado o que pypdf no puede leer da un error que nombra
el archivo. Nunca se lee el contenido para sacar números u otros datos.
"""

from __future__ import annotations

import io
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

# Tope por archivo subido y por pedido: un invoice de la recaudadora pesa
# decenas de KB. El parser multipart mantiene en memoria hasta este tamaño
# (ver ``facturador.api.app``), así que lo subido no toca el disco.
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 30 * 1024 * 1024

_DIGITOS = re.compile(r"[0-9]+")
# Lo que puede ir en el nombre del archivo como número de un invoice.
_NUMERO_VALIDO = re.compile(r"[0-9A-Za-z-]{1,40}")


class OrdenPagoError(ValueError):
    """Pedido inválido para armar la orden de pago (mensaje para el usuario)."""


@dataclass(frozen=True)
class PdfSubido:
    """Un PDF de la recaudadora tal como llegó: nombre original y bytes."""

    nombre: str
    contenido: bytes


def _normalizar(texto: object) -> str:
    return " ".join(str(texto or "").split()).casefold()


def identidad_receptor(factura: sqlite3.Row) -> str:
    """Receptor según el snapshot de la factura, que ARCA también guarda.

    Coincide entre una factura emitida acá y la misma reconstruida desde
    ARCA (que no tiene ``client_id``): CUIT país + ID impositivo, o el
    nombre si el cliente no tiene ID impositivo.
    """
    impositivo = _normalizar(factura["id_impositivo"])
    if impositivo:
        return f"fiscal:{factura['cuit_pais_cliente']}:{impositivo}"
    return f"nombre:{factura['cuit_pais_cliente']}:{_normalizar(factura['cliente'])}"


def mismo_cliente(facturas: Sequence[sqlite3.Row]) -> bool:
    """True si todas las facturas son del mismo cliente.

    Con ``client_id`` en todas, decide el id (aunque el cliente se haya
    renombrado). Las reconstruidas desde ARCA tras un restore no tienen
    ``client_id``: si hay alguna, además de no haber dos ids distintos,
    todas tienen que compartir el receptor del snapshot.
    """
    ids = {f["client_id"] or None for f in facturas}
    if None not in ids:
        return len(ids) <= 1
    return len(ids - {None}) <= 1 and (
        len({identidad_receptor(f) for f in facturas}) <= 1
    )


def numero_desde_nombre(nombre_archivo: str) -> str:
    """Primer grupo de dígitos del nombre, sin la extensión ("" si no hay).

    ``Invoice-42.pdf`` → ``42``; ``cobro_2026_0042.pdf`` → ``2026``. Es solo
    una precarga: el usuario lo corrige en la pantalla.
    """
    base = nombre_archivo.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    m = _DIGITOS.search(stem)
    return m.group(0) if m else ""


def normalizar_cuenta(nombre: str) -> str:
    """Nombre de la cuenta apto para un nombre de archivo.

    Minúsculas, sin acentos, sin espacios ni caracteres raros:
    ``"Cobros Sur S.A."`` → ``"cobrossursa"``, ``"Recaudadora"`` → ``"recaudadora"``.
    """
    sin_acentos = (
        unicodedata.normalize("NFKD", nombre)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]", "", sin_acentos.lower())


def validar_numero(numero: str, nombre_archivo: str) -> str:
    """Número de un invoice de la recaudadora, obligatorio y apto para el
    nombre del archivo."""
    limpio = numero.strip()
    if not limpio:
        raise OrdenPagoError(f"Falta el número del invoice {nombre_archivo}.")
    if not _NUMERO_VALIDO.fullmatch(limpio):
        raise OrdenPagoError(
            f"El número del invoice {nombre_archivo} solo puede tener "
            "letras, dígitos y guiones."
        )
    return limpio


def nombre_facturas_e(cbte_nros: Sequence[int]) -> str:
    """``e_invoices_12_13_14.pdf``: números sin punto de venta, ascendentes."""
    return f"e_invoices_{'_'.join(str(n) for n in sorted(cbte_nros))}.pdf"


def nombre_recaudadora(cuenta: str, numeros: Sequence[str]) -> str:
    """``recaudadora_invoices_42_43_44.pdf``: números en el orden de subida."""
    normalizada = normalizar_cuenta(cuenta)
    if not normalizada:
        raise OrdenPagoError("Falta el nombre de la cuenta recaudadora.")
    return f"{normalizada}_invoices_{'_'.join(numeros)}.pdf"


def validar_pdf_subido(archivo: PdfSubido) -> None:
    """Rechaza lo que no es un PDF legible antes de unir nada."""
    if not archivo.contenido.startswith(b"%PDF"):
        raise OrdenPagoError(f"{archivo.nombre} no es un PDF.")
    if len(archivo.contenido) > MAX_PDF_BYTES:
        raise OrdenPagoError(
            f"{archivo.nombre} pesa más de {MAX_PDF_BYTES // (1024 * 1024)} MB."
        )
    _leer(archivo.nombre, archivo.contenido)


def _leer(nombre: str, contenido: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(contenido))
        if reader.is_encrypted:
            raise OrdenPagoError(
                f"{nombre} está protegido con contraseña; "
                "subí una copia sin protección."
            )
        if len(reader.pages) == 0:
            raise OrdenPagoError(f"{nombre} no tiene páginas.")
    except OrdenPagoError:
        raise
    except (PdfReadError, ValueError, TypeError, KeyError, OSError) as exc:
        raise OrdenPagoError(
            f"No se pudo leer {nombre}: el PDF está dañado o no es válido."
        ) from exc
    return reader


def unir_pdfs(partes: Sequence[tuple[str, bytes]]) -> bytes:
    """Une los PDFs en el orden dado. ``partes`` es (nombre, bytes).

    Cada página sale tal cual del PDF original. Un archivo ilegible levanta
    ``OrdenPagoError`` con su nombre.
    """
    if not partes:
        raise OrdenPagoError("No hay PDFs para unir.")
    writer = PdfWriter()
    for nombre, contenido in partes:
        reader = _leer(nombre, contenido)
        try:
            writer.append(reader)
        except (PdfReadError, ValueError, TypeError, KeyError, OSError) as exc:
            raise OrdenPagoError(
                f"No se pudo unir {nombre}: el PDF está dañado o no es válido."
            ) from exc
    salida = io.BytesIO()
    writer.write(salida)
    return salida.getvalue()

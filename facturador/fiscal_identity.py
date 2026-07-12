"""Identidad fiscal inmutable del perfil (FAC-39 / ADR 0001).

Un perfil tiene exactamente un CUIT: el del certificado ARCA validado. Se
sella en ``settings.fiscal_cuit`` la primera vez que se resuelve y no se
puede cambiar por configuración de emisor ni por rotación de certificado a
otro contribuyente. Cambiar de CUIT exige un perfil nuevo o un reset
explícito del perfil (no hay migración automática).
"""

from __future__ import annotations

import sqlite3

from .certs import CertificateError, load_certificate_metadata
from .profile import EnvironmentProfile

FISCAL_CUIT_KEY = "fiscal_cuit"

_CUIT_RE_MSG = (
    "Para cambiar de contribuyente hay que usar un perfil nuevo o "
    "resetear este (no hay migración automática de identidad fiscal)."
)


class FiscalIdentityError(ValueError):
    """Operación que alteraría el CUIT sellado del perfil."""


def get_sealed_fiscal_cuit(conn: sqlite3.Connection) -> str | None:
    """CUIT sellado del perfil, o ``None`` si todavía no se selló."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (FISCAL_CUIT_KEY,)
    ).fetchone()
    if row is None:
        return None
    value = str(row["value"] if isinstance(row, sqlite3.Row) else row[0]).strip()
    return value or None


def seal_fiscal_cuit(conn: sqlite3.Connection, cuit: str) -> str:
    """Sella el CUIT del perfil. Idempotente si coincide; rechaza otro CUIT."""
    normalized = _normalize_cuit(cuit)
    current = get_sealed_fiscal_cuit(conn)
    if current is None:
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (FISCAL_CUIT_KEY, normalized),
            )
        return normalized
    if current != normalized:
        raise FiscalIdentityError(
            f"Este perfil está sellado al CUIT {current}; el certificado "
            f"corresponde al CUIT {normalized}. {_CUIT_RE_MSG}"
        )
    return current


def resolve_profile_fiscal_cuit(
    conn: sqlite3.Connection,
    profile: EnvironmentProfile,
) -> str:
    """Identidad fiscal del perfil: sello en settings, o del certificado.

    Si aún no hay sello, lo deriva del certificado instalado y lo persiste.
    Si hay sello y certificado, deben coincidir.
    """
    sealed = get_sealed_fiscal_cuit(conn)
    meta = load_certificate_metadata(profile)
    if meta is None:
        if sealed is not None:
            return sealed
        raise FiscalIdentityError(
            "No hay certificado instalado ni CUIT sellado en este perfil."
        )
    if sealed is None:
        return seal_fiscal_cuit(conn, meta.cuit)
    if sealed != meta.cuit:
        raise FiscalIdentityError(
            f"Este perfil está sellado al CUIT {sealed}, pero el certificado "
            f"instalado corresponde al CUIT {meta.cuit}. {_CUIT_RE_MSG}"
        )
    return sealed


def assert_certificate_matches_profile(
    profile: EnvironmentProfile,
    candidate_cuit: str,
    *,
    sealed_cuit: str | None = None,
) -> None:
    """Rechaza un certificado cuyo CUIT no coincide con la identidad del perfil.

    Compara contra el certificado vivo (si existe) y, si se pasa, contra el
    sello en settings. No muta nada.
    """
    candidate = _normalize_cuit(candidate_cuit)
    if sealed_cuit is not None:
        sealed = _normalize_cuit(sealed_cuit)
        if candidate != sealed:
            raise CertificateError(
                f"El certificado corresponde al CUIT {candidate}, pero este "
                f"perfil está sellado al CUIT {sealed}. {_CUIT_RE_MSG}"
            )
    existing = load_certificate_metadata(profile)
    if existing is not None and existing.cuit != candidate:
        raise CertificateError(
            f"El certificado corresponde al CUIT {candidate}, pero este "
            f"perfil ya tiene instalado el CUIT {existing.cuit}. {_CUIT_RE_MSG}"
        )


def _normalize_cuit(cuit: str | int) -> str:
    text = str(cuit).strip()
    if not text.isdigit() or len(text) != 11:
        raise FiscalIdentityError(
            f"CUIT fiscal inválido: se esperaban 11 dígitos, llegó {text!r}."
        )
    return text

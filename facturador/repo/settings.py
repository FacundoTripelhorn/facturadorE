"""Acceso a datos de settings (configuración de dominio, clave/valor)."""

from __future__ import annotations

import sqlite3

# Claves que solo escriben helpers dedicados (FAC-39: identidad fiscal;
# FAC-47: estado de seed backup). ``save_settings`` las rechaza para que
# la config de emisor/backups no pueda sobrescribirlas. Los nombres deben
# coincidir con ``SEED_BACKUP_STATE_KEYS`` en ``seed_backup_sync``.
PROTECTED_KEYS = frozenset(
    {
        "fiscal_cuit",
        "seed_backup_status",
        "seed_backup_last_success_at",
        "seed_backup_last_attempt_at",
        "seed_backup_last_error",
        "seed_backup_pending_reason",
    }
)


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM settings")
    }


def save_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    """Upsert de los pares recibidos; no toca claves ausentes.

    Rechaza claves protegidas (p.ej. ``fiscal_cuit``): la identidad fiscal
    se sella solo vía ``fiscal_identity.seal_fiscal_cuit``.
    """
    blocked = PROTECTED_KEYS & values.keys()
    if blocked:
        raise ValueError(
            "No se puede modificar la identidad fiscal del perfil desde "
            f"configuración ({', '.join(sorted(blocked))}). "
            "Para cambiar de contribuyente hay que usar un perfil nuevo o "
            "resetear este."
        )
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )

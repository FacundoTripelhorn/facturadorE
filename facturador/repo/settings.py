"""Acceso a datos de settings (configuración de dominio, clave/valor)."""

from __future__ import annotations

import sqlite3


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM settings")
    }


def save_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    """Upsert de los pares recibidos; no toca claves ausentes."""
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )

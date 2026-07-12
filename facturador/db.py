"""Conexión SQLite (design.md §2.2, FAC-43).

El esquema se aplica vía migraciones versionadas (``facturador.migrations``),
no con ``CREATE TABLE IF NOT EXISTS`` en cada conexión. Toda conexión de la
app habilita ``PRAGMA foreign_keys=ON``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .migrations import MigrationError, current_version, latest_version, migrate

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

__all__ = [
    "SCHEMA_PATH",
    "MigrationError",
    "connect",
    "current_version",
    "latest_version",
    "migrate",
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Abre la DB del perfil, habilita FKs y aplica migraciones pendientes.

    Si una migración falla, la transacción se revierte, la conexión se
    cierra y se propaga ``MigrationError`` (arranque bloqueado).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI atiende en threadpool; el volumen
    # (~1 factura/semana) no justifica pool de conexiones.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # PRAGMA por conexión (SQLite no lo persiste en el archivo).
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        migrate(conn)
    except MigrationError:
        conn.close()
        raise
    return conn

"""Conexión SQLite (design.md §2.2). El esquema vive en schema.sql y las
queries en el paquete repo/."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _apply_migrations(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(invoices)")}
    if "emisor_id" not in cols:
        conn.execute(
            "ALTER TABLE invoices ADD COLUMN emisor_id TEXT REFERENCES emisores(id)"
        )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI atiende en threadpool; el volumen
    # (~1 factura/semana) no justifica pool de conexiones.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _apply_migrations(conn)
    return conn

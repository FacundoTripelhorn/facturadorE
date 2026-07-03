"""SQLite mínimo para el spike: cache de tablas dinámicas de ARCA (§2.2)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS arca_params (
    kind        TEXT NOT NULL,
    code        TEXT NOT NULL,
    description TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (kind, code)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def replace_params(
    conn: sqlite3.Connection,
    kind: str,
    records: list[dict],
    fetched_at: dt.datetime | None = None,
) -> int:
    """Reemplaza el cache completo de un kind (refresh atómico)."""
    fetched = (fetched_at or dt.datetime.now(dt.timezone.utc)).isoformat()
    with conn:
        conn.execute("DELETE FROM arca_params WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT INTO arca_params (kind, code, description, valid_from, valid_to, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    kind,
                    r["code"],
                    r.get("description"),
                    r.get("valid_from"),
                    r.get("valid_to"),
                    fetched,
                )
                for r in records
            ],
        )
    return len(records)


def get_params(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM arca_params WHERE kind = ? ORDER BY code", (kind,)
    ).fetchall()

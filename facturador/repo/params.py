"""Acceso a datos de arca_params (cache de tablas dinámicas de ARCA)."""

from __future__ import annotations

import datetime as dt
import sqlite3


def replace_params(
    conn: sqlite3.Connection,
    kind: str,
    records: list[dict],
    fetched_at: dt.datetime | None = None,
) -> int:
    """Reemplaza el cache completo de un kind (refresh atómico)."""
    fetched = (fetched_at or dt.datetime.now(dt.UTC)).isoformat()
    with conn:
        conn.execute("DELETE FROM arca_params WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT INTO arca_params"
            " (kind, code, description, valid_from, valid_to, fetched_at)"
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

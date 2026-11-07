"""Acceso a datos de clients."""

from __future__ import annotations

import sqlite3

from ._common import new_id, now

CLIENT_FIELDS = (
    "razon_social",
    "domicilio",
    "pais_dst",
    "cuit_pais",
    "id_impositivo",
    "moneda_default",
    "incoterms_default",
    "idioma_default",
    "forma_pago_default",
    "descripcion_default",
    "cuenta_recaudadora",
    "is_default",
)
# Campos que el llamador puede omitir (quedan NULL).
_OPTIONAL_FIELDS = frozenset({"cuenta_recaudadora"})


def _values(data: dict) -> tuple:
    return tuple(
        data.get(f) if f in _OPTIONAL_FIELDS else data[f] for f in CLIENT_FIELDS
    )


def create_client(conn: sqlite3.Connection, data: dict) -> sqlite3.Row:
    client_id = new_id()
    ts = now()
    with conn:
        if data.get("is_default"):
            conn.execute("UPDATE clients SET is_default = 0")
        conn.execute(
            f"INSERT INTO clients (id, {', '.join(CLIENT_FIELDS)},"
            " created_at, updated_at)"
            f" VALUES (?{', ?' * len(CLIENT_FIELDS)}, ?, ?)",
            (client_id, *_values(data), ts, ts),
        )
    row = get_client(conn, client_id)
    if row is None:  # recién insertado: no debe pasar
        raise RuntimeError(f"Cliente {client_id} no se pudo releer tras el INSERT")
    return row


def update_client(
    conn: sqlite3.Connection, client_id: str, data: dict
) -> sqlite3.Row | None:
    if get_client(conn, client_id) is None:
        return None
    with conn:
        if data.get("is_default"):
            conn.execute("UPDATE clients SET is_default = 0")
        conn.execute(
            f"UPDATE clients SET {', '.join(f'{f} = ?' for f in CLIENT_FIELDS)},"
            " updated_at = ? WHERE id = ?",
            (*_values(data), now(), client_id),
        )
    return get_client(conn, client_id)


def get_client(conn: sqlite3.Connection, client_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def get_default_client(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clients WHERE is_default = 1 LIMIT 1").fetchone()


def list_clients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clients ORDER BY is_default DESC, razon_social"
    ).fetchall()

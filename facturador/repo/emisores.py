"""Acceso a datos de emisores.

La entidad es el emisor, LOCAL al perfil (FAC-26): la DB entera es de un
solo ambiente, así que acá no se filtra por ambiente. Cada emisor tiene sus
puntos de venta habilitados (JSON) y un perfil puede tener varios emisores.
La columna ``ambiente`` queda como sello del perfil al crear (lo estampa el
service, no el usuario); no se actualiza después.
"""

from __future__ import annotations

import sqlite3

from ._common import new_id, now

EMISOR_FIELDS = (
    "razon_social",
    "domicilio",
    "iibb",
    "inicio_actividades",
    "condicion_iva",
    "ambiente",
    "puntos_venta",
)

EMISOR_UPDATE_FIELDS = (
    "razon_social",
    "domicilio",
    "iibb",
    "inicio_actividades",
    "condicion_iva",
    "puntos_venta",
)


def get_emisor(conn: sqlite3.Connection, emisor_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM emisores WHERE id = ?", (emisor_id,)
    ).fetchone()


def list_emisores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Sin parámetro de ambiente (FAC-26): todos los emisores de la DB son
    # del perfil.
    return conn.execute(
        "SELECT * FROM emisores ORDER BY created_at, id"
    ).fetchall()


def create_emisor(conn: sqlite3.Connection, data: dict) -> sqlite3.Row:
    emisor_id = new_id()
    ts = now()
    with conn:
        conn.execute(
            f"INSERT INTO emisores (id, {', '.join(EMISOR_FIELDS)},"
            " created_at, updated_at)"
            f" VALUES (?{', ?' * len(EMISOR_FIELDS)}, ?, ?)",
            (emisor_id, *(data[f] for f in EMISOR_FIELDS), ts, ts),
        )
    row = get_emisor(conn, emisor_id)
    if row is None:
        raise RuntimeError(f"Emisor {emisor_id} no se pudo releer tras el INSERT")
    return row


def update_emisor(
    conn: sqlite3.Connection, emisor_id: str, data: dict
) -> sqlite3.Row | None:
    if get_emisor(conn, emisor_id) is None:
        return None
    with conn:
        conn.execute(
            f"UPDATE emisores SET"
            f" {', '.join(f'{f} = ?' for f in EMISOR_UPDATE_FIELDS)},"
            " updated_at = ? WHERE id = ?",
            (*(data[f] for f in EMISOR_UPDATE_FIELDS), now(), emisor_id),
        )
    return get_emisor(conn, emisor_id)

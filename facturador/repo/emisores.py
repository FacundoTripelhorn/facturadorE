"""Acceso a datos de emisores.

La entidad es el emisor: cada uno declara con qué ambiente interactúa y qué
puntos de venta tiene habilitados (JSON). Un mismo ambiente puede tener
varios emisores.
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


def get_emisor_por_ambiente(
    conn: sqlite3.Connection, ambiente: str
) -> sqlite3.Row | None:
    """El emisor con el que se opera en un ambiente. Puede haber varios; hoy
    la app usa el más antiguo (la selección de emisor llega con el alta)."""
    return conn.execute(
        "SELECT * FROM emisores WHERE ambiente = ?"
        " ORDER BY created_at, id LIMIT 1",
        (ambiente,),
    ).fetchone()


def upsert_emisor(conn: sqlite3.Connection, data: dict) -> None:
    """Crea o actualiza el (hoy único) emisor del ambiente que ``data``
    declara; el alta de varios emisores llega con su feature."""
    existente = get_emisor_por_ambiente(conn, data["ambiente"])
    ts = now()
    with conn:
        if existente is None:
            conn.execute(
                f"INSERT INTO emisores (id, {', '.join(EMISOR_FIELDS)},"
                " created_at, updated_at)"
                f" VALUES (?{', ?' * len(EMISOR_FIELDS)}, ?, ?)",
                (new_id(), *(data[f] for f in EMISOR_FIELDS), ts, ts),
            )
        else:
            conn.execute(
                f"UPDATE emisores SET"
                f" {', '.join(f'{f} = ?' for f in EMISOR_FIELDS)},"
                " updated_at = ? WHERE id = ?",
                (*(data[f] for f in EMISOR_FIELDS), ts, existente["id"]),
            )

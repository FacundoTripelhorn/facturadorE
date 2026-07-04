"""Acceso a datos: clients / invoices / invoice_items / arca_params.

Convenciones: ids uuid4 hex; montos como TEXT (str de Decimal, nunca float);
fechas de comprobante AAAAMMDD; timestamps ISO UTC.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# --- clients ---

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
    "is_default",
)


def create_client(conn: sqlite3.Connection, data: dict) -> sqlite3.Row:
    client_id = _new_id()
    now = _now()
    with conn:
        if data.get("is_default"):
            conn.execute("UPDATE clients SET is_default = 0")
        conn.execute(
            f"INSERT INTO clients (id, {', '.join(CLIENT_FIELDS)},"
            " created_at, updated_at)"
            f" VALUES (?{', ?' * len(CLIENT_FIELDS)}, ?, ?)",
            (client_id, *(data[f] for f in CLIENT_FIELDS), now, now),
        )
    row = get_client(conn, client_id)
    assert row is not None  # recién insertado
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
            (*(data[f] for f in CLIENT_FIELDS), _now(), client_id),
        )
    return get_client(conn, client_id)


def get_client(conn: sqlite3.Connection, client_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def get_default_client(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM clients WHERE is_default = 1 LIMIT 1"
    ).fetchone()


def list_clients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clients ORDER BY is_default DESC, razon_social"
    ).fetchall()


# --- invoices ---

INVOICE_FIELDS = (
    "client_id",
    "cbte_tipo",
    "punto_venta",
    "fecha_cbte",
    "fecha_pago",
    "tipo_expo",
    "permiso_existente",
    "dst_cmp",
    "cliente",
    "cuit_pais_cliente",
    "domicilio_cliente",
    "id_impositivo",
    "moneda_id",
    "moneda_ctz",
    "incoterms",
    "incoterms_ds",
    "forma_pago",
    "idioma_cbte",
    "imp_total",
    "obs",
    "environment",
)


def create_invoice(
    conn: sqlite3.Connection, data: dict, items: list[dict]
) -> sqlite3.Row:
    invoice_id = _new_id()
    now = _now()
    with conn:
        conn.execute(
            f"INSERT INTO invoices (id, {', '.join(INVOICE_FIELDS)},"
            " status, created_at, updated_at)"
            f" VALUES (?{', ?' * len(INVOICE_FIELDS)}, 'draft', ?, ?)",
            (invoice_id, *(data[f] for f in INVOICE_FIELDS), now, now),
        )
        for item in items:
            conn.execute(
                "INSERT INTO invoice_items"
                " (id, invoice_id, pro_codigo, pro_ds, pro_qty, pro_umed,"
                "  pro_precio_uni, pro_total_item)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(),
                    invoice_id,
                    item["pro_codigo"],
                    item["pro_ds"],
                    item["pro_qty"],
                    item["pro_umed"],
                    item["pro_precio_uni"],
                    item["pro_total_item"],
                ),
            )
    row = get_invoice(conn, invoice_id)
    assert row is not None  # recién insertado
    return row


def get_invoice(conn: sqlite3.Connection, invoice_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()


def get_invoice_items(conn: sqlite3.Connection, invoice_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY rowid",
        (invoice_id,),
    ).fetchall()


def list_invoices(
    conn: sqlite3.Connection, limit: int = 50, offset: int = 0
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


# Columnas que la máquina de estados puede tocar tras la creación. Los nombres
# de columna se interpolan en el SQL, así que el allowlist es obligatorio.
UPDATABLE_INVOICE_FIELDS = frozenset(
    {
        "status",
        "arca_id",
        "cbte_nro",
        "cae",
        "cae_fch_vto",
        "raw_request",
        "raw_response",
        "last_error",
    }
)


def update_invoice(conn: sqlite3.Connection, invoice_id: str, **fields) -> None:
    unknown = set(fields) - UPDATABLE_INVOICE_FIELDS
    if unknown:
        raise ValueError(f"Columnas no actualizables: {', '.join(sorted(unknown))}")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with conn:
        conn.execute(
            f"UPDATE invoices SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), invoice_id),
        )


def try_transition_to_submitting(
    conn: sqlite3.Connection, invoice_id: str, stale_seconds: int = 300
) -> bool:
    """Lock anti doble-submit (§2.3 regla 1): pasa a 'submitting' solo desde
    draft/unknown, o desde un 'submitting' viejo (proceso muerto a mitad de
    llamada). Un 'submitting' reciente => otro submit en curso, no tomar."""
    stale_cutoff = (
        dt.datetime.now(dt.UTC) - dt.timedelta(seconds=stale_seconds)
    ).isoformat()
    with conn:
        cursor = conn.execute(
            "UPDATE invoices SET status = 'submitting', updated_at = ?"
            " WHERE id = ? AND (status IN ('draft', 'unknown')"
            "   OR (status = 'submitting' AND updated_at < ?))",
            (_now(), invoice_id, stale_cutoff),
        )
    return cursor.rowcount == 1


def max_authorized_cbte_nro(
    conn: sqlite3.Connection, punto_venta: int, cbte_tipo: int
) -> int:
    row = conn.execute(
        "SELECT MAX(cbte_nro) AS m FROM invoices"
        " WHERE punto_venta = ? AND cbte_tipo = ? AND status = 'authorized'",
        (punto_venta, cbte_tipo),
    ).fetchone()
    return int(row["m"] or 0)


# --- arca_params (cache de tablas dinámicas) ---


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

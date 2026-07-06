"""Acceso a datos de invoices / invoice_items, incluido el lock de submit."""

from __future__ import annotations

import datetime as dt
import sqlite3

from ..constants import InvoiceStatus
from ._common import new_id, now

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
    invoice_id = new_id()
    ts = now()
    with conn:
        conn.execute(
            f"INSERT INTO invoices (id, {', '.join(INVOICE_FIELDS)},"
            " status, created_at, updated_at)"
            f" VALUES (?{', ?' * len(INVOICE_FIELDS)}, ?, ?, ?)",
            (
                invoice_id,
                *(data[f] for f in INVOICE_FIELDS),
                InvoiceStatus.DRAFT,
                ts,
                ts,
            ),
        )
        for item in items:
            conn.execute(
                "INSERT INTO invoice_items"
                " (id, invoice_id, pro_codigo, pro_ds, pro_qty, pro_umed,"
                "  pro_precio_uni, pro_total_item)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
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
    if row is None:  # recién insertado: no debe pasar
        raise RuntimeError(f"Factura {invoice_id} no se pudo releer tras el INSERT")
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
    conn: sqlite3.Connection,
    limit: int = 50,
    offset: int = 0,
    statuses: tuple[str, ...] | None = None,
) -> list[sqlite3.Row]:
    where = ""
    params: tuple = ()
    if statuses:
        where = f" WHERE status IN ({', '.join('?' * len(statuses))})"
        params = tuple(statuses)
    return conn.execute(
        f"SELECT * FROM invoices{where}"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def count_invoices_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM invoices GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


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
            (*fields.values(), now(), invoice_id),
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
            "UPDATE invoices SET status = ?, updated_at = ?"
            " WHERE id = ? AND (status IN (?, ?)"
            "   OR (status = ? AND updated_at < ?))",
            (
                InvoiceStatus.SUBMITTING,
                now(),
                invoice_id,
                InvoiceStatus.DRAFT,
                InvoiceStatus.UNKNOWN,
                InvoiceStatus.SUBMITTING,
                stale_cutoff,
            ),
        )
    return cursor.rowcount == 1


def max_authorized_cbte_nro(
    conn: sqlite3.Connection, punto_venta: int, cbte_tipo: int
) -> int:
    row = conn.execute(
        "SELECT MAX(cbte_nro) AS m FROM invoices"
        " WHERE punto_venta = ? AND cbte_tipo = ? AND status = ?",
        (punto_venta, cbte_tipo, InvoiceStatus.AUTHORIZED),
    ).fetchone()
    return int(row["m"] or 0)


def delete_draft(conn: sqlite3.Connection, invoice_id: str) -> bool:
    """Borra un borrador jamás enviado. El WHERE repite la guarda de estado
    para que sea atómica: si otro proceso lo transicionó, no borra nada."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM invoices WHERE id = ? AND status = ?"
            " AND raw_request IS NULL",
            (invoice_id, InvoiceStatus.DRAFT),
        )
        if cursor.rowcount == 1:
            conn.execute(
                "DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,)
            )
    return cursor.rowcount == 1


def max_arca_id(conn: sqlite3.Connection) -> int:
    """Máximo Id de FEXAuthorize reservado localmente, en cualquier estado.

    Una factura 'unknown'/'submitting' ya reservó su arca_id aunque ARCA no
    lo conozca todavía (el timeout pudo ser pre-envío): el próximo Id nuevo
    debe superar también estas reservas, no solo FEXGetLast_ID."""
    row = conn.execute("SELECT MAX(arca_id) AS m FROM invoices").fetchone()
    return int(row["m"] or 0)

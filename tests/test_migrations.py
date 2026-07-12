"""Migraciones de esquema e integridad SQLite (FAC-43)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from facturador import db
from facturador.migrations import (
    MIGRATIONS,
    Migration,
    MigrationError,
    current_version,
    latest_version,
    migrate,
)

BASELINE_TABLES = {
    "schema_migrations",
    "settings",
    "emisores",
    "arca_params",
    "clients",
    "invoices",
    "invoice_items",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def test_fresh_profile_creates_latest_schema(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "fresh.db")
    try:
        assert current_version(conn) == latest_version()
        assert current_version(conn) == MIGRATIONS[-1].version
        assert BASELINE_TABLES <= _tables(conn)
        rows = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [(r["version"], r["name"]) for r in rows] == [
            (m.version, m.name) for m in MIGRATIONS
        ]
    finally:
        conn.close()


def test_connect_is_idempotent_when_already_migrated(tmp_path: Path) -> None:
    path = tmp_path / "idem.db"
    first = db.connect(path)
    first.execute(
        "INSERT INTO settings (key, value) VALUES ('k', 'v')"
    )
    first.commit()
    first.close()

    second = db.connect(path)
    try:
        assert current_version(second) == latest_version()
        assert second.execute(
            "SELECT value FROM settings WHERE key = 'k'"
        ).fetchone()[0] == "v"
    finally:
        second.close()


def test_older_schema_upgrades_in_order(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        baseline = MIGRATIONS[0]
        migrate(conn, migrations=(baseline,))
        assert current_version(conn) == 1

        def add_marker(c: sqlite3.Connection) -> None:
            c.execute("ALTER TABLE settings ADD COLUMN marker TEXT")

        def add_flag(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO settings (key, value, marker) VALUES ('flag', '1', 'ok')"
            )

        v2 = Migration(version=2, name="add_marker", apply_fn=add_marker)
        v3 = Migration(version=3, name="seed_flag", apply_fn=add_flag)
        final = migrate(conn, migrations=(baseline, v2, v3))
        assert final == 3
        assert current_version(conn) == 3
        applied = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY version"
            )
        ]
        assert applied == ["baseline", "add_marker", "seed_flag"]
        row = conn.execute(
            "SELECT value, marker FROM settings WHERE key = 'flag'"
        ).fetchone()
        assert row["value"] == "1"
        assert row["marker"] == "ok"
    finally:
        conn.close()


def test_failed_migration_leaves_db_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "fail.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        baseline = MIGRATIONS[0]
        migrate(conn, migrations=(baseline,))
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('keep', 'me')"
        )
        conn.commit()
        assert current_version(conn) == 1

        def boom(_c: sqlite3.Connection) -> None:
            raise RuntimeError("simulated failure")

        with pytest.raises(MigrationError, match="rollback aplicado"):
            migrate(
                conn,
                migrations=(
                    baseline,
                    Migration(version=2, name="boom", apply_fn=boom),
                ),
            )

        assert current_version(conn) == 1
        assert "marker" not in {
            r[1]
            for r in conn.execute("PRAGMA table_info(settings)").fetchall()
        }
        assert conn.execute(
            "SELECT value FROM settings WHERE key = 'keep'"
        ).fetchone()[0] == "me"
        assert _tables(conn) == BASELINE_TABLES
    finally:
        conn.close()


def test_failed_migration_blocks_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si migrate falla dentro de connect, no se devuelve una conexión usable."""
    path = tmp_path / "blocked.db"

    def boom(_conn: sqlite3.Connection, migrations=None) -> int:
        raise MigrationError("simulated migration failure")

    monkeypatch.setattr(db, "migrate", boom)
    with pytest.raises(MigrationError, match="simulated migration failure"):
        db.connect(path)
    assert path.exists()


def test_foreign_keys_enabled_on_every_connection(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "fk.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        now = "2026-01-01T00:00:00+00:00"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO invoice_items ("
                "  id, invoice_id, pro_ds, pro_precio_uni, pro_total_item"
                ") VALUES ('item-1', 'missing-invoice', 'x', '1', '1')"
            )
        # Padre válido + hijo OK.
        conn.execute(
            "INSERT INTO invoices ("
            "  id, punto_venta, fecha_cbte, fecha_pago, dst_cmp, cliente,"
            "  cuit_pais_cliente, moneda_ctz, imp_total, environment,"
            "  created_at, updated_at"
            ") VALUES ("
            "  'inv-1', 1, '20260101', '20260101', 203, 'Acme',"
            "  55000000000, '1', '10', 'homo', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO invoice_items ("
            "  id, invoice_id, pro_ds, pro_precio_uni, pro_total_item"
            ") VALUES ('item-1', 'inv-1', 'servicio', '10', '10')"
        )
        conn.commit()
    finally:
        conn.close()


def test_foreign_keys_reenabled_after_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "fk2.db"
    db.connect(path).close()
    # Conexión cruda: SQLite apaga FKs por default.
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    raw.close()
    again = db.connect(path)
    try:
        assert again.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        again.close()

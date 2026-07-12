"""Migraciones de esquema SQLite versionadas (FAC-43).

El ciclo de vida es:

1. ``schema_migrations`` registra cada versión aplicada.
2. Al conectar, se aplican en orden las migraciones pendientes dentro de
   una sola transacción ``BEGIN IMMEDIATE`` … ``COMMIT``.
3. Si alguna falla, se hace ``ROLLBACK`` y el arranque queda bloqueado
   con un ``MigrationError`` claro — la DB no queda a medias.

La baseline (versión 1) es el esquema de perfiles aislados vigente. Toda
DB vive bajo un perfil; no hay camino de compatibilidad para esquemas
anteriores a este mecanismo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(Exception):
    """Fallo al migrar el esquema; la DB quedó sin cambios y el arranque
    no debe continuar."""


@dataclass(frozen=True)
class Migration:
    """Una migración ordenada por ``version`` (entera, 1-based)."""

    version: int
    name: str
    sql: str | None = None
    apply_fn: Callable[[sqlite3.Connection], None] | None = None

    def apply(self, conn: sqlite3.Connection) -> None:
        if self.sql is not None:
            execute_script(conn, self.sql)
        elif self.apply_fn is not None:
            self.apply_fn(conn)
        else:
            raise MigrationError(
                f"Migración {self.version} ({self.name}) no tiene SQL ni apply_fn"
            )


def execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Ejecuta varias sentencias SQL sin el COMMIT implícito de
    ``executescript`` (necesario para rollback transaccional)."""
    for statement in _split_statements(script):
        conn.execute(statement)


def _split_statements(script: str) -> list[str]:
    """Parte un script en sentencias, ignorando comentarios de línea ``--``.

    Suficiente para DDL del esquema (sin `;` dentro de strings literales).
    """
    lines: list[str] = []
    for line in script.splitlines():
        if line.lstrip().startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [part.strip() for part in body.split(";") if part.strip()]


def _load_baseline_sql() -> str:
    path = Path(__file__).resolve().parent.parent / "schema.sql"
    return path.read_text(encoding="utf-8")


def _add_invoices_cuit_emisor(conn: sqlite3.Connection) -> None:
    """FAC-39: snapshot del CUIT fiscal del perfil en cada factura.

    Condicional: el baseline (v1) ya puede incluir la columna si
    ``schema.sql`` se actualizó; en DBs que corrieron el baseline viejo
    hay que agregarla.
    """
    cols = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "cuit_emisor" not in cols:
        conn.execute("ALTER TABLE invoices ADD COLUMN cuit_emisor TEXT")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="baseline", sql=_load_baseline_sql()),
    Migration(
        version=2,
        name="invoices_cuit_emisor",
        apply_fn=_add_invoices_cuit_emisor,
    ),
)


def latest_version(migrations: Sequence[Migration] | None = None) -> int:
    ms = migrations if migrations is not None else MIGRATIONS
    if not ms:
        return 0
    return max(m.version for m in ms)


def current_version(conn: sqlite3.Connection) -> int:
    """Versión aplicada más alta, o 0 si aún no hay tabla de migraciones."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    assert version_row is not None
    return int(version_row[0])


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _validate_migrations(migrations: Sequence[Migration]) -> list[Migration]:
    ordered = sorted(migrations, key=lambda m: m.version)
    seen: set[int] = set()
    expected = 1
    for m in ordered:
        if m.version < 1:
            raise MigrationError(f"Versión de migración inválida: {m.version}")
        if m.version in seen:
            raise MigrationError(f"Versión de migración duplicada: {m.version}")
        if m.version != expected:
            raise MigrationError(
                f"Migraciones deben ser contiguas desde 1; "
                f"se esperaba {expected}, llegó {m.version}"
            )
        seen.add(m.version)
        expected = m.version + 1
    return ordered


def migrate(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> int:
    """Aplica migraciones pendientes. Devuelve la versión final.

    Toda la corrida (crear ``schema_migrations`` + DDL + registros) ocurre
    en una sola transacción. Ante error: rollback y ``MigrationError``.
    """
    ordered = _validate_migrations(
        migrations if migrations is not None else MIGRATIONS
    )
    start_version = current_version(conn)
    pending = [m for m in ordered if m.version > start_version]
    if not pending:
        return start_version

    if pending[0].version != start_version + 1:
        raise MigrationError(
            f"Hueco en migraciones: DB en v{start_version}, "
            f"siguiente disponible es v{pending[0].version}"
        )

    # Evitar que el autocommit de sqlite3 interfiera con BEGIN/COMMIT
    # explícitos (no usamos executescript por la misma razón).
    previous_isolation = conn.isolation_level
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            if start_version == 0 and "schema_migrations" not in _table_names(conn):
                conn.execute(_SCHEMA_MIGRATIONS_DDL)
            applied_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            version = start_version
            for migration in pending:
                if migration.version != version + 1:
                    raise MigrationError(
                        f"Orden de migración inválido: tras v{version} "
                        f"llegó v{migration.version}"
                    )
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, applied_at),
                )
                version = migration.version
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(
                f"Falló la migración de esquema (rollback aplicado): {exc}"
            ) from exc
    finally:
        conn.isolation_level = previous_isolation

    return version

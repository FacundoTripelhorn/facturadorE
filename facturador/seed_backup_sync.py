"""Trigger encrypted seed backup on profile config changes.

The seed is pure configuration — it does not change on emission.
This module:

* marks a pending backup when config changes (emisor, PV, default client /
  UI, recipients, onboarding → ready);
* coalesces multiple edits in one session into a single upload (debounce);
* creates the local ``seed.age`` and uploads via the S3 adapter;
* never blocks or rolls back the config change on backup failure;
* persists queryable state (ok / pending / failed + timestamps) for diagnostics;
* retries on next process launch and on an explicit ``backup_now`` call.

Authorization / voucher emission must not call into this module.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .db import connect as connect_db
from .fiscal_identity import get_sealed_fiscal_cuit
from .profile import ProfilePaths
from .s3_seed import (
    MemoryObjectStore,
    ObjectStore,
    S3SeedAdapter,
    S3SeedError,
    SeedLocation,
)
from .seed_backup import (
    SeedBackupError,
    create_encrypted_seed,
    recipients_path,
    write_recipients_file,
)
from .settings import BACKUP_PREFIX_DEFAULT, load_settings

logger = logging.getLogger(__name__)

# Settings keys (not part of the seed UI payload — assemble_seed allowlists).
STATUS_KEY = "seed_backup_status"
LAST_SUCCESS_KEY = "seed_backup_last_success_at"
LAST_ATTEMPT_KEY = "seed_backup_last_attempt_at"
LAST_ERROR_KEY = "seed_backup_last_error"
PENDING_REASON_KEY = "seed_backup_pending_reason"

SEED_BACKUP_STATE_KEYS = frozenset(
    {
        STATUS_KEY,
        LAST_SUCCESS_KEY,
        LAST_ATTEMPT_KEY,
        LAST_ERROR_KEY,
        PENDING_REASON_KEY,
    }
)

DEFAULT_DEBOUNCE_S = 1.0


class SeedBackupStatus(StrEnum):
    """Queryable backup status for diagnostics."""

    IDLE = "idle"  # never attempted, nothing pending
    PENDING = "pending"  # config changed; upload not yet succeeded
    OK = "ok"  # last attempt succeeded; no pending changes
    FAILED = "failed"  # last attempt failed; still needs retry


@dataclass(frozen=True)
class SeedBackupState:
    status: SeedBackupStatus
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    last_error: str | None = None
    pending_reason: str | None = None

    @property
    def needs_retry(self) -> bool:
        return self.status in (SeedBackupStatus.PENDING, SeedBackupStatus.FAILED)


BackupRunner = Callable[[], str | None]
"""Runs create+upload; returns S3 URI or local path; raises on failure."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _read_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    value = str(row["value"] if isinstance(row, sqlite3.Row) else row[0]).strip()
    return value or None


def _write_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    """Persist backup-state keys (bypasses domain ``save_settings`` guard)."""
    if not values:
        return
    unknown = set(values) - SEED_BACKUP_STATE_KEYS
    if unknown:
        raise ValueError(
            f"Claves de estado de seed backup no permitidas: "
            f"{', '.join(sorted(unknown))}"
        )
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )


def load_seed_backup_state(conn: sqlite3.Connection) -> SeedBackupState:
    raw = _read_setting(conn, STATUS_KEY)
    try:
        status = SeedBackupStatus(raw) if raw else SeedBackupStatus.IDLE
    except ValueError:
        status = SeedBackupStatus.IDLE
    return SeedBackupState(
        status=status,
        last_success_at=_read_setting(conn, LAST_SUCCESS_KEY),
        last_attempt_at=_read_setting(conn, LAST_ATTEMPT_KEY),
        last_error=_read_setting(conn, LAST_ERROR_KEY),
        pending_reason=_read_setting(conn, PENDING_REASON_KEY),
    )


def mark_seed_backup_pending(
    conn: sqlite3.Connection, reason: str
) -> SeedBackupState:
    """Record that config changed; does not upload. Never raises to callers."""
    clean_reason = (reason or "config_changed").strip()[:200] or "config_changed"
    _write_settings(
        conn,
        {
            STATUS_KEY: SeedBackupStatus.PENDING.value,
            PENDING_REASON_KEY: clean_reason,
        },
    )
    return load_seed_backup_state(conn)


def _mark_success(conn: sqlite3.Connection, *, when: str) -> SeedBackupState:
    _write_settings(
        conn,
        {
            STATUS_KEY: SeedBackupStatus.OK.value,
            LAST_SUCCESS_KEY: when,
            LAST_ATTEMPT_KEY: when,
            LAST_ERROR_KEY: "",
            PENDING_REASON_KEY: "",
        },
    )
    return load_seed_backup_state(conn)


def _mark_failed(
    conn: sqlite3.Connection, *, when: str, error: str
) -> SeedBackupState:
    # Keep pending_reason; surface a short, non-secret error.
    clean = " ".join(error.split())[:500]
    _write_settings(
        conn,
        {
            STATUS_KEY: SeedBackupStatus.FAILED.value,
            LAST_ATTEMPT_KEY: when,
            LAST_ERROR_KEY: clean,
        },
    )
    return load_seed_backup_state(conn)


def run_seed_backup(
    conn: sqlite3.Connection,
    paths: ProfilePaths,
    *,
    environment: str,
    store: ObjectStore | None = None,
) -> str | None:
    """Create local ``seed.age`` and upload when S3 is configured.

    Returns the S3 URI when uploaded, or the local archive path when the
    profile has no bucket (local-only success). Raises ``SeedBackupError`` /
    ``S3SeedError`` on failure — callers must not propagate to config flows.
    """
    archive, envelope = create_encrypted_seed(
        conn, paths, environment=environment
    )
    seed = envelope["seed"]
    settings = load_settings(conn)
    bucket = settings.backup_s3_bucket.strip()
    if not bucket:
        # Empty bucket ⇒ local staging only (design.md §2.5 / Settings).
        return str(archive)

    cuit = str(seed.get("fiscal_cuit") or get_sealed_fiscal_cuit(conn) or "")
    prefix = (
        (settings.backup_s3_prefix or "").strip() or BACKUP_PREFIX_DEFAULT
    )
    location = SeedLocation.from_config(
        bucket=bucket,
        prefix=prefix,
        cuit=cuit,
        environment=environment,
    )
    adapter = S3SeedAdapter(location, store)
    ciphertext = archive.read_bytes()
    uri = adapter.put_seed(ciphertext)
    # Keep recipients.txt in S3 aligned with the local list.
    recipients = recipients_path(paths)
    if recipients.is_file():
        adapter.put_recipients(recipients.read_text(encoding="utf-8"))
    return uri


class SeedBackupCoordinator:
    """Session-scoped coalesce + retry for seed uploads.

    ``notify_config_changed`` is safe to call from request threads: it only
    marks pending and (re)schedules a debounced run. Failures never raise.

    Backup work (``_execute``) always opens its **own** SQLite connection so
    the FastAPI request thread's shared ``conn`` is never used from the
    debounced worker thread.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        paths: ProfilePaths,
        *,
        environment: str,
        run_backup: BackupRunner | None = None,
        store: ObjectStore | None = None,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        now: Callable[[], str] = _utcnow_iso,
        timer_factory: Callable[..., threading.Timer] | None = None,
        connect: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._conn = conn
        self._paths = paths
        self._environment = environment.strip().lower()
        self._store = store
        # None ⇒ default runner (uses the per-execute worker connection).
        self._custom_runner = run_backup
        self._debounce_s = max(0.0, float(debounce_s))
        self._now = now
        self._timer_factory = timer_factory or threading.Timer
        self._connect = connect or (lambda: connect_db(paths.db))
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._closed = False
        self._run_count = 0  # test aid

    def get_state(self) -> SeedBackupState:
        return load_seed_backup_state(self._conn)

    def notify_config_changed(self, reason: str) -> SeedBackupState:
        """Mark pending and schedule a coalesced upload. Never raises."""
        try:
            state = mark_seed_backup_pending(self._conn, reason)
        except Exception:
            logger.exception(
                "No se pudo persistir pending de seed backup (%s)", reason
            )
            return load_seed_backup_state(self._conn)
        # Sin recipients no hay cifrado posible: dejar pending y esperar
        # alta de recipients / retry en launch / backup_now (evita spam).
        if recipients_path(self._paths).is_file():
            self._schedule()
        return state

    def backup_now(self) -> SeedBackupState:
        """Run immediately (manual action / flush). Never raises to callers."""
        with self._lock:
            self._cancel_timer_unlocked()
        return self._execute()

    def retry_if_pending(self) -> SeedBackupState | None:
        """Launch-time retry when status is pending/failed. No-op if idle/ok."""
        state = self.get_state()
        if not state.needs_retry:
            return None
        return self.backup_now()

    def replace_recipients(self, text: str) -> SeedBackupState:
        """Write local ``recipients.txt`` and trigger a backup."""
        write_recipients_file(self._paths, text)
        return self.notify_config_changed("recipients")

    def shutdown(self) -> None:
        """Cancel any pending debounced run (app lifespan / tests)."""
        with self._lock:
            self._closed = True
            self._cancel_timer_unlocked()

    def _schedule(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._cancel_timer_unlocked()
            if self._debounce_s <= 0:
                # Immediate coalesce window closed — run on a worker thread
                # so the config request is never blocked by S3/age.
                worker = threading.Thread(
                    target=self._execute, name="seed-backup", daemon=True
                )
                worker.start()
                return
            timer = self._timer_factory(self._debounce_s, self._execute)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _cancel_timer_unlocked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _run_with_worker_conn(self, worker_conn: sqlite3.Connection) -> str | None:
        if self._custom_runner is not None:
            return self._custom_runner()
        return run_seed_backup(
            worker_conn,
            self._paths,
            environment=self._environment,
            store=self._store,
        )

    def _execute(self) -> SeedBackupState:
        """Create/upload on a dedicated DB connection (never ``self._conn``)."""
        when = self._now()
        try:
            worker_conn = self._connect()
        except Exception as exc:
            logger.exception("No se pudo abrir conexión worker de seed backup")
            try:
                return _mark_failed(self._conn, when=when, error=str(exc))
            except Exception:
                logger.exception("No se pudo persistir fallo de seed backup")
                return load_seed_backup_state(self._conn)

        try:
            try:
                self._run_with_worker_conn(worker_conn)
            except (SeedBackupError, S3SeedError, OSError, ValueError) as exc:
                logger.warning("Seed backup falló: %s", exc)
                try:
                    return _mark_failed(worker_conn, when=when, error=str(exc))
                except Exception:
                    logger.exception(
                        "No se pudo persistir fallo de seed backup"
                    )
                    return load_seed_backup_state(worker_conn)
            except Exception as exc:
                logger.exception("Seed backup falló inesperadamente")
                try:
                    return _mark_failed(worker_conn, when=when, error=str(exc))
                except Exception:
                    logger.exception(
                        "No se pudo persistir fallo de seed backup"
                    )
                    return load_seed_backup_state(worker_conn)
            self._run_count += 1
            try:
                return _mark_success(worker_conn, when=when)
            except Exception:
                logger.exception("No se pudo persistir éxito de seed backup")
                return load_seed_backup_state(worker_conn)
        finally:
            worker_conn.close()


def seed_backup_state_as_dict(state: SeedBackupState) -> dict[str, Any]:
    """JSON-friendly shape for API / diagnostics."""
    return {
        "status": state.status.value,
        "last_success_at": state.last_success_at,
        "last_attempt_at": state.last_attempt_at,
        "last_error": state.last_error,
        "pending_reason": state.pending_reason,
        "needs_retry": state.needs_retry,
    }


# Re-export for tests that inject an in-memory store without importing s3_seed.
__all__ = [
    "DEFAULT_DEBOUNCE_S",
    "MemoryObjectStore",
    "SEED_BACKUP_STATE_KEYS",
    "SeedBackupCoordinator",
    "SeedBackupState",
    "SeedBackupStatus",
    "load_seed_backup_state",
    "mark_seed_backup_pending",
    "run_seed_backup",
    "seed_backup_state_as_dict",
]

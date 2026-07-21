"""FAC-47: config-change seed backup trigger, coalesce, failure/retry."""

from __future__ import annotations

import sqlite3
import threading
import time
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db, repo
from facturador.api import create_app
from facturador.api.localhost_policy import loopback_base_url
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config
from facturador.constants import ArcaEnvironment
from facturador.fiscal_identity import seal_fiscal_cuit
from facturador.profile import EnvironmentProfile
from facturador.s3_seed import MemoryObjectStore, S3SeedPermanentError
from facturador.schemas import (
    BackupSettingsIn,
    ClientIn,
    EmisorCreateIn,
    EmisorUpdateIn,
    InvoiceCreate,
)
from facturador.seed_backup import recipients_path, write_recipients_file
from facturador.seed_backup_sync import (
    SeedBackupCoordinator,
    SeedBackupStatus,
    load_seed_backup_state,
    mark_seed_backup_pending,
    run_seed_backup,
)
from facturador.service import InvoiceService
from facturador.settings import Emisor, Settings, save_settings, set_active_emisor
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import (
    EMISOR_PRUEBA,
    TEST_CUIT,
    install_test_cert_pair,
    seed_params,
    seed_settings,
)

# Shape-only age public keys (same fixtures as test_s3_seed).
AGE_KEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"


@pytest.fixture
def profile_conn(tmp_path):
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "profile"
    )
    profile.paths.ensure_layout()
    conn = db.connect(profile.paths.db)
    seal_fiscal_cuit(conn, TEST_CUIT)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo", puntos_venta=(1,)),
            backup_s3_bucket="test-bucket",
            backup_s3_prefix="pfx",
        ),
    )
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    write_recipients_file(profile.paths, AGE_KEY_A + "\n")
    return profile, conn


def _coord(profile, conn, *, runner=None, store=None, debounce_s=60.0):
    return SeedBackupCoordinator(
        conn,
        profile.paths,
        environment="homo",
        run_backup=runner,
        store=store,
        debounce_s=debounce_s,
    )


def test_notify_marks_pending_and_coalesces_uploads(profile_conn):
    profile, conn = profile_conn
    calls: list[str] = []

    def runner() -> str:
        calls.append("run")
        return "s3://test-bucket/pfx/seed.age"

    coord = _coord(profile, conn, runner=runner, debounce_s=60.0)
    try:
        coord.notify_config_changed("emisor")
        coord.notify_config_changed("ui_config")
        coord.notify_config_changed("default_client")
        state = load_seed_backup_state(conn)
        assert state.status is SeedBackupStatus.PENDING
        assert state.pending_reason == "default_client"
        assert calls == []

        state = coord.backup_now()
        assert state.status is SeedBackupStatus.OK
        assert state.last_success_at
        assert calls == ["run"]
    finally:
        coord.shutdown()


def test_s3_failure_keeps_config_and_marks_failed(profile_conn):
    profile, conn = profile_conn
    # Config change (emisor) must succeed even when upload raises.
    service = InvoiceService(
        Config(env=ArcaEnvironment.HOMO, paths=profile.paths),
        conn,
        wsfex=None,  # type: ignore[arg-type]
        seed_backup=_coord(
            profile,
            conn,
            runner=lambda: (_ for _ in ()).throw(
                S3SeedPermanentError("S3 down")
            ),
            debounce_s=60.0,
        ),
    )
    try:
        row = service.update_emisor(
            repo.list_emisores(conn)[0]["id"],
            EmisorUpdateIn(
                razon_social="NUEVA SA",
                domicilio=EMISOR_PRUEBA["domicilio"],
                iibb=EMISOR_PRUEBA["iibb"],
                inicio_actividades=EMISOR_PRUEBA["inicio_actividades"],
                puntos_venta=[1],
            ),
        )
        assert row["razon_social"] == "NUEVA SA"
        # Debounced — still pending until flush.
        assert load_seed_backup_state(conn).status is SeedBackupStatus.PENDING

        state = service.run_seed_backup_now()
        assert state.status is SeedBackupStatus.FAILED
        assert "S3 down" in (state.last_error or "")
        # Config row untouched by the failed upload.
        assert repo.get_emisor(conn, row["id"])["razon_social"] == "NUEVA SA"
    finally:
        assert service.seed_backup is not None
        service.seed_backup.shutdown()


def test_retry_on_launch_and_manual_trigger(profile_conn):
    profile, conn = profile_conn
    attempts = {"n": 0}

    def runner() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise S3SeedPermanentError("transient outage")
        return "s3://ok"

    coord = _coord(profile, conn, runner=runner, debounce_s=60.0)
    try:
        mark_seed_backup_pending(conn, "ui_config")
        state = coord.retry_if_pending()
        assert state is not None
        assert state.status is SeedBackupStatus.FAILED
        assert attempts["n"] == 1

        state = coord.backup_now()
        assert state.status is SeedBackupStatus.OK
        assert attempts["n"] == 2
        assert coord.retry_if_pending() is None  # ok → no retry
    finally:
        coord.shutdown()


def test_service_triggers_on_config_events_not_non_default_client(profile_conn):
    profile, conn = profile_conn
    reasons: list[str] = []

    class Recording(SeedBackupCoordinator):
        def notify_config_changed(self, reason: str):
            reasons.append(reason)
            return super().notify_config_changed(reason)

    coord = Recording(
        conn,
        profile.paths,
        environment="homo",
        run_backup=lambda: "ok",
        debounce_s=60.0,
    )
    # Minimal wsfex stub not used by these methods.
    service = InvoiceService(
        Config(env=ArcaEnvironment.HOMO, paths=profile.paths),
        conn,
        wsfex=object(),  # type: ignore[arg-type]
        seed_backup=coord,
    )
    seed_params(conn)
    try:
        service.create_emisor(
            EmisorCreateIn(
                razon_social="OTRO",
                domicilio="X",
                iibb="Exento",
                inicio_actividades="01/01/2020",
                puntos_venta=[2],
            )
        )
        service.update_backup_settings(
            BackupSettingsIn(backup_s3_bucket="b2", backup_s3_prefix="p2")
        )
        service.create_client(
            ClientIn(
                razon_social="NO DEFAULT",
                pais_dst=225,
                cuit_pais=55000002002,
                is_default=False,
            )
        )
        service.create_client(
            ClientIn(
                razon_social="DEFAULT",
                pais_dst=225,
                cuit_pais=55000002002,
                is_default=True,
            )
        )
        coord.replace_recipients(AGE_KEY_A + "\n")
        assert "emisor" in reasons
        assert "ui_config" in reasons
        assert "default_client" in reasons
        assert "recipients" in reasons
        # Non-default client must not trigger.
        assert reasons.count("default_client") == 1
    finally:
        coord.shutdown()


def test_authorize_does_not_trigger_seed_backup(
    test_cert_and_key, tmp_path
):
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "auth-profile"
    )
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    conn = db.connect(profile.paths.db)
    seed_params(conn)
    seed_settings(conn)
    write_recipients_file(profile.paths, AGE_KEY_A + "\n")
    repo.save_settings(
        conn,
        {"backup_s3_bucket": "b", "backup_s3_prefix": "p"},
    )

    reasons: list[str] = []

    class Recording(SeedBackupCoordinator):
        def notify_config_changed(self, reason: str):
            reasons.append(reason)
            return super().notify_config_changed(reason)

    coord = Recording(
        conn,
        profile.paths,
        environment="homo",
        run_backup=lambda: "ok",
        debounce_s=60.0,
    )
    arca = FakeArca()
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    service = InvoiceService(config, conn, wsfex, seed_backup=coord)
    repo.create_client(
        conn,
        {
            "razon_social": "CLIENTE",
            "domicilio": "",
            "pais_dst": 225,
            "cuit_pais": 55000002002,
            "id_impositivo": "",
            "moneda_default": "DOL",
            "incoterms_default": "",
            "idioma_default": 1,
            "forma_pago_default": "WIRE",
            "descripcion_default": "Servicios",
            "is_default": 1,
        },
    )
    try:
        inv = service.create_invoice(
            InvoiceCreate(imp_total=Decimal("100.00"), descripcion="Servicios")
        )
        # create_invoice is not a seed trigger.
        assert reasons == []
        service.authorize(inv["id"])
        assert reasons == []
        assert load_seed_backup_state(conn).status is SeedBackupStatus.IDLE
    finally:
        coord.shutdown()


def test_run_seed_backup_uploads_to_memory_store(profile_conn):
    """Integration with FAC-44/45 stubs when age is available."""
    import shutil

    if shutil.which("age") is None:
        pytest.skip("age not installed")
    profile, conn = profile_conn
    store = MemoryObjectStore()
    uri = run_seed_backup(
        conn, profile.paths, environment="homo", store=store
    )
    assert uri is not None
    assert uri.startswith("s3://test-bucket/")
    assert any(k.endswith("seed.age") for (_, k) in store.objects)
    assert any(k.endswith("recipients.txt") for (_, k) in store.objects)


def test_api_status_and_manual_backup(test_cert_and_key, tmp_path):
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "api-profile"
    )
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    conn = db.connect(profile.paths.db)
    seed_params(conn)
    seed_settings(conn)
    write_recipients_file(profile.paths, AGE_KEY_A + "\n")
    repo.save_settings(
        conn, {"backup_s3_bucket": "bucket", "backup_s3_prefix": "pfx"}
    )

    calls = {"n": 0}

    def runner() -> str:
        calls["n"] += 1
        return "s3://bucket/pfx/seed.age"

    coord = SeedBackupCoordinator(
        conn,
        profile.paths,
        environment="homo",
        run_backup=runner,
        debounce_s=60.0,
    )
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    arca = FakeArca()
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(
        profile, config=config, conn=conn, wsfex=wsfex, seed_backup=coord
    )
    with TestClient(app, base_url=loopback_base_url()) as client:
        r = client.get("/backup/seed")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in {"idle", "pending", "ok", "failed"}

        mark_seed_backup_pending(conn, "ui_config")
        r = client.post("/backup/seed")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert calls["n"] >= 1


def test_debounce_timer_coalesces_to_single_run(profile_conn):
    profile, conn = profile_conn
    barrier = threading.Event()
    calls: list[float] = []

    def runner() -> str:
        calls.append(time.monotonic())
        barrier.set()
        return "ok"

    coord = _coord(profile, conn, runner=runner, debounce_s=0.05)
    try:
        coord.notify_config_changed("emisor")
        coord.notify_config_changed("emisor")
        coord.notify_config_changed("ui_config")
        assert barrier.wait(timeout=2.0)
        # Allow a short grace period; still only one run.
        time.sleep(0.1)
        assert len(calls) == 1
        assert load_seed_backup_state(conn).status is SeedBackupStatus.OK
    finally:
        coord.shutdown()


def test_without_recipients_stays_pending_without_scheduling(profile_conn):
    profile, conn = profile_conn
    recipients_path(profile.paths).unlink()
    calls: list[str] = []
    coord = _coord(
        profile, conn, runner=lambda: calls.append("x") or "ok", debounce_s=0.01
    )
    try:
        state = coord.notify_config_changed("emisor")
        assert state.status is SeedBackupStatus.PENDING
        time.sleep(0.05)
        assert calls == []
    finally:
        coord.shutdown()


def test_protected_settings_reject_backup_state_keys(profile_conn):
    _, conn = profile_conn
    with pytest.raises(ValueError, match="identidad fiscal|seed_backup"):
        repo.save_settings(conn, {"seed_backup_status": "ok"})


def test_backup_worker_uses_dedicated_db_connection(profile_conn):
    """Codex: never share the request-thread SQLite connection with the worker."""
    profile, conn = profile_conn
    opened: list[sqlite3.Connection] = []

    def connect_factory() -> sqlite3.Connection:
        worker = db.connect(profile.paths.db)
        opened.append(worker)
        return worker

    seen_conn: list[object] = []

    def tracking_run(worker_conn, paths, *, environment, store=None):
        seen_conn.append(worker_conn)
        assert worker_conn is not conn
        return "s3://ok"

    coord = SeedBackupCoordinator(
        conn,
        profile.paths,
        environment="homo",
        debounce_s=60.0,
        connect=connect_factory,
    )
    try:
        with patch(
            "facturador.seed_backup_sync.run_seed_backup", tracking_run
        ):
            state = coord.backup_now()
        assert state.status is SeedBackupStatus.OK
        assert len(opened) == 1
        assert seen_conn == opened
        # Worker connection must be closed after execute.
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        # Request-thread connection still usable.
        assert load_seed_backup_state(conn).status is SeedBackupStatus.OK
    finally:
        coord.shutdown()

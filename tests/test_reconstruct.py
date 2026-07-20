"""FAC-65: rebuild / catch-up from ARCA + seed identity validation."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from facturador.arca.wsfex import CmpNotFoundError, WsfexClient
from facturador.config import Config
from facturador.constants import ArcaEnvironment
from facturador.db import connect
from facturador.fiscal_identity import seal_fiscal_cuit
from facturador.migrations import latest_version
from facturador.profile import EnvironmentProfile
from facturador.reconstruct import (
    ProfileSnapshot,
    ReconstructError,
    ReconstructMode,
    PvTipoTarget,
    profile_snapshot_from_conn,
    reconstruct_register,
)
from facturador.restore import catch_up_register
from facturador.seed_backup import (
    assert_seed_has_no_secrets,
    build_envelope,
    build_manifest,
    seed_checksum,
)
from facturador.seed_import import (
    SeedIdentityError,
    apply_seed,
    assert_profile_ready,
    parse_envelope,
    validate_seed_identity,
)
from facturador.settings import Emisor, Settings, save_settings, set_active_emisor
from facturador.setup import SetupState, reconcile_setup_state
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import EMISOR_PRUEBA, TEST_CUIT, install_test_cert_pair


@pytest.fixture
def profile_wsfex(tmp_path, test_cert_and_key):
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "perfil"
    )
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    conn = connect(profile.paths.db)
    seal_fiscal_cuit(conn, TEST_CUIT)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo", puntos_venta=(1,)),
        ),
    )
    set_active_emisor(
        conn,
        conn.execute("SELECT id FROM emisores LIMIT 1").fetchone()["id"],
    )
    assert reconcile_setup_state(profile, conn) is SetupState.READY
    arca = FakeArca()
    wsfex = WsfexClient(
        Config(env=ArcaEnvironment.HOMO, paths=profile.paths),
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    yield profile, conn, wsfex, arca
    conn.close()


def _snapshot(conn: sqlite3.Connection) -> ProfileSnapshot:
    return profile_snapshot_from_conn(
        conn, environment="homo", cuit_emisor=TEST_CUIT
    )


def _seed_envelope(*, environment: str = "homo", cuit: str = TEST_CUIT) -> dict:
    seed = {
        "schema_version": latest_version(),
        "environment": environment,
        "fiscal_cuit": cuit,
        "emisores": [
            {
                "id": "emisor-seed-1",
                "razon_social": "Emisor Seed SA",
                "domicilio": "Calle 1",
                "iibb": "Exento",
                "inicio_actividades": "01/01/2020",
                "condicion_iva": "IVA Responsable Inscripto",
                "ambiente": environment,
                "puntos_venta": [1],
            }
        ],
        "active_emisor_id": "emisor-seed-1",
        "comprobante_tipos": [19],
        "default_client": {
            "id": "client-seed-1",
            "razon_social": "Buyer LLC",
            "domicilio": "NY",
            "pais_dst": 225,
            "cuit_pais": 55000002002,
            "id_impositivo": "12",
            "moneda_default": "DOL",
            "incoterms_default": "",
            "idioma_default": 1,
            "forma_pago_default": "WIRE",
            "descripcion_default": "Services",
        },
        "ui": {
            "backup_s3_bucket": "",
            "backup_s3_prefix": "facturador",
            "pdf_render_version": 1,
        },
    }
    manifest = build_manifest(seed, device_id="device-test")
    envelope = build_envelope(seed, manifest)
    assert_seed_has_no_secrets(envelope)
    return envelope


def test_cmp_not_found_is_distinct_error(profile_wsfex):
    _profile, _conn, wsfex, arca = profile_wsfex
    arca.last_cmp[(1, 19)] = 0
    with pytest.raises(CmpNotFoundError) as exc:
        wsfex.get_cmp(19, 1, 1)
    assert exc.value.code == "1521"


def test_get_cmp_record_parses_items(profile_wsfex):
    _profile, _conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, imp_total="250.50")
    record = wsfex.get_cmp_record(19, 1, 1)
    assert record.fields["Cae"].startswith("761")
    assert record.fields["Fecha_pago"] == "20260703"
    assert len(record.items) == 1
    assert record.items[0].pro_total_item == "250.50"


def test_full_rebuild_single_transaction(profile_wsfex):
    _profile, conn, wsfex, arca = profile_wsfex
    for n in (1, 2, 3):
        arca.seed_issued(
            tipo=19, pv=1, nro=n, arca_id=100 + n, imp_total=f"{n}00.00"
        )
    conn.execute(
        "INSERT INTO invoices ("
        " id, cbte_tipo, punto_venta, cbte_nro, status, source,"
        " fecha_cbte, fecha_pago, dst_cmp, cliente, cuit_pais_cliente,"
        " moneda_ctz, imp_total, environment, created_at, updated_at"
        ") VALUES ("
        " 'old', 19, 1, 99, 'authorized', 'wsfex',"
        " '20260101', '20260101', 225, 'OLD', 55000002002,"
        " '1', '1', 'homo', '2026-01-01', '2026-01-01')"
    )
    conn.commit()

    report = reconstruct_register(
        conn,
        wsfex,
        mode=ReconstructMode.FULL,
        targets=[PvTipoTarget(1, 19)],
        snapshot=_snapshot(conn),
        retries=1,
        retry_sleep_s=0,
        batch_size=2,
    )
    assert report.inserted == 3
    assert report.gaps == []
    rows = conn.execute(
        "SELECT cbte_nro, cae, imp_total, source, status FROM invoices"
        " ORDER BY cbte_nro"
    ).fetchall()
    assert [r["cbte_nro"] for r in rows] == [1, 2, 3]
    assert all(r["source"] == "wsfex" and r["status"] == "authorized" for r in rows)
    assert (
        conn.execute("SELECT COUNT(*) FROM invoices WHERE id='old'").fetchone()[0]
        == 0
    )


def test_rebuild_records_known_gaps(profile_wsfex):
    _profile, conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=201)
    arca.seed_issued(tipo=19, pv=1, nro=3, arca_id=203)
    arca.last_cmp[(1, 19)] = 3

    report = reconstruct_register(
        conn,
        wsfex,
        mode=ReconstructMode.FULL,
        targets=[PvTipoTarget(1, 19)],
        snapshot=_snapshot(conn),
        retries=1,
        retry_sleep_s=0,
    )
    assert report.inserted == 2
    assert len(report.gaps) == 1
    assert report.gaps[0].cbte_nro == 2
    gap_rows = conn.execute("SELECT cbte_nro FROM registry_gaps").fetchall()
    assert [r["cbte_nro"] for r in gap_rows] == [2]


def test_transient_error_aborts_after_prepare(profile_wsfex):
    """Fetch failure aborts; prepare (clear) may already have committed."""
    _profile, conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=301)
    arca.seed_issued(tipo=19, pv=1, nro=2, arca_id=302)
    arca.get_cmp_mode = "error"
    with pytest.raises(ReconstructError, match="no respondió"):
        reconstruct_register(
            conn,
            wsfex,
            mode=ReconstructMode.FULL,
            targets=[PvTipoTarget(1, 19)],
            snapshot=_snapshot(conn),
            retries=2,
            retry_sleep_s=0,
        )
    # Clear ya corrió; sin inserts → registro vacío (FAC-48 bloquearía).
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0


def test_partial_batch_then_rerun_succeeds(profile_wsfex):
    """Lote 1 commitido + fallo en lote 2 → re-run full deja registro OK."""
    _profile, conn, wsfex, arca = profile_wsfex
    for n in (1, 2, 3, 4):
        arca.seed_issued(tipo=19, pv=1, nro=n, arca_id=700 + n)

    calls = {"n": 0}
    real_get = wsfex.get_cmp_record

    def flaky(tipo, pv, nro):
        calls["n"] += 1
        # Tras 2 fetches exitosos (lote batch_size=2), fallar el 3º.
        if calls["n"] == 3:
            raise httpx.ConnectTimeout("boom mid-rebuild")
        return real_get(tipo, pv, nro)

    wsfex.get_cmp_record = flaky  # type: ignore[method-assign]
    with pytest.raises(ReconstructError):
        reconstruct_register(
            conn,
            wsfex,
            mode=ReconstructMode.FULL,
            targets=[PvTipoTarget(1, 19)],
            snapshot=_snapshot(conn),
            retries=1,
            retry_sleep_s=0,
            batch_size=2,
        )
    # Lote 1 (nros 1–2) quedó; FAC-48 vería local_max=2 < arca_last=4.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE status='authorized'"
        ).fetchone()[0]
        == 2
    )

    wsfex.get_cmp_record = real_get  # type: ignore[method-assign]
    report = reconstruct_register(
        conn,
        wsfex,
        mode=ReconstructMode.FULL,
        targets=[PvTipoTarget(1, 19)],
        snapshot=_snapshot(conn),
        retries=1,
        retry_sleep_s=0,
        batch_size=2,
    )
    assert report.inserted == 4
    nros = [
        r["cbte_nro"]
        for r in conn.execute(
            "SELECT cbte_nro FROM invoices ORDER BY cbte_nro"
        )
    ]
    assert nros == [1, 2, 3, 4]


def test_abort_on_any_gap(profile_wsfex):
    _profile, conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=801)
    arca.seed_issued(tipo=19, pv=1, nro=3, arca_id=803)
    arca.last_cmp[(1, 19)] = 3
    with pytest.raises(ReconstructError, match="abort_on_any_gap"):
        reconstruct_register(
            conn,
            wsfex,
            mode=ReconstructMode.FULL,
            targets=[PvTipoTarget(1, 19)],
            snapshot=_snapshot(conn),
            retries=1,
            retry_sleep_s=0,
            abort_on_any_gap=True,
        )


def test_last_number_gap_fails_final_validation(profile_wsfex):
    """last_CMP=N pero FEXGetCMP(N)→1521: validación final aborta."""
    _profile, conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=901)
    arca.last_cmp[(1, 19)] = 2  # ARCA dice 2, pero 2 no existe
    with pytest.raises(ReconstructError, match="Validación post-rebuild"):
        reconstruct_register(
            conn,
            wsfex,
            mode=ReconstructMode.FULL,
            targets=[PvTipoTarget(1, 19)],
            snapshot=_snapshot(conn),
            retries=1,
            retry_sleep_s=0,
        )


def test_catch_up_appends_only_missing(profile_wsfex):
    profile, conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=401)
    arca.seed_issued(tipo=19, pv=1, nro=2, arca_id=402)
    reconstruct_register(
        conn,
        wsfex,
        mode=ReconstructMode.FULL,
        targets=[PvTipoTarget(1, 19)],
        snapshot=_snapshot(conn),
        retries=1,
        retry_sleep_s=0,
    )
    arca.seed_issued(tipo=19, pv=1, nro=3, arca_id=403)
    arca.seed_issued(tipo=19, pv=1, nro=4, arca_id=404)

    report = catch_up_register(
        profile, wsfex, retries=1, abort_on_any_gap=False
    )
    assert report.mode is ReconstructMode.CATCH_UP
    assert report.inserted == 2
    # Dedicated conn wrote; re-read via fixture conn.
    nros = [
        r["cbte_nro"]
        for r in conn.execute(
            "SELECT cbte_nro FROM invoices WHERE source='wsfex' ORDER BY cbte_nro"
        )
    ]
    assert nros == [1, 2, 3, 4]


def test_identity_rejects_wrong_environment(profile_wsfex):
    profile, _conn, _wsfex, _arca = profile_wsfex
    envelope = _seed_envelope(environment="prod")
    seed, manifest = parse_envelope(envelope)
    with pytest.raises(SeedIdentityError, match="ambiente"):
        validate_seed_identity(seed, manifest, profile)


def test_identity_rejects_wrong_cuit(profile_wsfex):
    profile, _conn, _wsfex, _arca = profile_wsfex
    envelope = _seed_envelope(cuit="20999999991")
    seed, manifest = parse_envelope(envelope)
    with pytest.raises(SeedIdentityError, match="CUIT"):
        validate_seed_identity(seed, manifest, profile)


def test_identity_rejects_seed_vs_sealed_mismatch(profile_wsfex):
    profile, conn, _wsfex, _arca = profile_wsfex
    envelope = _seed_envelope(cuit="20999999991")
    # Bypass cert check path by only comparing sealed via conn after
    # forcing seed cuit that != sealed (cert also rejects first).
    seed, manifest = parse_envelope(envelope)
    with pytest.raises(SeedIdentityError, match="CUIT"):
        validate_seed_identity(seed, manifest, profile, conn=conn)


def test_identity_rejects_bad_checksum(profile_wsfex):
    profile, _conn, _wsfex, _arca = profile_wsfex
    envelope = _seed_envelope()
    envelope["manifest"]["checksum"] = "sha256:" + ("0" * 64)
    seed, manifest = parse_envelope(envelope)
    with pytest.raises(SeedIdentityError, match="checksum"):
        validate_seed_identity(seed, manifest, profile)


def test_identity_rejects_future_schema(profile_wsfex):
    profile, _conn, _wsfex, _arca = profile_wsfex
    envelope = _seed_envelope()
    envelope["seed"]["schema_version"] = latest_version() + 10
    envelope["manifest"]["schema_version"] = latest_version() + 10
    envelope["manifest"]["checksum"] = seed_checksum(envelope["seed"])
    seed, manifest = parse_envelope(envelope)
    with pytest.raises(SeedIdentityError, match="schema_version"):
        validate_seed_identity(seed, manifest, profile)


def test_parse_envelope_missing_seed_key():
    from facturador.seed_backup import SeedBackupError

    with pytest.raises(SeedBackupError, match="falta la clave"):
        parse_envelope(
            {
                "format": "facturador.seed",
                "format_version": 1,
                "manifest": {
                    "schema_version": 1,
                    "device_id": "x",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "checksum": "sha256:" + ("a" * 64),
                },
            }
        )


def test_unsealed_profile_rejected_for_apply(tmp_path, test_cert_and_key):
    """apply_seed sin sello falla en claro (no with-conn commit mid-txn)."""
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "unsealed"
    )
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    conn = connect(profile.paths.db)
    try:
        envelope = _seed_envelope()
        seed, _manifest = parse_envelope(envelope)
        with pytest.raises(SeedIdentityError, match="sellado"):
            apply_seed(conn, seed)
    finally:
        conn.close()


def test_assert_profile_ready_blocks_incomplete(tmp_path, test_cert_and_key):
    profile = EnvironmentProfile.for_testing(
        ArcaEnvironment.HOMO, tmp_path / "incomplete"
    )
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    conn = connect(profile.paths.db)
    try:
        with pytest.raises(SeedIdentityError, match="setup"):
            assert_profile_ready(profile, conn)
    finally:
        conn.close()


def test_restore_apply_seed_then_rebuild(profile_wsfex):
    """Validate + apply_seed + rebuild in batched writes."""
    profile, conn, wsfex, arca = profile_wsfex
    envelope = _seed_envelope()
    seed, manifest = parse_envelope(envelope)
    validate_seed_identity(seed, manifest, profile, conn=conn)
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=501, cliente="From ARCA")

    report = reconstruct_register(
        conn,
        wsfex,
        mode=ReconstructMode.FULL,
        targets=[PvTipoTarget(1, 19)],
        snapshot=ProfileSnapshot(
            environment="homo",
            cuit_emisor=TEST_CUIT,
            emisor_id="emisor-seed-1",
            emisor_razon_social="Emisor Seed SA",
            emisor_domicilio="Calle 1",
            emisor_condicion_iva="IVA Responsable Inscripto",
            emisor_iibb="Exento",
            emisor_inicio_actividades="01/01/2020",
        ),
        retries=1,
        retry_sleep_s=0,
        on_begin=lambda c: apply_seed(c, seed),
    )
    assert report.inserted == 1
    emisor = conn.execute(
        "SELECT razon_social FROM emisores WHERE id='emisor-seed-1'"
    ).fetchone()
    assert emisor["razon_social"] == "Emisor Seed SA"
    inv = conn.execute("SELECT cliente, cae FROM invoices").fetchone()
    assert inv["cliente"] == "From ARCA"


def test_launcher_restore_refuses_when_lock_held(
    profile_wsfex, tmp_path, monkeypatch
):
    from facturador.constants import ArcaEnvironment
    from facturador.launcher.lock import ProfileLock
    from facturador.launcher.restore_flow import run_launcher_restore

    profile, _conn, _wsfex, _arca = profile_wsfex
    lock = ProfileLock(profile.paths.launcher_lock)
    lock.acquire(port=8399, environment="homo")
    try:
        monkeypatch.setattr(
            "facturador.launcher.restore_flow.EnvironmentProfile.resolve",
            lambda env, app_data_root=None: profile,
        )
        code = run_launcher_restore(
            ArcaEnvironment.HOMO,
            identity_path=tmp_path / "missing-identity.txt",
        )
        assert code == 1
    finally:
        lock.release()


def test_api_catch_up_unblocks_issuance(api, arca):
    """FAC-48 remediation: catch-up then authorize succeeds."""
    from tests.test_api import _crear_cliente, _crear_draft

    _crear_cliente(api)
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=601)
    arca.seed_issued(tipo=19, pv=1, nro=2, arca_id=602)
    arca.last_cmp[(1, 19)] = 2

    draft = _crear_draft(api)
    blocked = api.post(f"/invoices/{draft['id']}/authorize")
    assert blocked.status_code == 409
    assert "desactualizado" in blocked.json()["detail"]
    assert "Sincronizar desde ARCA" in blocked.json()["detail"]

    caught = api.post("/registry/catch-up")
    assert caught.status_code == 200, caught.text
    body = caught.json()
    assert body["inserted"] == 2

    ok = api.post(f"/invoices/{draft['id']}/authorize")
    assert ok.status_code == 200, ok.text
    assert ok.json()["cbte_nro"] == 3

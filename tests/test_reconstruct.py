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
    PvTipoTarget,
    ReconstructError,
    ReconstructMode,
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
    parse_envelope,
    validate_seed_identity,
)
from facturador.settings import Emisor, Settings, save_settings, set_active_emisor
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


def test_transient_error_aborts_and_rolls_back(profile_wsfex):
    _profile, conn, wsfex, arca = profile_wsfex
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=301)
    arca.seed_issued(tipo=19, pv=1, nro=2, arca_id=302)
    before = conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"]

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
    after = conn.execute("SELECT COUNT(*) AS n FROM invoices").fetchone()["n"]
    assert after == before


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
        conn, wsfex, profile, retries=1, abort_on_any_gap=False
    )
    assert report.mode is ReconstructMode.CATCH_UP
    assert report.inserted == 2
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


def test_restore_apply_seed_then_rebuild(profile_wsfex):
    """Validate + apply_seed + rebuild in one transaction."""
    profile, conn, wsfex, arca = profile_wsfex
    envelope = _seed_envelope()
    seed, manifest = parse_envelope(envelope)
    validate_seed_identity(seed, manifest, profile)
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

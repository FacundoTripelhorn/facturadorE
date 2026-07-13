"""FAC-39: un CUIT fiscal por perfil (sello, snapshot, rechazo de mismatch)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from facturador import db, repo
from facturador.arca.wsfex import WsfexClient
from facturador.certs import store_certificate_pair
from facturador.config import Config
from facturador.constants import ArcaEnvironment
from facturador.fiscal_identity import (
    FISCAL_CUIT_KEY,
    FiscalIdentityError,
    get_sealed_fiscal_cuit,
    resolve_profile_fiscal_cuit,
    seal_fiscal_cuit,
)
from facturador.profile import EnvironmentProfile
from facturador.schemas import EmisorCreateIn, InvoiceCreate
from facturador.service import ConflictError, InvoiceService
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import TEST_CUIT, seed_params, seed_settings
from tests.test_certs import OTHER_CUIT, VALID_CUIT, _build_pair

CLIENTE = {
    "razon_social": "CLIENTE SA",
    "domicilio": "Montevideo",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "UY123",
    "moneda_default": "DOL",
    "incoterms_default": "",
    "idioma_default": 1,
    "forma_pago_default": "WIRE",
    "descripcion_default": "Servicio",
    "is_default": True,
}


@pytest.fixture
def profile(tmp_path: Path) -> EnvironmentProfile:
    return EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "profile")


@pytest.fixture
def conn(profile: EnvironmentProfile):
    profile.paths.ensure_layout()
    connection = db.connect(profile.paths.db)
    yield connection
    connection.close()


def test_seal_fiscal_cuit_idempotente_y_rechaza_otro(conn):
    assert get_sealed_fiscal_cuit(conn) is None
    assert seal_fiscal_cuit(conn, VALID_CUIT) == VALID_CUIT
    assert get_sealed_fiscal_cuit(conn) == VALID_CUIT
    assert seal_fiscal_cuit(conn, VALID_CUIT) == VALID_CUIT
    with pytest.raises(FiscalIdentityError, match="perfil nuevo|resetear"):
        seal_fiscal_cuit(conn, OTHER_CUIT)
    assert get_sealed_fiscal_cuit(conn) == VALID_CUIT


def test_save_settings_no_puede_sobrescribir_fiscal_cuit(conn):
    seal_fiscal_cuit(conn, VALID_CUIT)
    with pytest.raises(ValueError, match="identidad fiscal"):
        repo.save_settings(conn, {FISCAL_CUIT_KEY: OTHER_CUIT})
    assert get_sealed_fiscal_cuit(conn) == VALID_CUIT


def test_resolve_sella_desde_certificado(profile: EnvironmentProfile, conn):
    cert_pem, key_pem, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, cert_pem, key_pem)
    assert get_sealed_fiscal_cuit(conn) is None
    resolved = resolve_profile_fiscal_cuit(conn, profile)
    assert resolved == VALID_CUIT
    assert get_sealed_fiscal_cuit(conn) == VALID_CUIT


def test_resolve_detecta_cert_distinto_del_sello(profile: EnvironmentProfile, conn):
    seal_fiscal_cuit(conn, VALID_CUIT)
    # Instalar a mano un cert de otro CUIT (bypass store) para simular
    # inconsistencia; resolve debe fallar en lugar de emitir con el sello.
    cert_pem, key_pem, _ = _build_pair(cuit=OTHER_CUIT)
    profile.paths.ensure_layout()
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    with pytest.raises(FiscalIdentityError, match="sellado al CUIT"):
        resolve_profile_fiscal_cuit(conn, profile)


def _service(profile: EnvironmentProfile, conn) -> InvoiceService:
    cert_pem, key_pem, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, cert_pem, key_pem)
    seed_params(conn)
    seed_settings(conn)
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(FakeArca().handler)),
    )
    return InvoiceService(config, conn, wsfex)


def test_create_invoice_snapshotea_cuit_y_sella(profile: EnvironmentProfile, conn):
    service = _service(profile, conn)
    assert get_sealed_fiscal_cuit(conn) is None
    client = repo.create_client(conn, CLIENTE)
    inv = service.create_invoice(
        InvoiceCreate(imp_total=Decimal("100.00"), client_id=client["id"])
    )
    assert inv["cuit_emisor"] == VALID_CUIT
    assert get_sealed_fiscal_cuit(conn) == VALID_CUIT
    assert service.wsfex.cuit == int(VALID_CUIT)


def test_create_invoice_usa_sello_coincidente(profile: EnvironmentProfile, conn):
    seal_fiscal_cuit(conn, VALID_CUIT)
    service = _service(profile, conn)
    client = repo.create_client(conn, CLIENTE)
    inv = service.create_invoice(
        InvoiceCreate(imp_total=Decimal("50.00"), client_id=client["id"])
    )
    assert inv["cuit_emisor"] == VALID_CUIT


def test_emisor_no_puede_cambiar_identidad_fiscal(profile: EnvironmentProfile, conn):
    """Alta/edición de emisor no toca fiscal_cuit ni el CUIT de ARCA."""
    service = _service(profile, conn)
    seal_fiscal_cuit(conn, VALID_CUIT)
    before = get_sealed_fiscal_cuit(conn)
    service.create_emisor(
        EmisorCreateIn(
            razon_social="OTRO EMISOR SA",
            domicilio="Otra calle 1",
            iibb="Exento",
            inicio_actividades="01/01/2020",
            puntos_venta=[1],
        )
    )
    assert get_sealed_fiscal_cuit(conn) == before
    assert service.wsfex.cuit == int(VALID_CUIT)
    with pytest.raises(ValidationError):
        EmisorCreateIn(
            razon_social="X",
            domicilio="Y",
            iibb="Z",
            inicio_actividades="01/01/2020",
            cuit=OTHER_CUIT,  # type: ignore[call-arg]
        )


def test_mismatch_sello_bloquea_emision(profile: EnvironmentProfile, conn):
    seal_fiscal_cuit(conn, OTHER_CUIT)
    cert_pem, key_pem, _ = _build_pair(cuit=VALID_CUIT)
    # store rechazaría sealed mismatch; instalamos el cert a mano.
    profile.paths.ensure_layout()
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    seed_params(conn)
    seed_settings(conn)
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(FakeArca().handler)),
    )
    service = InvoiceService(config, conn, wsfex)
    client = repo.create_client(conn, CLIENTE)
    with pytest.raises(ConflictError, match="sellado al CUIT"):
        service.create_invoice(
            InvoiceCreate(imp_total=Decimal("10.00"), client_id=client["id"])
        )


def test_authorize_rechaza_si_identidad_diverge_del_snapshot(
    profile: EnvironmentProfile, conn
):
    """Authorize revalida el CUIT antes de WSFEX (swap manual del cert)."""
    service = _service(profile, conn)
    client = repo.create_client(conn, CLIENTE)
    inv = service.create_invoice(
        InvoiceCreate(imp_total=Decimal("80.00"), client_id=client["id"])
    )
    assert inv["cuit_emisor"] == VALID_CUIT

    # Bypass store: reemplazar el par vivo con otro CUIT (el sello sigue).
    other_cert, other_key, _ = _build_pair(cuit=OTHER_CUIT)
    profile.paths.cert.write_bytes(other_cert)
    profile.paths.key.chmod(0o600)
    profile.paths.key.write_bytes(other_key)
    profile.paths.key.chmod(0o400)
    # Invalidar cache de CUIT del cliente WSFEX.
    service.wsfex._cuit = None  # noqa: SLF001 — test del guard de authorize

    with pytest.raises(ConflictError, match="sellado al CUIT|CUIT"):
        service.authorize(inv["id"], force_desync=True)

    # No debe haber tocado ARCA ni dejado la factura en submitting.
    reloaded = repo.get_invoice(conn, inv["id"])
    assert reloaded is not None
    assert reloaded["status"] == "draft"
    assert reloaded["raw_request"] is None


def test_create_invoice_no_llama_arca_si_identidad_diverge(
    profile: EnvironmentProfile, conn, monkeypatch: pytest.MonkeyPatch
):
    """Identidad se resuelve antes de get_param/get_ctz (Auth con CUIT)."""
    seal_fiscal_cuit(conn, OTHER_CUIT)
    cert_pem, key_pem, _ = _build_pair(cuit=VALID_CUIT)
    profile.paths.ensure_layout()
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    seed_params(conn)
    seed_settings(conn)
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(FakeArca().handler)),
    )
    service = InvoiceService(config, conn, wsfex)
    repo.create_client(conn, CLIENTE)

    def boom(*_a, **_k):
        raise AssertionError("WSFEX no debe llamarse con identidad divergente")

    monkeypatch.setattr(service.wsfex, "get_ctz", boom)
    monkeypatch.setattr(service.wsfex, "get_param", boom)

    with pytest.raises(ConflictError, match="sellado al CUIT"):
        service.create_invoice(InvoiceCreate(imp_total=Decimal("10.00")))


def test_reconcile_rechaza_si_identidad_diverge(profile: EnvironmentProfile, conn):
    """Lazy FEXGetCMP también exige coincidencia snapshot/perfil."""
    from facturador.constants import InvoiceStatus

    service = _service(profile, conn)
    client = repo.create_client(conn, CLIENTE)
    inv = service.create_invoice(
        InvoiceCreate(imp_total=Decimal("90.00"), client_id=client["id"])
    )
    # Simular unknown con request persistido (como post-timeout).
    repo.update_invoice(
        conn,
        inv["id"],
        status=InvoiceStatus.UNKNOWN,
        arca_id=1,
        cbte_nro=1,
        raw_request=(
            '{"arca_id":1,"cbte_tipo":19,"punto_venta":1,"cbte_nro":1,'
            '"fecha_cbte":"20260101","fecha_pago":"20260101","tipo_expo":2,'
            '"permiso_existente":"","dst_cmp":225,"cliente":"X",'
            '"cuit_pais_cliente":55000002002,"domicilio_cliente":"",'
            '"id_impositivo":"","moneda_id":"DOL","moneda_ctz":"1000",'
            '"incoterms":"","incoterms_ds":"","forma_pago":"WIRE",'
            '"idioma_cbte":1,"imp_total":"90.00","obs":"","items":[]}'
        ),
    )

    other_cert, other_key, _ = _build_pair(cuit=OTHER_CUIT)
    profile.paths.cert.write_bytes(other_cert)
    profile.paths.key.chmod(0o600)
    profile.paths.key.write_bytes(other_key)
    profile.paths.key.chmod(0o400)
    service.wsfex._cuit = None  # noqa: SLF001

    with pytest.raises(ConflictError, match="sellado al CUIT|CUIT"):
        service.get_invoice(inv["id"], reconcile=True)

    reloaded = repo.get_invoice(conn, inv["id"])
    assert reloaded is not None
    assert reloaded["status"] == InvoiceStatus.UNKNOWN
    assert reloaded["cae"] is None


def test_baseline_incluye_cuit_emisor(tmp_path: Path):
    """Sin migración de upgrade: la columna vive en el baseline (no hay DBs viejas)."""
    conn = db.connect(tmp_path / "fresh.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        assert "cuit_emisor" in cols
    finally:
        conn.close()


def test_api_invoice_incluye_cuit_emisor(api):
    """Contrato REST: el draft lleva el CUIT snapshot del perfil."""
    r = api.post(
        "/clients",
        json={
            "razon_social": "CLIENTE URUGUAY S.A.",
            "domicilio": "Montevideo",
            "pais_dst": 225,
            "cuit_pais": 55000002002,
            "id_impositivo": "UY-1",
            "is_default": True,
            "descripcion_default": "Servicios",
        },
    )
    assert r.status_code == 201
    client_id = r.json()["id"]
    inv = api.post(
        "/invoices",
        json={"imp_total": "123.45", "client_id": client_id},
    )
    assert inv.status_code == 201
    body = inv.json()
    assert body["cuit_emisor"] == TEST_CUIT
    sealed = api.conn.execute(
        "SELECT value FROM settings WHERE key = ?", (FISCAL_CUIT_KEY,)
    ).fetchone()
    assert sealed is not None
    assert sealed[0] == TEST_CUIT

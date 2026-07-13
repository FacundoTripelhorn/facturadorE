"""Setup state por perfil y guardia de onboarding (FAC-35)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db, repo
from facturador.api import create_app
from facturador.api.localhost_policy import loopback_base_url
from facturador.api.setup_guard import requires_ready_profile
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config, load_config
from facturador.constants import ArcaEnvironment
from facturador.profile import EnvironmentProfile
from facturador.settings import Emisor, Settings, save_settings, set_active_emisor
from facturador.setup import (
    SetupState,
    evaluate_setup_state,
    load_setup_state,
    make_setup_state_provider,
    reconcile_setup_state,
    save_setup_state,
)
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import EMISOR_PRUEBA, seed_params, seed_settings


def _client_for_profile(
    profile: EnvironmentProfile, *, seed: bool = True
) -> TestClient:
    config = load_config(profile)
    conn = db.connect(config.paths.db)
    if seed:
        seed_params(conn)
        seed_settings(conn, ambiente=profile.environment.value)
    arca = FakeArca()
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(profile, config=config, conn=conn, wsfex=wsfex)
    client = TestClient(app, base_url=loopback_base_url())
    client.conn = conn
    return client


def test_make_setup_state_provider_refleja_hechos_sin_reinicio(
    tmp_path, test_cert_and_key
):
    """El provider de la guardia re-reconcilia: certs/emisor mid-process."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    conn = db.connect(profile.paths.db)
    get_state = make_setup_state_provider(profile, conn)

    assert get_state() is SetupState.CERTIFICATE_REQUIRED

    cert_pem, key_pem = test_cert_and_key
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    assert get_state() is SetupState.EMISOR_REQUIRED
    assert load_setup_state(profile.paths) is SetupState.EMISOR_REQUIRED

    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    assert get_state() is SetupState.READY


def test_load_setup_state_sin_archivo_es_uninitialized(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    assert load_setup_state(profile.paths) is SetupState.UNINITIALIZED


def test_reconcile_persiste_y_reinicio_retoma_paso(tmp_path, test_cert_and_key):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    conn = db.connect(profile.paths.db)

    assert reconcile_setup_state(profile, conn) is SetupState.CERTIFICATE_REQUIRED
    assert load_setup_state(profile.paths) is SetupState.CERTIFICATE_REQUIRED

    cert_pem, key_pem = test_cert_and_key
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    assert reconcile_setup_state(profile, conn) is SetupState.EMISOR_REQUIRED

    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    assert reconcile_setup_state(profile, conn) is SetupState.READY

    # Reinicio: solo lee el archivo + hechos; mismo paso.
    assert load_setup_state(profile.paths) is SetupState.READY
    assert evaluate_setup_state(profile, conn) is SetupState.READY


def test_point_of_sale_required_sin_puntos_venta(tmp_path, test_cert_and_key):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    conn = db.connect(profile.paths.db)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo", puntos_venta=())
        ),
    )
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    assert evaluate_setup_state(profile, conn) is SetupState.POINT_OF_SALE_REQUIRED


def test_homo_y_prod_guardan_setup_independiente(tmp_path, test_cert_and_key):
    homo = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "homo")
    prod = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "prod")
    for profile in (homo, prod):
        profile.paths.ensure_layout()
        db.connect(profile.paths.db).close()

    save_setup_state(homo.paths, SetupState.CERTIFICATE_REQUIRED)
    save_setup_state(prod.paths, SetupState.READY)

    assert load_setup_state(homo.paths) is SetupState.CERTIFICATE_REQUIRED
    assert load_setup_state(prod.paths) is SetupState.READY
    assert homo.paths.onboarding.resolve() != prod.paths.onboarding.resolve()
    assert SetupState.READY.value not in homo.paths.onboarding.read_text(
        encoding="utf-8"
    )


def test_health_y_setup_disponibles_sin_ready(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    setup = client.get("/setup")
    assert setup.status_code == 200
    body = setup.json()
    assert body["state"] == SetupState.CERTIFICATE_REQUIRED.value
    assert body["ready"] is False
    assert body["environment"] == "homo"


def test_factura_y_arca_bloqueados_hasta_ready(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)

    create = client.post(
        "/invoices",
        json={
            "imp_total": "100.00",
            "descripcion": "servicio",
        },
    )
    assert create.status_code == 503
    assert create.json()["setup_state"] == SetupState.CERTIFICATE_REQUIRED.value

    assert client.get("/").status_code == 503
    assert client.get("/health/arca").status_code == 503
    assert client.get("/params/moneda").status_code == 503


def test_factura_permitida_cuando_ready(api):
    """El fixture ``api`` siembra certs + emisor completo → ready."""
    assert api.get("/setup").json()["ready"] is True
    # create_invoice exige cliente/códigos; acá solo importa que no sea 503.
    listed = api.get("/invoices")
    assert listed.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "blocked"),
    [
        ("GET", "/health", False),
        ("GET", "/setup", False),
        ("GET", "/configuracion", False),
        ("GET", "/static/htmx.min.js", False),
        ("GET", "/", True),
        ("POST", "/ui/facturas", True),
        ("POST", "/ui/facturas/abc/authorize", True),
        ("GET", "/invoices", True),
        ("GET", "/health/arca", True),
        ("GET", "/params/moneda", True),
        ("GET", "/clientes", False),
    ],
)
def test_requires_ready_matrix(method, path, blocked):
    assert requires_ready_profile(method, path) is blocked


def test_config_sin_certs_carga_ok(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    config = load_config(profile)
    assert isinstance(config, Config)
    assert config.env is ArcaEnvironment.PROD

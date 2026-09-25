"""Setup state por perfil y guardia de onboarding."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db, repo
from facturador.api import create_app
from facturador.api.localhost_policy import loopback_base_url
from facturador.api.setup_guard import is_setup_exempt, requires_ready_profile
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config, load_config
from facturador.constants import ArcaEnvironment
from facturador.fiscal_identity import seal_fiscal_cuit
from facturador.profile import EnvironmentProfile
from facturador.settings import Emisor, Settings, save_settings, set_active_emisor
from facturador.setup import (
    SetupError,
    SetupState,
    evaluate_setup_state,
    load_setup_state,
    make_setup_state_provider,
    reconcile_setup_state,
    save_setup_state,
)
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import (
    EMISOR_PRUEBA,
    install_test_cert_pair,
    seed_params,
    seed_settings,
    with_csrf,
)
from tests.test_certs import OTHER_CUIT, VALID_CUIT, _build_pair

_CLIENTE_FORM = {
    "razon_social": "CLIENTE URUGUAY S.A.",
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": "225",
    "cuit_pais": "55000002002",
    "id_impositivo": "RUT 219999830019",
    "moneda_default": "DOL",
    "idioma_default": "1",
    "forma_pago_default": "WIRE TRANSFER",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": "true",
}


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
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    assert get_state() is SetupState.EMISOR_REQUIRED
    assert load_setup_state(profile.paths) is SetupState.EMISOR_REQUIRED

    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    assert get_state() is SetupState.READY


def test_load_setup_state_sin_archivo_es_uninitialized(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    assert load_setup_state(profile.paths) is SetupState.UNINITIALIZED


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("{not-json", "No se pudo leer"),
        ("42", "objeto JSON"),
        ('{"state": 1}', "inválido"),
        ('{"state": true}', "inválido"),
        ('{"state": {"nested": true}}', "inválido"),
        ('{"state": "bad_state"}', "inválido"),
    ],
)
def test_load_setup_state_malformed_json(tmp_path, payload, match):
    """Edge case: onboarding.json corrupto debe fallar con SetupError claro."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    profile.paths.onboarding.write_text(payload, encoding="utf-8")
    with pytest.raises(SetupError, match=match):
        load_setup_state(profile.paths)


def test_reconcile_recupera_onboarding_corrupto(tmp_path):
    """Edge case: reconcile no 500; re-deriva hechos y reescribe el archivo."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    profile.paths.onboarding.write_text("{not-json", encoding="utf-8")
    conn = db.connect(profile.paths.db)

    state = reconcile_setup_state(profile, conn)
    assert state is SetupState.CERTIFICATE_REQUIRED
    assert load_setup_state(profile.paths) is SetupState.CERTIFICATE_REQUIRED


def test_reconcile_persiste_y_reinicio_retoma_paso(tmp_path, test_cert_and_key):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    conn = db.connect(profile.paths.db)

    assert reconcile_setup_state(profile, conn) is SetupState.CERTIFICATE_REQUIRED
    assert load_setup_state(profile.paths) is SetupState.CERTIFICATE_REQUIRED

    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
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
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
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

    setup = client.get("/setup/status")
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
    assert create.json()["setup_url"] == "/setup"

    # UI: el launcher abre ``/`` → redirect al onboarding (no JSON 503 opaco).
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == (
        f"/setup?desde={SetupState.CERTIFICATE_REQUIRED.value}"
    )
    assert client.get("/health/arca").status_code == 503
    assert client.get("/params/moneda").status_code == 503


def test_factura_permitida_cuando_ready(api):
    """El fixture ``api`` siembra certs + emisor completo → ready."""
    assert api.get("/setup/status").json()["ready"] is True
    # create_invoice exige cliente/códigos; acá solo importa que no sea 503.
    listed = api.get("/invoices")
    assert listed.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "blocked"),
    [
        ("GET", "/health", False),
        ("GET", "/setup/status", False),
        ("GET", "/setup", False),
        ("GET", "/configuracion", False),
        ("GET", "/static/htmx.min.js", False),
        ("GET", "/", True),
        ("POST", "/ui/facturas", True),
        ("POST", "/ui/facturas/abc/authorize", True),
        ("GET", "/facturas/abc", True),
        ("GET", "/facturas/abc/revisar", True),
        ("GET", "/invoices", True),
        ("GET", "/health/arca", True),
        ("GET", "/params/moneda", True),
        ("GET", "/constatacion", True),
        ("POST", "/ui/constatacion", True),
        ("GET", "/clientes", False),
        ("GET", "/clients", False),
        ("POST", "/ui/clientes", False),
    ],
)
def test_requires_ready_matrix(method, path, blocked):
    assert requires_ready_profile(method, path) is blocked


def test_detalle_html_bloqueado_hasta_ready(tmp_path):
    """Codex review: GET /facturas/{id} reconcilia UNKNOWN y no debe tocar ARCA."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)
    r = client.get("/facturas/any-id", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/setup?desde={SetupState.CERTIFICATE_REQUIRED.value}"
    )


def test_ui_sin_ready_redirige_a_setup(tmp_path):
    """Home y formularios HTML van a /setup; APIs JSON siguen en 503."""
    from facturador.api.setup_guard import is_html_ui_route

    assert is_html_ui_route("GET", "/") is True
    assert is_html_ui_route("GET", "/facturas/x") is True
    assert is_html_ui_route("POST", "/ui/facturas") is True
    assert is_html_ui_route("GET", "/invoices") is False
    assert is_html_ui_route("GET", "/params/moneda") is False

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)
    landed = client.get("/", follow_redirects=True)
    assert landed.status_code == 200
    assert "Configuración inicial" in landed.text
    assert "certificado" in landed.text.lower()


def test_clientes_disponibles_con_params_cacheados_sin_ready(tmp_path):
    """Codex: clientes offline con arca_params sembrados, sin exigir ready."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)
    seed_params(client.conn)
    assert client.get("/clients").status_code == 200
    assert client.get("/clientes").status_code == 200


def test_clientes_con_params_stale_sin_certs_usa_cache(tmp_path):
    """Cache >24h sin certs: get_params cae al stale, selects poblados."""
    import datetime as dt

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)
    seed_params(client.conn)
    stale = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)).isoformat()
    client.conn.execute("UPDATE arca_params SET fetched_at = ?", (stale,))
    client.conn.commit()

    r = client.get("/clientes")
    assert r.status_code == 200
    assert "URUGUAY" in r.text
    assert 'class="panel error"' not in r.text

    created = client.post(
        "/ui/clientes",
        data=with_csrf(client, _CLIENTE_FORM),
        follow_redirects=False,
    )
    assert created.status_code == 303


def test_clientes_sin_params_ni_certs_muestra_error_no_500(tmp_path):
    """Sin cache de params ni certs: página con error, no 500."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)
    r = client.get("/clientes")
    assert r.status_code == 200
    assert 'class="panel error"' in r.text


def test_post_clientes_sin_params_ni_certs_muestra_error_no_500(tmp_path):
    """POST /ui/clientes sin cache/certs: 422 con error, no 500."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)
    r = client.post(
        "/ui/clientes",
        data=with_csrf(client, _CLIENTE_FORM),
    )
    assert r.status_code == 422
    assert 'class="panel error"' in r.text


def test_clientes_y_ui_clientes_son_setup_exempt():
    assert is_setup_exempt("GET", "/clientes") is True
    assert is_setup_exempt("POST", "/ui/clientes") is True
    assert is_setup_exempt("GET", "/invoices") is False


def test_cert_distinto_del_sello_fiscal_queda_en_certificate_required(tmp_path):
    """Sello CUIT A + cert CUIT B no llega a ready."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    conn = db.connect(profile.paths.db)
    seal_fiscal_cuit(conn, OTHER_CUIT)
    cert_pem, key_pem, _ = _build_pair(cuit=VALID_CUIT)
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])

    assert evaluate_setup_state(profile, conn) is SetupState.CERTIFICATE_REQUIRED


def test_key_con_permisos_laxos_no_avanza_setup(tmp_path, test_cert_and_key):
    """Codex review: mid-process con key 644 sigue en certificate_required."""
    import sys

    if sys.platform == "win32":
        pytest.skip("Chequeo de modo 400/600 es POSIX")

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem, key_mode=0o644)
    conn = db.connect(profile.paths.db)
    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])

    assert evaluate_setup_state(profile, conn) is SetupState.CERTIFICATE_REQUIRED
    profile.paths.key.chmod(0o400)
    assert evaluate_setup_state(profile, conn) is SetupState.READY


def test_par_cert_invalido_no_avanza_setup(tmp_path):
    """Codex: PEM basura / desparejado no desbloquea ready."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    profile.paths.ensure_layout()
    profile.paths.cert.write_text("NOT-A-CERT", encoding="utf-8")
    profile.paths.key.write_text("NOT-A-KEY", encoding="utf-8")
    profile.paths.key.chmod(0o400)
    conn = db.connect(profile.paths.db)
    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    assert evaluate_setup_state(profile, conn) is SetupState.CERTIFICATE_REQUIRED


def test_emisor_de_otro_ambiente_no_es_ready(tmp_path, test_cert_and_key):
    """Codex: emisor sellado con otro ambiente no completa el setup."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    conn = db.connect(profile.paths.db)
    save_settings(conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente="prod")))
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])
    assert evaluate_setup_state(profile, conn) is SetupState.EMISOR_REQUIRED


def test_config_sin_certs_carga_ok(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    config = load_config(profile)
    assert isinstance(config, Config)
    assert config.env is ArcaEnvironment.PROD

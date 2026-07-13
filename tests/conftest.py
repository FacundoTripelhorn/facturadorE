"""Fixtures compartidas: certificado autofirmado de prueba (nunca el real),
app FastAPI contra el simulador de ARCA y cache de params sembrado."""

import datetime as dt

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from facturador import db
from facturador.api import create_app
from facturador.api.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from facturador.api.localhost_policy import loopback_base_url
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config
from facturador.constants import ArcaEnvironment
from facturador.profile import EnvironmentProfile
from tests.arca_fake import FakeArca, FakeWsaa

TEST_CUIT = "20111111112"


@pytest.fixture(scope="session")
def test_cert_and_key() -> tuple[bytes, bytes]:
    """Par cert/key autofirmado con el mismo formato de subject que ARCA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "facturador-test"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {TEST_CUIT}"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


@pytest.fixture
def test_profile(tmp_path) -> EnvironmentProfile:
    """Perfil homo aislado en tmp (FAC-24: create_app exige uno explícito)."""
    return EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "profile")


def install_test_cert_pair(
    paths, cert_pem: bytes, key_pem: bytes, *, key_mode: int = 0o400
) -> None:
    """Escribe el par de prueba con permisos de key listos para setup ready."""
    paths.ensure_layout()
    paths.cert.write_bytes(cert_pem)
    paths.key.write_bytes(key_pem)
    paths.key.chmod(key_mode)


@pytest.fixture
def test_config(test_profile, test_cert_and_key) -> Config:
    """Config con TODOS los paths saliendo del perfil de test (FAC-25)."""
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(test_profile.paths, cert_pem, key_pem)
    return Config(env=ArcaEnvironment.HOMO, paths=test_profile.paths)


@pytest.fixture
def arca() -> FakeArca:
    return FakeArca()


@pytest.fixture
def api(test_config, test_profile, arca):
    conn = db.connect(test_profile.paths.db)
    seed_params(conn)
    seed_settings(conn)
    wsfex = WsfexClient(
        test_config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(test_profile, config=test_config, conn=conn, wsfex=wsfex)
    # Host válido (FAC-41): TestClient default "testserver" sería rechazado.
    client = TestClient(app, base_url=loopback_base_url())
    client.conn = conn  # para asserts directos sobre la DB
    return client


def ensure_csrf_cookie(client: TestClient) -> str:
    """Garantiza cookie CSRF (FAC-42) y devuelve el token para forms/tests."""
    token = client.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        client.get("/health")
        token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token, "el middleware CSRF debió emitir facturador_csrf"
    return token


def with_csrf(client: TestClient, data: dict | None = None) -> dict[str, str]:
    """Copia de ``data`` con el campo csrf_token alineado a la cookie."""
    payload = {str(k): str(v) for k, v in (data or {}).items()}
    payload[CSRF_FORM_FIELD] = ensure_csrf_cookie(client)
    return payload


EMISOR_PRUEBA = {
    "razon_social": "MI EMPRESA S.R.L.",
    "domicilio": "Calle Falsa 123, CABA",
    "iibb": "Exento",
    "inicio_actividades": "01/08/2020",
}


def seed_settings(conn, ambiente: str = "homo") -> None:
    """Settings de dominio con el emisor completo y activo (sin él no se
    emite). La selección activa es local al perfil (FAC-26). ``ambiente``
    debe coincidir con el perfil del backend bajo prueba."""
    from facturador.settings import Emisor, Settings, save_settings, set_active_emisor

    save_settings(
        conn, Settings(emisor=Emisor(**EMISOR_PRUEBA, ambiente=ambiente))
    )
    from facturador import repo

    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])


def seed_params(conn) -> None:
    """Cache arca_params fresco con los códigos usados en los tests."""
    from facturador import repo

    repo.replace_params(conn, "moneda", [
        {"code": "DOL", "description": "Dolar Estadounidense"},
        {"code": "PES", "description": "Pesos Argentinos"},
    ])
    repo.replace_params(conn, "pais", [{"code": "225", "description": "URUGUAY"}])
    repo.replace_params(conn, "cuit_pais", [
        {"code": "55000002002", "description": "URUGUAY - Persona Juridica"},
    ])
    repo.replace_params(conn, "idioma", [{"code": "1", "description": "Espanol"}])
    repo.replace_params(conn, "umed", [{"code": "7", "description": "unidades"}])
    repo.replace_params(conn, "cbte_tipo", [
        {"code": "19", "description": "Facturas de Exportacion"},
    ])

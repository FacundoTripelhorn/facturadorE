"""Fixtures compartidas: certificado autofirmado de prueba (nunca el real)."""

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from facturador.config import Config

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
    now = dt.datetime.now(dt.timezone.utc)
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
def test_config(tmp_path, test_cert_and_key) -> Config:
    cert_pem, key_pem = test_cert_and_key
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "homo.crt").write_bytes(cert_pem)
    (secrets / "homo.key").write_bytes(key_pem)
    (tmp_path / "data").mkdir()
    return Config(env="homo", home=tmp_path, cuit=None, key_passphrase=None)

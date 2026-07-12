"""FAC-36: validación y persistencia atómica del par certificado/clave ARCA."""

from __future__ import annotations

import datetime as dt
import stat
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from facturador.certs import (
    CertificateError,
    CertificateMetadata,
    load_certificate_metadata,
    read_live_certificate_pair,
    store_certificate_pair,
    validate_certificate_pair,
)
from facturador.constants import ArcaEnvironment
from facturador.profile import EnvironmentProfile

VALID_CUIT = "20111111112"
OTHER_CUIT = "27999999993"


def _build_pair(
    *,
    cuit: str = VALID_CUIT,
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
    include_cuit_serial: bool = True,
    serial_number: str | None = None,
    key: rsa.RSAPrivateKey | None = None,
) -> tuple[bytes, bytes, rsa.RSAPrivateKey]:
    """Construye un par PEM autofirmado con el formato de subject de ARCA."""
    private_key = key or rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test"),
        x509.NameAttribute(NameOID.COMMON_NAME, "facturador-test"),
    ]
    if include_cuit_serial:
        attrs.append(
            x509.NameAttribute(
                NameOID.SERIAL_NUMBER,
                serial_number if serial_number is not None else f"CUIT {cuit}",
            )
        )
    subject = x509.Name(attrs)
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (now - dt.timedelta(days=1)))
        .not_valid_after(not_after or (now + dt.timedelta(days=365)))
        .sign(private_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem, private_key


@pytest.fixture
def profile(tmp_path: Path) -> EnvironmentProfile:
    return EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "profile")


# --- validación ---


def test_par_valido_devuelve_metadata_segura(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair()
    meta = validate_certificate_pair(
        cert_pem, key_pem, environment=profile.environment
    )

    assert isinstance(meta, CertificateMetadata)
    assert meta.cuit == VALID_CUIT
    assert "CUIT" in meta.subject or VALID_CUIT in meta.subject
    assert meta.environment is ArcaEnvironment.HOMO
    assert meta.not_valid_before < meta.not_valid_after
    dumped = repr(meta)
    assert "BEGIN PRIVATE KEY" not in dumped
    assert key_pem.decode() not in dumped


def test_certificado_malformado_rechazado(profile: EnvironmentProfile):
    _, key_pem, _ = _build_pair()
    with pytest.raises(CertificateError, match="certificado") as exc_info:
        validate_certificate_pair(
            b"not-a-certificate", key_pem, environment=profile.environment
        )
    assert "BEGIN PRIVATE KEY" not in str(exc_info.value)
    assert key_pem.decode() not in str(exc_info.value)


def test_clave_malformada_rechazada(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair()
    with pytest.raises(CertificateError, match="clave privada") as exc_info:
        validate_certificate_pair(
            cert_pem, b"not-a-key", environment=profile.environment
        )
    assert key_pem.decode() not in str(exc_info.value)
    assert "BEGIN PRIVATE KEY" not in str(exc_info.value)


def test_par_desparejado_rechazado(profile: EnvironmentProfile):
    cert_pem, _, _ = _build_pair(cuit=VALID_CUIT)
    _, other_key_pem, _ = _build_pair(cuit=OTHER_CUIT)
    with pytest.raises(CertificateError, match="no corresponde") as exc_info:
        validate_certificate_pair(
            cert_pem, other_key_pem, environment=profile.environment
        )
    assert "BEGIN PRIVATE KEY" not in str(exc_info.value)
    assert other_key_pem.decode() not in str(exc_info.value)


def test_sin_cuit_en_subject_rechazado(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair(include_cuit_serial=False)
    with pytest.raises(CertificateError, match="serialNumber=CUIT"):
        validate_certificate_pair(
            cert_pem, key_pem, environment=profile.environment
        )


def test_serial_number_sin_prefijo_cuit_rechazado(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair(serial_number=VALID_CUIT)
    with pytest.raises(CertificateError, match="serialNumber=CUIT"):
        validate_certificate_pair(
            cert_pem, key_pem, environment=profile.environment
        )


def test_certificado_vencido_rechazado(profile: EnvironmentProfile):
    now = dt.datetime.now(dt.UTC)
    cert_pem, key_pem, _ = _build_pair(
        not_before=now - dt.timedelta(days=40),
        not_after=now - dt.timedelta(days=10),
    )
    with pytest.raises(CertificateError, match="vencido"):
        validate_certificate_pair(
            cert_pem, key_pem, environment=profile.environment, now=now
        )


def test_certificado_aun_no_vigente_rechazado(profile: EnvironmentProfile):
    now = dt.datetime.now(dt.UTC)
    cert_pem, key_pem, _ = _build_pair(
        not_before=now + dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=400),
    )
    with pytest.raises(CertificateError, match="todavía no es válido"):
        validate_certificate_pair(
            cert_pem, key_pem, environment=profile.environment, now=now
        )


def test_clave_con_passphrase_rechazada(profile: EnvironmentProfile):
    cert_pem, _, private_key = _build_pair()
    encrypted = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secreto"),
    )
    with pytest.raises(CertificateError, match="passphrase") as exc_info:
        validate_certificate_pair(
            cert_pem, encrypted, environment=profile.environment
        )
    assert "secreto" not in str(exc_info.value)
    assert encrypted.decode(errors="replace") not in str(exc_info.value)


def test_clave_ed25519_rechazada(profile: EnvironmentProfile):
    """WSAA solo firma CMS con RSA/EC: no persistir algoritmos incompatibles."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private_key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
            x509.NameAttribute(NameOID.COMMON_NAME, "ed25519-test"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"CUIT {VALID_CUIT}"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(private_key, None)
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(CertificateError, match="RSA o EC"):
        validate_certificate_pair(
            cert_pem, key_pem, environment=profile.environment
        )
    with pytest.raises(CertificateError, match="RSA o EC"):
        store_certificate_pair(profile, cert_pem, key_pem)
    assert not profile.paths.cert.exists()
    assert not profile.paths.key.exists()


# --- persistencia atómica ---


def test_store_persiste_nombres_genericos_y_metadata(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair()
    meta = store_certificate_pair(profile, cert_pem, key_pem)

    assert profile.paths.cert.is_file()
    assert profile.paths.key.is_file()
    assert profile.paths.cert.name == "cert.crt"
    assert profile.paths.key.name == "cert.key"
    assert profile.paths.cert.read_bytes() == cert_pem
    assert profile.paths.key.read_bytes() == key_pem
    assert meta.cuit == VALID_CUIT
    assert meta.environment is ArcaEnvironment.HOMO

    loaded = load_certificate_metadata(profile)
    assert loaded is not None
    assert loaded.cuit == meta.cuit
    assert loaded.subject == meta.subject


@pytest.mark.skipif(sys.platform == "win32", reason="permisos POSIX")
def test_store_aplica_permisos_restrictivos(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair()
    store_certificate_pair(profile, cert_pem, key_pem)

    secrets_mode = stat.S_IMODE(profile.paths.secrets_dir.stat().st_mode)
    cert_mode = stat.S_IMODE(profile.paths.cert.stat().st_mode)
    key_mode = stat.S_IMODE(profile.paths.key.stat().st_mode)
    assert secrets_mode == 0o700
    assert cert_mode == 0o644
    assert key_mode == 0o400
    assert key_mode & 0o077 == 0


def test_store_rechaza_antes_de_reemplazar_par_vivo(profile: EnvironmentProfile):
    good_cert, good_key, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, good_cert, good_key)
    before_cert = profile.paths.cert.read_bytes()
    before_key = profile.paths.key.read_bytes()

    bad_cert, _, _ = _build_pair(cuit=OTHER_CUIT)
    _, other_key, _ = _build_pair(cuit=VALID_CUIT)
    with pytest.raises(CertificateError, match="no corresponde"):
        store_certificate_pair(profile, bad_cert, other_key)

    assert profile.paths.cert.read_bytes() == before_cert
    assert profile.paths.key.read_bytes() == before_key
    # No deben quedar temporales sueltos.
    leftovers = list(profile.paths.secrets_dir.glob(".*"))
    assert leftovers == []


def test_store_preserva_par_previo_si_falla_el_replace(
    profile: EnvironmentProfile, monkeypatch: pytest.MonkeyPatch
):
    good_cert, good_key, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, good_cert, good_key)

    # Mismo CUIT, par distinto: la identidad fiscal lo permite; el replace falla.
    new_cert, new_key, _ = _build_pair(cuit=VALID_CUIT)
    import os as os_module

    real_replace = os_module.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if Path(dst).name == "cert.key":
            raise OSError("simulated failure writing key")
        return real_replace(src, dst)

    monkeypatch.setattr("facturador.certs.os.replace", flaky_replace)

    with pytest.raises(CertificateError, match="No se pudo guardar"):
        store_certificate_pair(profile, new_cert, new_key)

    assert profile.paths.cert.read_bytes() == good_cert
    assert profile.paths.key.read_bytes() == good_key
    assert calls["n"] >= 2
    leftovers = list(profile.paths.secrets_dir.glob(".*"))
    assert leftovers == []


def test_store_usa_temps_unicos_por_llamada(
    profile: EnvironmentProfile, monkeypatch: pytest.MonkeyPatch
):
    """Review: temps fijos (.cert.crt.tmp) colisionan entre stores concurrentes."""
    cert_pem, key_pem, _ = _build_pair()
    seen: list[str] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(name)
        return fd, name

    monkeypatch.setattr("facturador.certs.tempfile.mkstemp", tracking_mkstemp)
    store_certificate_pair(profile, cert_pem, key_pem)
    store_certificate_pair(profile, cert_pem, key_pem)

    assert len(seen) == 4  # 2 temps × 2 stores
    assert len(set(seen)) == 4
    assert not any(name.endswith(".cert.crt.tmp") for name in seen)
    assert not any(name.endswith(".cert.key.tmp") for name in seen)


def test_stores_concurrentes_dejan_par_consistente(profile: EnvironmentProfile):
    """Lock + temps únicos: el par vivo nunca queda cert A + key B.

    FAC-39: todos los pares comparten el mismo CUIT (renovación permitida).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pairs = [
        _build_pair(cuit=VALID_CUIT),
        _build_pair(cuit=VALID_CUIT),
        _build_pair(cuit=VALID_CUIT),
        _build_pair(cuit=VALID_CUIT),
    ]

    def _store(pair: tuple[bytes, bytes, object]) -> CertificateMetadata:
        cert_pem, key_pem, _ = pair
        return store_certificate_pair(profile, cert_pem, key_pem)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_store, pair) for pair in pairs]
        for fut in as_completed(futures):
            fut.result()

    live_cert = profile.paths.cert.read_bytes()
    live_key = profile.paths.key.read_bytes()
    # Debe ser exactamente uno de los pares enviados (no un cruce).
    assert (live_cert, live_key) in {(c, k) for c, k, _ in pairs}
    validate_certificate_pair(
        live_cert, live_key, environment=profile.environment
    )
    leftovers = list(profile.paths.secrets_dir.glob(".*"))
    assert leftovers == []


def test_store_invalida_cache_de_ta_wsaa(profile: EnvironmentProfile):
    """Tras rotar el cert, un TA viejo no debe reutilizarse con el CUIT nuevo."""
    profile.paths.ensure_layout()
    profile.paths.data_dir.mkdir(exist_ok=True)
    cache = profile.paths.wsaa_ta_cache
    cache.write_text(
        '{"token":"old","sign":"old","service":"wsfex",'
        '"environment":"homo","generation":"2026-01-01T00:00:00+00:00",'
        '"expiration":"2099-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )
    assert cache.is_file()

    cert_pem, key_pem, _ = _build_pair()
    store_certificate_pair(profile, cert_pem, key_pem)
    assert not cache.exists()


def test_store_fallido_no_borra_cache_de_ta(
    profile: EnvironmentProfile, monkeypatch: pytest.MonkeyPatch
):
    """Si el replace falla y se restaura el par previo, el TA sigue válido."""
    good_cert, good_key, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, good_cert, good_key)

    cache = profile.paths.wsaa_ta_cache
    cache.write_text("{}", encoding="utf-8")

    new_cert, new_key, _ = _build_pair(cuit=VALID_CUIT)
    import os as os_module

    real_replace = os_module.replace

    def flaky_replace(src, dst):
        if Path(dst).name == "cert.key":
            raise OSError("simulated failure writing key")
        return real_replace(src, dst)

    monkeypatch.setattr("facturador.certs.os.replace", flaky_replace)
    with pytest.raises(CertificateError, match="No se pudo guardar"):
        store_certificate_pair(profile, new_cert, new_key)

    assert cache.is_file()
    assert cache.read_text(encoding="utf-8") == "{}"


def test_reader_espera_par_completo_bajo_lock(
    profile: EnvironmentProfile, monkeypatch: pytest.MonkeyPatch
):
    """Un reader concurrente no observa cert nuevo + key vieja a mitad del store."""
    import threading

    old_cert, old_key, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, old_cert, old_key)
    new_cert, new_key, _ = _build_pair(cuit=VALID_CUIT)

    holding = threading.Event()
    release = threading.Event()
    reader_entered = threading.Event()
    reader_finished = threading.Event()
    seen: list[tuple[bytes, bytes]] = []
    errors: list[BaseException] = []

    import os as os_module

    real_replace = os_module.replace

    def gated_replace(src, dst):
        result = real_replace(src, dst)
        if Path(dst).name == "cert.crt":
            holding.set()
            assert release.wait(timeout=2.0)
        return result

    monkeypatch.setattr("facturador.certs.os.replace", gated_replace)

    def writer() -> None:
        try:
            store_certificate_pair(profile, new_cert, new_key)
        except BaseException as exc:  # noqa: BLE001 — recolectar para el assert
            errors.append(exc)

    def reader() -> None:
        try:
            assert holding.wait(timeout=2.0)
            reader_entered.set()
            seen.append(read_live_certificate_pair(profile.paths))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            reader_finished.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    reader_thread.start()

    assert holding.wait(timeout=2.0)
    assert reader_entered.wait(timeout=2.0)
    # Sigue bloqueado en el lock mientras el store no terminó el segundo replace.
    assert not reader_finished.wait(timeout=0.2)
    release.set()
    writer_thread.join(timeout=2.0)
    reader_thread.join(timeout=2.0)
    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert errors == []
    assert seen == [(new_cert, new_key)]
    assert (new_cert, old_key) not in seen
    assert (old_cert, new_key) not in seen


def test_store_rechaza_certificado_con_otro_cuit(profile: EnvironmentProfile):
    """FAC-39: renovar con otro CUIT no muta el perfil."""
    good_cert, good_key, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, good_cert, good_key)
    before_cert = profile.paths.cert.read_bytes()
    before_key = profile.paths.key.read_bytes()

    new_cert, new_key, _ = _build_pair(cuit=OTHER_CUIT)
    with pytest.raises(CertificateError, match="perfil nuevo|resetear"):
        store_certificate_pair(profile, new_cert, new_key)

    assert profile.paths.cert.read_bytes() == before_cert
    assert profile.paths.key.read_bytes() == before_key


def test_store_permite_renovar_mismo_cuit(profile: EnvironmentProfile):
    """FAC-39: rotación de par con el mismo CUIT está permitida."""
    old_cert, old_key, _ = _build_pair(cuit=VALID_CUIT)
    store_certificate_pair(profile, old_cert, old_key)
    new_cert, new_key, _ = _build_pair(cuit=VALID_CUIT)
    meta = store_certificate_pair(profile, new_cert, new_key)
    assert meta.cuit == VALID_CUIT
    assert profile.paths.cert.read_bytes() == new_cert
    assert profile.paths.key.read_bytes() == new_key


def test_store_rechaza_cuit_distinto_del_sello(profile: EnvironmentProfile):
    """FAC-39: el sello en settings también bloquea otro CUIT (sin cert vivo)."""
    new_cert, new_key, _ = _build_pair(cuit=OTHER_CUIT)
    with pytest.raises(CertificateError, match="sellado al CUIT"):
        store_certificate_pair(
            profile, new_cert, new_key, sealed_cuit=VALID_CUIT
        )
    assert not profile.paths.cert.exists()
    assert not profile.paths.key.exists()


def test_store_en_perfil_prod_marca_ambiente_prod(tmp_path: Path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "prod")
    cert_pem, key_pem, _ = _build_pair()
    meta = store_certificate_pair(profile, cert_pem, key_pem)
    assert meta.environment is ArcaEnvironment.PROD
    assert load_certificate_metadata(profile) is not None


def test_load_sin_certificado_devuelve_none(profile: EnvironmentProfile):
    profile.paths.ensure_layout()
    assert load_certificate_metadata(profile) is None


def test_errores_nunca_incluyen_material_de_clave(profile: EnvironmentProfile):
    cert_pem, key_pem, _ = _build_pair()
    # Inyectar un fragmento único de la key en un PEM roto para detectar fugas.
    marker = b"MARKER_PRIVATE_MATERIAL_9f3a"
    broken = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + marker
        + b"\n-----END PRIVATE KEY-----\n"
    )
    with pytest.raises(CertificateError) as exc_info:
        validate_certificate_pair(
            cert_pem, broken, environment=profile.environment
        )
    message = str(exc_info.value)
    assert "MARKER_PRIVATE_MATERIAL_9f3a" not in message
    assert key_pem.decode() not in message
    assert "BEGIN PRIVATE KEY" not in message

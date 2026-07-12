"""Validación y persistencia atómica del par certificado/clave ARCA (FAC-36).

Servicio de backend sin UI: valida el par PEM, lo escribe en el ``secrets/``
del perfil con nombres genéricos (``cert.crt`` / ``cert.key``) y permisos
restrictivos, y solo expone metadata segura (CUIT, subject, vigencia,
ambiente). El contenido de la clave privada nunca entra en logs, respuestas
ni mensajes de error.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID

from .constants import ArcaEnvironment
from .profile import CERT_FILENAME, KEY_FILENAME, EnvironmentProfile, ProfilePaths

# Subject ARCA: serialNumber=CUIT <11 dígitos> (así lo emite WSASS / portal).
_CUIT_IN_SUBJECT = re.compile(r"CUIT\s+(\d{11})", re.IGNORECASE)

_CERT_MODE = 0o644
_KEY_MODE = 0o400
_SECRETS_DIR_MODE = 0o700

# Serializa escritura del par vivo Y lectura conjunta (WSAA firma CMS):
# sin esto un reader puede ver cert nuevo + key vieja entre los dos replace.
_STORE_LOCK = threading.Lock()


class CertificateError(ValueError):
    """Par certificado/clave inválido. El mensaje nunca incluye material PEM."""


@dataclass(frozen=True)
class CertificateMetadata:
    """Metadata segura del certificado instalado o validado."""

    cuit: str
    subject: str
    not_valid_before: dt.datetime
    not_valid_after: dt.datetime
    environment: ArcaEnvironment

    def __repr__(self) -> str:
        return (
            "CertificateMetadata("
            f"cuit={self.cuit!r}, "
            f"subject={self.subject!r}, "
            f"not_valid_before={self.not_valid_before.isoformat()!r}, "
            f"not_valid_after={self.not_valid_after.isoformat()!r}, "
            f"environment={self.environment.value!r})"
        )


def validate_certificate_pair(
    cert_pem: bytes | str,
    key_pem: bytes | str,
    *,
    environment: ArcaEnvironment,
    now: dt.datetime | None = None,
) -> CertificateMetadata:
    """Valida el par y devuelve metadata segura. No escribe archivos."""
    cert_bytes = _as_pem_bytes(cert_pem, label="certificado")
    key_bytes = _as_pem_bytes(key_pem, label="clave privada")

    cert = _load_certificate(cert_bytes)
    key = _load_private_key(key_bytes)
    _assert_supported_key_type(key)
    _assert_key_matches_certificate(cert, key)

    cuit = _cuit_from_subject(cert)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    _assert_validity_window(not_before, not_after, now=now)

    return CertificateMetadata(
        cuit=cuit,
        subject=cert.subject.rfc4514_string(),
        not_valid_before=not_before,
        not_valid_after=not_after,
        environment=environment,
    )


def store_certificate_pair(
    profile: EnvironmentProfile,
    cert_pem: bytes | str,
    key_pem: bytes | str,
    *,
    now: dt.datetime | None = None,
    sealed_cuit: str | None = None,
) -> CertificateMetadata:
    """Valida y persiste el par en el perfil de forma atómica.

    Si la validación falla, no toca los archivos vivos. Si la escritura falla
    a mitad de camino, restaura el par anterior (si existía). Temps por
    llamada + lock evitan cruces ante stores concurrentes. Tras un replace
    exitoso se invalida el cache de TA de WSAA del perfil.

    Identidad fiscal (FAC-39): un CUIT distinto al certificado vivo o al
    sello ``sealed_cuit`` se rechaza sin mutar el perfil. Renovar el par
    con el mismo CUIT está permitido.
    """
    # Import diferido: fiscal_identity importa load_certificate_metadata de acá.
    from .fiscal_identity import assert_certificate_matches_profile

    cert_bytes = _as_pem_bytes(cert_pem, label="certificado")
    key_bytes = _as_pem_bytes(key_pem, label="clave privada")
    metadata = validate_certificate_pair(
        cert_bytes,
        key_bytes,
        environment=profile.environment,
        now=now,
    )
    assert_certificate_matches_profile(
        profile, metadata.cuit, sealed_cuit=sealed_cuit
    )

    with _STORE_LOCK:
        # Releer bajo lock: otro hilo pudo instalar un CUIT distinto entre
        # la validación y el replace.
        assert_certificate_matches_profile(
            profile, metadata.cuit, sealed_cuit=sealed_cuit
        )
        _persist_validated_pair(profile, cert_bytes, key_bytes)
        # El TA cacheado fue firmado/emitido para el cert anterior; WSFEX
        # arma Auth con el CUIT del cert actual → hay que descartarlo.
        _invalidate_wsaa_ta_cache(profile)
    return metadata


def _persist_validated_pair(
    profile: EnvironmentProfile,
    cert_bytes: bytes,
    key_bytes: bytes,
) -> None:
    """Escribe el par ya validado. Caller debe sostener ``_STORE_LOCK``."""
    paths = profile.paths
    paths.ensure_layout()
    _restrict_secrets_dir(paths.secrets_dir)

    previous_cert = _read_if_file(paths.cert)
    previous_key = _read_if_file(paths.key)
    previous_cert_mode = _mode_if_file(paths.cert)
    previous_key_mode = _mode_if_file(paths.key)

    cert_tmp = _make_private_temp(paths.secrets_dir, CERT_FILENAME)
    key_tmp = _make_private_temp(paths.secrets_dir, KEY_FILENAME)
    cert_replaced = False

    try:
        _write_bytes_restricted(cert_tmp, cert_bytes, _CERT_MODE)
        _write_bytes_restricted(key_tmp, key_bytes, _KEY_MODE)
        os.replace(cert_tmp, paths.cert)
        cert_replaced = True
        os.replace(key_tmp, paths.key)
    except OSError as exc:
        if cert_replaced:
            _restore_previous(
                paths.cert,
                previous_cert,
                previous_cert_mode,
                default_mode=_CERT_MODE,
            )
            _restore_previous(
                paths.key,
                previous_key,
                previous_key_mode,
                default_mode=_KEY_MODE,
            )
        raise CertificateError(
            "No se pudo guardar el par certificado/clave en el perfil."
        ) from exc
    finally:
        cert_tmp.unlink(missing_ok=True)
        key_tmp.unlink(missing_ok=True)

    # Reforzar permisos finales (replace puede heredar umask del FS).
    _chmod_if_posix(paths.cert, _CERT_MODE)
    _chmod_if_posix(paths.key, _KEY_MODE)


def _make_private_temp(secrets_dir: Path, final_name: str) -> Path:
    """Temp único en ``secrets_dir`` (evita colisiones entre llamadas)."""
    fd, name = tempfile.mkstemp(
        prefix=f".{final_name}.",
        suffix=".tmp",
        dir=secrets_dir,
    )
    os.close(fd)
    return Path(name)


def _invalidate_wsaa_ta_cache(profile: EnvironmentProfile) -> None:
    """Borra ``ta-wsfex.json`` del perfil tras rotar el par cert/key."""
    cache = profile.paths.wsaa_ta_cache
    try:
        cache.unlink(missing_ok=True)
    except OSError as exc:
        raise CertificateError(
            "El par se guardó pero no se pudo invalidar el cache de TA de WSAA."
        ) from exc


def read_live_certificate_pair(paths: ProfilePaths) -> tuple[bytes, bytes]:
    """Lee cert+key bajo el mismo lock que ``store_certificate_pair``.

    Evita que un reader (p.ej. WSAA al firmar el TRA) observe un par a
    medias mientras otro hilo reemplaza los archivos vivos.
    """
    with _STORE_LOCK:
        if not paths.cert.is_file() or not paths.key.is_file():
            raise CertificateError(
                "Falta el par certificado/clave en el perfil."
            )
        try:
            return paths.cert.read_bytes(), paths.key.read_bytes()
        except OSError as exc:
            raise CertificateError(
                "No se pudo leer el par certificado/clave del perfil."
            ) from exc


def load_certificate_metadata(
    profile: EnvironmentProfile,
) -> CertificateMetadata | None:
    """Lee metadata del certificado instalado (sin tocar la clave privada).

    Devuelve ``None`` si aún no hay certificado. Si el archivo existe pero no
    es un PEM ARCA parseable, levanta ``CertificateError``. No revalida la
    ventana de vigencia: sirve para mostrar metadata (incluido vencido).
    """
    cert_path = profile.paths.cert
    if not cert_path.is_file():
        return None
    try:
        cert_bytes = cert_path.read_bytes()
    except OSError as exc:
        raise CertificateError(
            "No se pudo leer el certificado del perfil."
        ) from exc
    cert = _load_certificate(cert_bytes)
    cuit = _cuit_from_subject(cert)
    return CertificateMetadata(
        cuit=cuit,
        subject=cert.subject.rfc4514_string(),
        not_valid_before=cert.not_valid_before_utc,
        not_valid_after=cert.not_valid_after_utc,
        environment=profile.environment,
    )


def _as_pem_bytes(value: bytes | str, *, label: str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not value or not value.strip():
        raise CertificateError(f"El {label} está vacío.")
    return value


def _load_certificate(cert_pem: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        raise CertificateError(
            "El certificado no es un PEM X.509 válido."
        ) from exc


def _load_private_key(key_pem: bytes):
    try:
        return load_pem_private_key(key_pem, password=None)
    except TypeError as exc:
        # cryptography pide password cuando la key está cifrada.
        raise CertificateError(
            "La clave privada no debe tener passphrase."
        ) from exc
    except ValueError as exc:
        raise CertificateError(
            "La clave privada no es un PEM válido."
        ) from exc


def _assert_supported_key_type(key) -> None:
    # Misma restricción que sign_tra_cms (WSAA): solo RSA o EC firman el CMS.
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise CertificateError(
            "La clave privada debe ser RSA o EC (requerido para firmar el CMS de WSAA)."
        )


def _assert_key_matches_certificate(cert: x509.Certificate, key) -> None:
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_pub != key_pub:
        raise CertificateError(
            "La clave privada no corresponde al certificado."
        )


def _cuit_from_subject(cert: x509.Certificate) -> str:
    attrs = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
    for attr in attrs:
        match = _CUIT_IN_SUBJECT.search(str(attr.value))
        if match is not None:
            return match.group(1)
    raise CertificateError(
        "El subject del certificado debe incluir serialNumber=CUIT "
        "<11 dígitos> (formato ARCA)."
    )


def _assert_validity_window(
    not_before: dt.datetime,
    not_after: dt.datetime,
    *,
    now: dt.datetime | None,
) -> None:
    instant = now or dt.datetime.now(dt.UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.UTC)
    else:
        instant = instant.astimezone(dt.UTC)
    if instant < not_before:
        raise CertificateError(
            "El certificado todavía no es válido "
            f"(vigente desde {not_before.date().isoformat()})."
        )
    if instant > not_after:
        raise CertificateError(
            "El certificado está vencido "
            f"(vencía el {not_after.date().isoformat()})."
        )


def _read_if_file(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    return path.read_bytes()


def _mode_if_file(path: Path) -> int | None:
    if sys.platform == "win32" or not path.is_file():
        return None
    return stat.S_IMODE(path.stat().st_mode)


def _write_bytes_restricted(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    _chmod_if_posix(path, mode)


def _chmod_if_posix(path: Path, mode: int) -> None:
    if sys.platform == "win32":
        return
    path.chmod(mode)


def _restrict_secrets_dir(secrets_dir: Path) -> None:
    if sys.platform == "win32":
        return
    secrets_dir.chmod(_SECRETS_DIR_MODE)


def _restore_previous(
    path: Path,
    previous: bytes | None,
    previous_mode: int | None,
    *,
    default_mode: int,
) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    # La key queda en 0400: hace falta poder reescribir antes de restaurar.
    if path.exists() and sys.platform != "win32":
        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
    path.write_bytes(previous)
    _chmod_if_posix(path, previous_mode if previous_mode is not None else default_mode)

"""Seed cifrado del perfil: solo config que ARCA no puede reproducir.

ARCA es el ledger autoritativo. La DB local y los PDFs son copias de
conveniencia regenerables (rebuild desde ARCA / cache de PDF). El backup que
sale de la máquina es un seed casi estático de kilobytes: emisores, CUIT
fiscal, ambiente, PV/tipos, cliente default / UI, versión de esquema, más
un manifiesto de sanidad. Sin DB, sin secretos, sin PDFs, sin datos por
comprobante, sin ``last_CMP``.

Cifrado: ``age`` a **todas** las claves públicas listadas en el
``recipients.txt`` del perfil (una por máquina). Layout lógico de objeto
(S3): ``{prefix}/{cuit}/{env}/seed.age`` (overwrite) y
``recipients.txt`` en claro al lado. Este módulo arma y cifra el seed;
no sube a S3.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import repo
from .constants import (
    CBTE_TIPO_FACTURA_E,
    CBTE_TIPO_NOTA_CREDITO_E,
    CBTE_TIPO_NOTA_DEBITO_E,
)
from .fiscal_identity import get_sealed_fiscal_cuit
from .migrations import current_version, latest_version
from .profile import ProfilePaths
from .settings import (
    ACTIVE_EMISOR_KEY,
    BACKUP_PREFIX_DEFAULT,
    CONDICION_IVA_DEFAULT,
)

SEED_FORMAT = "facturador.seed"
SEED_FORMAT_VERSION = 1
SEED_FILENAME = "seed.age"
RECIPIENTS_FILENAME = "recipients.txt"
DEVICE_ID_FILENAME = "device_id"

# Tipos de comprobante que el producto modela hoy (constants.py). El set
# viaja en el seed para saber qué re-consultar en ARCA tras un rebuild.
DEFAULT_COMPROBANTE_TIPOS: tuple[int, ...] = (
    CBTE_TIPO_FACTURA_E,
    CBTE_TIPO_NOTA_DEBITO_E,
    CBTE_TIPO_NOTA_CREDITO_E,
)

# Default del schema / renderer v1; no importamos el stack PDF acá.
DEFAULT_PDF_RENDER_VERSION = 1

_AGE_RECIPIENT_PREFIX = "age1"


class SeedBackupError(RuntimeError):
    """Error al armar, cifrar o persistir el seed del perfil."""


def seed_object_key(prefix: str, cuit: str, env: str) -> str:
    """Clave S3 fija del seed (overwrite). El upload va a este path."""
    return _object_key(prefix, cuit, env, SEED_FILENAME)


def recipients_object_key(prefix: str, cuit: str, env: str) -> str:
    """Clave S3 del listado de recipients (claro). El upload lo sincroniza."""
    return _object_key(prefix, cuit, env, RECIPIENTS_FILENAME)


def _object_key(prefix: str, cuit: str, env: str, filename: str) -> str:
    clean_prefix = prefix.strip().strip("/") or BACKUP_PREFIX_DEFAULT
    clean_cuit = cuit.strip()
    clean_env = env.strip().lower()
    if not clean_cuit:
        raise SeedBackupError("CUIT vacío: no se puede armar la clave del seed.")
    if clean_env not in ("homo", "prod"):
        raise SeedBackupError(
            f"Ambiente inválido para la clave del seed: {env!r}"
        )
    return f"{clean_prefix}/{clean_cuit}/{clean_env}/{filename}"


def recipients_path(paths: ProfilePaths) -> Path:
    """``recipients.txt`` local del perfil (staging; S3 lo sincroniza el upload)."""
    return paths.backups_dir / RECIPIENTS_FILENAME


def write_recipients_file(paths: ProfilePaths, text: str) -> Path:
    """Escribe ``recipients.txt`` local (overwrite atómico) y valida el cuerpo.

    Disparo de backup: (``SeedBackupCoordinator.replace_recipients``).
    """
    # Validar antes de tocar disco: parse_recipients exige claves age1…
    body = text if text.endswith("\n") or text == "" else text + "\n"
    parsed = parse_recipients(body)
    if not parsed:
        raise SeedBackupError(
            f"{RECIPIENTS_FILENAME} no lista ninguna clave age. "
            "Sin recipients no se puede cifrar el seed."
        )
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    dest = recipients_path(paths)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(dest)
    return dest


def seed_archive_path(paths: ProfilePaths) -> Path:
    """``seed.age`` local: nombre fijo, se sobrescribe en cada backup."""
    return paths.backups_dir / SEED_FILENAME


def device_id_path(paths: ProfilePaths) -> Path:
    return paths.data_dir / DEVICE_ID_FILENAME


def ensure_device_id(paths: ProfilePaths) -> str:
    """UUID estable por máquina/perfil; se crea la primera vez."""
    path = device_id_path(paths)
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


def parse_recipients(text: str) -> list[str]:
    """Lee recipients estilo age: una clave ``age1…`` por línea; ``#`` comenta."""
    recipients: list[str] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Permitir "comentario al final" solo tras whitespace (no en la clave).
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not line.startswith(_AGE_RECIPIENT_PREFIX) or len(line) < 20:
            raise SeedBackupError(
                f"recipients.txt línea {lineno}: se esperaba una clave "
                f"pública age (age1…), llegó {line[:32]!r}."
            )
        if line not in seen:
            seen.add(line)
            recipients.append(line)
    return recipients


def load_recipients(path: Path) -> list[str]:
    if not path.is_file():
        raise SeedBackupError(
            f"No hay {RECIPIENTS_FILENAME} en {path.parent}. "
            "Agregar al menos una clave pública age (una por máquina)."
        )
    recipients = parse_recipients(path.read_text(encoding="utf-8"))
    if not recipients:
        raise SeedBackupError(
            f"{path} no lista ninguna clave age. "
            "Sin recipients no se puede cifrar el seed."
        )
    return recipients


def _emisor_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        puntos = json.loads(row["puntos_venta"])
    except (TypeError, ValueError):
        puntos = [1]
    if not isinstance(puntos, list):
        puntos = [1]
    clean_pv = [int(v) for v in puntos if isinstance(v, int) and v >= 1]
    return {
        "id": row["id"],
        "razon_social": row["razon_social"] or "",
        "domicilio": row["domicilio"] or "",
        "iibb": row["iibb"] or "",
        "inicio_actividades": row["inicio_actividades"] or "",
        "condicion_iva": row["condicion_iva"] or CONDICION_IVA_DEFAULT,
        "ambiente": row["ambiente"],
        "puntos_venta": clean_pv,
    }


def _default_client_payload(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = repo.get_default_client(conn)
    if row is None:
        return None
    return {
        "id": row["id"],
        "razon_social": row["razon_social"],
        "domicilio": row["domicilio"] or "",
        "pais_dst": int(row["pais_dst"]),
        "cuit_pais": int(row["cuit_pais"]),
        "id_impositivo": row["id_impositivo"] or "",
        "moneda_default": row["moneda_default"] or "DOL",
        "incoterms_default": row["incoterms_default"] or "",
        "idioma_default": int(row["idioma_default"]),
        "forma_pago_default": row["forma_pago_default"] or "",
        "descripcion_default": row["descripcion_default"] or "",
    }


def assemble_seed(
    conn: sqlite3.Connection,
    *,
    environment: str,
) -> dict[str, Any]:
    """Arma el cuerpo del seed (sin manifiesto) desde la DB del perfil.

    No incluye filas de facturas, keys, certificados ni params ARCA.
    """
    env = environment.strip().lower()
    if env not in ("homo", "prod"):
        raise SeedBackupError(f"Ambiente inválido para el seed: {environment!r}")

    schema_version = current_version(conn)
    if schema_version <= 0:
        schema_version = latest_version()

    raw_settings = repo.get_settings(conn)
    emisores = [_emisor_payload(row) for row in repo.list_emisores(conn)]
    active = raw_settings.get(ACTIVE_EMISOR_KEY) or None
    if active and not any(e["id"] == active for e in emisores):
        active = None

    return {
        "schema_version": schema_version,
        "environment": env,
        "fiscal_cuit": get_sealed_fiscal_cuit(conn),
        "emisores": emisores,
        "active_emisor_id": active,
        "comprobante_tipos": list(DEFAULT_COMPROBANTE_TIPOS),
        "default_client": _default_client_payload(conn),
        "ui": {
            "backup_s3_bucket": (raw_settings.get("backup_s3_bucket") or "").strip(),
            "backup_s3_prefix": (
                (raw_settings.get("backup_s3_prefix") or "").strip()
                or BACKUP_PREFIX_DEFAULT
            ),
            "pdf_render_version": DEFAULT_PDF_RENDER_VERSION,
        },
    }


def canonical_seed_bytes(seed: Mapping[str, Any]) -> bytes:
    """JSON canónico del seed para checksum (orden estable, sin spaces)."""
    return json.dumps(
        seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def seed_checksum(seed: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_seed_bytes(seed)).hexdigest()
    return f"sha256:{digest}"


def build_manifest(
    seed: Mapping[str, Any],
    *,
    device_id: str,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Manifiesto de sanidad: sin last_CMP ni contadores de secuencia."""
    ts = timestamp or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return {
        "schema_version": int(seed["schema_version"]),
        "device_id": device_id,
        "timestamp": ts.astimezone(UTC).replace(microsecond=0).isoformat(),
        "checksum": seed_checksum(seed),
    }


def build_envelope(
    seed: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "format": SEED_FORMAT,
        "format_version": SEED_FORMAT_VERSION,
        "seed": dict(seed),
        "manifest": dict(manifest),
    }


def serialize_envelope(envelope: Mapping[str, Any]) -> bytes:
    return json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"


# Forma del envelope (allowlist). Rechaza claves de DB/comprobantes/secretos
# por estructura — no por substring en texto libre del usuario.
_ENVELOPE_KEYS = frozenset({"format", "format_version", "seed", "manifest"})
_SEED_KEYS = frozenset(
    {
        "schema_version",
        "environment",
        "fiscal_cuit",
        "emisores",
        "active_emisor_id",
        "comprobante_tipos",
        "default_client",
        "ui",
    }
)
_EMISOR_KEYS = frozenset(
    {
        "id",
        "razon_social",
        "domicilio",
        "iibb",
        "inicio_actividades",
        "condicion_iva",
        "ambiente",
        "puntos_venta",
    }
)
_CLIENT_KEYS = frozenset(
    {
        "id",
        "razon_social",
        "domicilio",
        "pais_dst",
        "cuit_pais",
        "id_impositivo",
        "moneda_default",
        "incoterms_default",
        "idioma_default",
        "forma_pago_default",
        "descripcion_default",
    }
)
_UI_KEYS = frozenset(
    {"backup_s3_bucket", "backup_s3_prefix", "pdf_render_version"}
)
_MANIFEST_KEYS = frozenset(
    {"schema_version", "device_id", "timestamp", "checksum"}
)

# Solo marcadores de material privado real (PEM / age identity). No listar
# nombres de archivo ni columnas de factura: un domicilio puede contenerlos.
_SECRET_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    "AGE-SECRET-KEY-",
)


def _require_exact_keys(
    obj: Mapping[str, Any], allowed: frozenset[str], *, where: str
) -> None:
    keys = frozenset(obj)
    extra = keys - allowed
    if extra:
        raise SeedBackupError(
            f"El seed tiene claves no permitidas en {where}: "
            f"{', '.join(sorted(extra))}."
        )
    missing = allowed - keys
    if missing:
        raise SeedBackupError(
            f"El seed carece de claves obligatorias en {where}: "
            f"{', '.join(sorted(missing))}."
        )


def _assert_no_secret_markers(value: object, *, where: str) -> None:
    """Recorre valores string buscando PEM/age identity; no escanea substrings
    de nombres de archivo o columnas (texto libre del emisor/cliente)."""
    if isinstance(value, str):
        for marker in _SECRET_MARKERS:
            if marker in value:
                raise SeedBackupError(
                    f"El seed contiene material privado en {where}."
                )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_secret_markers(child, where=f"{where}.{key}")
        return
    if isinstance(value, list):
        for idx, child in enumerate(value):
            _assert_no_secret_markers(child, where=f"{where}[{idx}]")


def assert_seed_has_no_secrets(envelope: Mapping[str, Any]) -> None:
    """Valida forma del envelope y ausencia de material privado real.

    Allowlist de claves (sin invoices/raw_*/secrets) + marcadores PEM/age.
    No rechaza texto libre que mencione p.ej. ``cert.key`` o ``last_cmp``.
    """
    _require_exact_keys(envelope, _ENVELOPE_KEYS, where="envelope")
    seed = envelope["seed"]
    manifest = envelope["manifest"]
    if not isinstance(seed, Mapping) or not isinstance(manifest, Mapping):
        raise SeedBackupError("Envelope inválido: seed/manifest deben ser objetos.")

    _require_exact_keys(seed, _SEED_KEYS, where="seed")
    _require_exact_keys(manifest, _MANIFEST_KEYS, where="manifest")

    emisores = seed["emisores"]
    if not isinstance(emisores, list):
        raise SeedBackupError("seed.emisores debe ser una lista.")
    for idx, emisor in enumerate(emisores):
        if not isinstance(emisor, Mapping):
            raise SeedBackupError(f"seed.emisores[{idx}] inválido.")
        _require_exact_keys(emisor, _EMISOR_KEYS, where=f"seed.emisores[{idx}]")

    client = seed["default_client"]
    if client is not None:
        if not isinstance(client, Mapping):
            raise SeedBackupError("seed.default_client inválido.")
        _require_exact_keys(client, _CLIENT_KEYS, where="seed.default_client")

    ui = seed["ui"]
    if not isinstance(ui, Mapping):
        raise SeedBackupError("seed.ui inválido.")
    _require_exact_keys(ui, _UI_KEYS, where="seed.ui")

    _assert_no_secret_markers(envelope, where="envelope")


def encrypt_to_recipients(
    plaintext: bytes,
    recipients: Sequence[str],
    *,
    age_bin: str = "age",
) -> bytes:
    """Cifra el plaintext a **todas** las claves listadas (age -r …)."""
    if not recipients:
        raise SeedBackupError("Se necesita al menos un recipient age.")
    for recipient in recipients:
        if not recipient.startswith(_AGE_RECIPIENT_PREFIX):
            raise SeedBackupError(
                f"Recipient age inválido: {recipient[:32]!r}"
            )

    import shutil

    if shutil.which(age_bin) is None:
        raise SeedBackupError(
            "No se encontró `age` en el PATH. Instalar: winget install "
            "FiloSottile.age (Windows) / brew install age (macOS)."
        )

    cmd = [age_bin]
    for recipient in recipients:
        cmd.extend(["-r", recipient])
    try:
        completed = subprocess.run(
            cmd,
            input=plaintext,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # stderr de age no debe filtrar plaintext; solo el código.
        raise SeedBackupError(
            f"age falló al cifrar el seed (exit {exc.returncode})."
        ) from exc
    ciphertext = completed.stdout
    if not ciphertext.startswith(b"age-encryption.org/v1"):
        raise SeedBackupError("La salida de age no parece un archivo age v1.")
    return ciphertext


def decrypt_with_identity(
    ciphertext: bytes,
    identity_path: Path,
    *,
    age_bin: str = "age",
) -> bytes:
    """Descifrado de prueba/operador. No loguea el plaintext."""
    import shutil

    if shutil.which(age_bin) is None:
        raise SeedBackupError("No se encontró `age` en el PATH.")
    try:
        completed = subprocess.run(
            [age_bin, "-d", "-i", str(identity_path)],
            input=ciphertext,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SeedBackupError(
            f"age falló al descifrar el seed (exit {exc.returncode})."
        ) from exc
    return completed.stdout


def write_seed_archive(paths: ProfilePaths, ciphertext: bytes) -> Path:
    """Escribe ``backups/seed.age`` con overwrite atómico (temp + replace)."""
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    dest = seed_archive_path(paths)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(ciphertext)
    tmp.chmod(0o600)
    tmp.replace(dest)
    return dest


def create_encrypted_seed(
    conn: sqlite3.Connection,
    paths: ProfilePaths,
    *,
    environment: str,
    recipients_file: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Arma, cifra y persiste el seed local. Devuelve (path, envelope).

    El envelope en claro NO se escribe a disco; solo el ciphertext.
    """
    seed = assemble_seed(conn, environment=environment)
    # Setup incompleto: el seed solo tiene sentido con identidad fiscal +
    # al menos un emisor activo (onboarding casi listo). No inventar un seed a
    # medias ni sugerir workarounds de CLI.
    if not seed.get("fiscal_cuit"):
        raise SeedBackupError(
            "Perfil incompleto: falta el CUIT fiscal sellado. "
            "Completar el setup antes de respaldar."
        )
    if not seed.get("emisores"):
        raise SeedBackupError(
            "Perfil incompleto: no hay emisores configurados. "
            "Completar el setup antes de respaldar."
        )
    if not seed.get("active_emisor_id"):
        raise SeedBackupError(
            "Perfil incompleto: no hay emisor activo. "
            "Completar el setup antes de respaldar."
        )
    device_id = ensure_device_id(paths)
    manifest = build_manifest(seed, device_id=device_id)
    envelope = build_envelope(seed, manifest)
    assert_seed_has_no_secrets(envelope)
    plaintext = serialize_envelope(envelope)
    recipients = load_recipients(recipients_file or recipients_path(paths))
    ciphertext = encrypt_to_recipients(plaintext, recipients)
    archive = write_seed_archive(paths, ciphertext)
    return archive, envelope

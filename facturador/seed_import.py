"""Importar un seed FAC-44 con validación de identidad (FAC-65).

Orden fijo de rechazo (fail-fast, sin override)::

    cert → environment → CUIT → schema → integrity

Ningún fetch a ARCA ocurre hasta que este módulo acepta el envelope.
El seed solo restaura config (emisores, cliente default, UI); el registro
de comprobantes lo reconstruye ``facturador.reconstruct``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import repo
from .certs import CertificateError, load_certificate_metadata
from .fiscal_identity import seal_fiscal_cuit
from .migrations import latest_version
from .profile import EnvironmentProfile
from .seed_backup import (
    SEED_FORMAT,
    SEED_FORMAT_VERSION,
    SeedBackupError,
    assert_seed_has_no_secrets,
    decrypt_with_identity,
    seed_checksum,
)
from .settings import (
    ACTIVE_EMISOR_KEY,
    BACKUP_PREFIX_DEFAULT,
    CONDICION_IVA_DEFAULT,
)


class SeedIdentityError(SeedBackupError):
    """El seed no coincide con la identidad del perfil (sin escape hatch)."""


def load_envelope_from_age(
    ciphertext_path: Path,
    identity_path: Path,
    *,
    age_bin: str = "age",
) -> dict[str, Any]:
    """Descifra ``seed.age`` y parsea el envelope JSON."""
    if not ciphertext_path.is_file():
        raise SeedBackupError(f"No existe el seed: {ciphertext_path}")
    if not identity_path.is_file():
        raise SeedBackupError(f"No existe la identidad age: {identity_path}")
    plaintext = decrypt_with_identity(
        ciphertext_path.read_bytes(), identity_path, age_bin=age_bin
    )
    try:
        envelope = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedBackupError(
            "El plaintext del seed no es un envelope JSON válido."
        ) from exc
    if not isinstance(envelope, dict):
        raise SeedBackupError("Envelope inválido: se esperaba un objeto JSON.")
    return envelope


def parse_envelope(
    envelope: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Valida forma/formato del envelope; no chequea identidad del perfil."""
    assert_seed_has_no_secrets(envelope)
    if envelope.get("format") != SEED_FORMAT:
        raise SeedBackupError(
            f"Formato de seed desconocido: {envelope.get('format')!r}."
        )
    try:
        format_version = int(envelope["format_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedBackupError("format_version inválido en el seed.") from exc
    if format_version != SEED_FORMAT_VERSION:
        raise SeedBackupError(
            f"Versión de formato de seed no soportada: {format_version}."
        )
    seed = dict(envelope["seed"])
    manifest = dict(envelope["manifest"])
    return seed, manifest


def validate_seed_identity(
    seed: Mapping[str, Any],
    manifest: Mapping[str, Any],
    profile: EnvironmentProfile,
    *,
    require_certificate: bool = True,
) -> None:
    """Validación de identidad en orden cert → env → CUIT → schema → integrity.

    ``require_certificate`` es True en restore real (la máquina define la
    identidad). Los tests de forma pura pueden pasar False solo tras haber
    sellado CUIT de otra vía — no hay override de mismatch.
    """
    # 1. Certificado instalado (identidad de la máquina).
    meta = load_certificate_metadata(profile)
    if require_certificate and meta is None:
        raise SeedIdentityError(
            "Falta el certificado del perfil (cert → …). "
            "Instalar el par cert/key antes de restaurar el seed."
        )
    if meta is not None and meta.environment != profile.environment:
        raise SeedIdentityError(
            f"El certificado es de ambiente {meta.environment.value}, "
            f"pero el perfil es {profile.environment.value}."
        )

    # 2. Ambiente del seed vs perfil.
    seed_env = str(seed.get("environment", "")).strip().lower()
    if seed_env not in ("homo", "prod"):
        raise SeedIdentityError(
            f"Ambiente del seed inválido: {seed.get('environment')!r}."
        )
    if seed_env != profile.environment.value:
        raise SeedIdentityError(
            f"Seed de ambiente {seed_env} no se puede importar en un perfil "
            f"{profile.environment.value} (sin override)."
        )

    # 3. CUIT del seed vs certificado (o sello si aún no hay cert en tests).
    seed_cuit = str(seed.get("fiscal_cuit") or "").strip()
    if not seed_cuit.isdigit() or len(seed_cuit) != 11:
        raise SeedIdentityError(
            f"CUIT fiscal del seed inválido: {seed.get('fiscal_cuit')!r}."
        )
    if meta is not None and meta.cuit != seed_cuit:
        raise SeedIdentityError(
            f"CUIT del seed ({seed_cuit}) no coincide con el certificado "
            f"({meta.cuit}). Sin override."
        )

    # 4. Schema: el seed no puede ser más nuevo que esta app.
    try:
        seed_schema = int(seed["schema_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedIdentityError("schema_version del seed inválido.") from exc
    app_schema = latest_version()
    if seed_schema < 1:
        raise SeedIdentityError(f"schema_version del seed inválido: {seed_schema}.")
    if seed_schema > app_schema:
        raise SeedIdentityError(
            f"El seed pide schema_version={seed_schema} pero esta app solo "
            f"conoce hasta {app_schema}. Actualizar la app antes de restaurar."
        )
    try:
        manifest_schema = int(manifest["schema_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedIdentityError(
            "schema_version del manifiesto inválido."
        ) from exc
    if manifest_schema != seed_schema:
        raise SeedIdentityError(
            f"schema_version del manifiesto ({manifest_schema}) no coincide "
            f"con el del seed ({seed_schema})."
        )

    # 5. Integridad: checksum del cuerpo del seed.
    expected = str(manifest.get("checksum") or "").strip()
    if not expected.startswith("sha256:") or len(expected) != len("sha256:") + 64:
        raise SeedIdentityError("checksum del manifiesto inválido.")
    actual = seed_checksum(seed)
    if actual != expected:
        raise SeedIdentityError(
            "checksum del seed no coincide con el manifiesto "
            "(integridad fallida)."
        )


def apply_seed(conn: sqlite3.Connection, seed: Mapping[str, Any]) -> None:
    """Reemplaza config del perfil con el seed (emisores / settings / cliente).

    No toca el registro de facturas ni ``arca_params``. Debe llamarse dentro
    de la misma transacción que el rebuild cuando se hace restore completo.
    """
    env = str(seed["environment"]).strip().lower()
    cuit = str(seed["fiscal_cuit"]).strip()
    seal_fiscal_cuit(conn, cuit)

    # Emisores: wipe + reinsert con ids del seed (estables entre máquinas).
    conn.execute("DELETE FROM emisores")
    for emisor in seed.get("emisores") or []:
        if not isinstance(emisor, Mapping):
            raise SeedBackupError("seed.emisores contiene una entrada inválida.")
        puntos = emisor.get("puntos_venta") or [1]
        if not isinstance(puntos, list) or not puntos:
            puntos = [1]
        clean_pv = [int(v) for v in puntos if isinstance(v, int) and v >= 1]
        if not clean_pv:
            clean_pv = [1]
        emisor_id = str(emisor["id"])
        ts_row = conn.execute(
            "SELECT datetime('now') AS t"
        ).fetchone()
        ts = str(ts_row["t"] if ts_row else "")
        conn.execute(
            "INSERT INTO emisores ("
            "  id, razon_social, domicilio, iibb, inicio_actividades,"
            "  condicion_iva, ambiente, puntos_venta, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                emisor_id,
                str(emisor.get("razon_social") or ""),
                str(emisor.get("domicilio") or ""),
                str(emisor.get("iibb") or ""),
                str(emisor.get("inicio_actividades") or ""),
                str(emisor.get("condicion_iva") or CONDICION_IVA_DEFAULT),
                str(emisor.get("ambiente") or env),
                json.dumps(clean_pv),
                ts,
                ts,
            ),
        )

    active = seed.get("active_emisor_id")
    settings_rows = {
        ACTIVE_EMISOR_KEY: str(active) if active else "",
        "backup_s3_bucket": "",
        "backup_s3_prefix": BACKUP_PREFIX_DEFAULT,
    }
    ui = seed.get("ui") or {}
    if isinstance(ui, Mapping):
        settings_rows["backup_s3_bucket"] = str(
            ui.get("backup_s3_bucket") or ""
        ).strip()
        prefix = str(ui.get("backup_s3_prefix") or "").strip()
        settings_rows["backup_s3_prefix"] = prefix or BACKUP_PREFIX_DEFAULT

    for key, value in settings_rows.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # Cliente default: wipe defaults y recrear el del seed (si hay).
    conn.execute("UPDATE clients SET is_default = 0")
    client = seed.get("default_client")
    if client is not None:
        if not isinstance(client, Mapping):
            raise SeedBackupError("seed.default_client inválido.")
        client_id = str(client["id"])
        existing = repo.get_client(conn, client_id)
        payload = {
            "razon_social": str(client["razon_social"]),
            "domicilio": str(client.get("domicilio") or ""),
            "pais_dst": int(client["pais_dst"]),
            "cuit_pais": int(client["cuit_pais"]),
            "id_impositivo": str(client.get("id_impositivo") or ""),
            "moneda_default": str(client.get("moneda_default") or "DOL"),
            "incoterms_default": str(client.get("incoterms_default") or ""),
            "idioma_default": int(client.get("idioma_default") or 1),
            "forma_pago_default": str(
                client.get("forma_pago_default") or ""
            ),
            "descripcion_default": str(
                client.get("descripcion_default") or ""
            ),
            "is_default": 1,
        }
        if existing is None:
            # Insert con id estable del seed (no usar create_client uuid).
            ts_row = conn.execute("SELECT datetime('now') AS t").fetchone()
            ts = str(ts_row["t"] if ts_row else "")
            fields = (
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
                "is_default",
            )
            conn.execute(
                f"INSERT INTO clients (id, {', '.join(fields)},"
                " created_at, updated_at)"
                f" VALUES (?{', ?' * len(fields)}, ?, ?)",
                (client_id, *(payload[f] for f in fields), ts, ts),
            )
        else:
            conn.execute(
                "UPDATE clients SET"
                " razon_social = ?, domicilio = ?, pais_dst = ?, cuit_pais = ?,"
                " id_impositivo = ?, moneda_default = ?, incoterms_default = ?,"
                " idioma_default = ?, forma_pago_default = ?,"
                " descripcion_default = ?, is_default = 1,"
                " updated_at = datetime('now')"
                " WHERE id = ?",
                (
                    payload["razon_social"],
                    payload["domicilio"],
                    payload["pais_dst"],
                    payload["cuit_pais"],
                    payload["id_impositivo"],
                    payload["moneda_default"],
                    payload["incoterms_default"],
                    payload["idioma_default"],
                    payload["forma_pago_default"],
                    payload["descripcion_default"],
                    client_id,
                ),
            )


def import_seed_envelope(
    conn: sqlite3.Connection,
    envelope: Mapping[str, Any],
    profile: EnvironmentProfile,
    *,
    require_certificate: bool = True,
) -> dict[str, Any]:
    """Parse + validate identity + apply. Devuelve el seed aceptado."""
    seed, manifest = parse_envelope(envelope)
    try:
        validate_seed_identity(
            seed, manifest, profile, require_certificate=require_certificate
        )
    except CertificateError as exc:
        raise SeedIdentityError(str(exc)) from exc
    apply_seed(conn, seed)
    return seed

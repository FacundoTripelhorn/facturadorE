"""FAC-44: seed cifrado (config + manifiesto) sin DB/secretos/comprobantes."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from facturador import repo
from facturador.backup import main as backup_main
from facturador.db import connect
from facturador.fiscal_identity import seal_fiscal_cuit
from facturador.profile import ProfilePaths
from facturador.seed_backup import (
    SeedBackupError,
    assemble_seed,
    assert_seed_has_no_secrets,
    build_envelope,
    build_manifest,
    create_encrypted_seed,
    decrypt_with_identity,
    ensure_device_id,
    load_recipients,
    parse_recipients,
    recipients_object_key,
    recipients_path,
    seed_archive_path,
    seed_checksum,
    seed_object_key,
    serialize_envelope,
)
from facturador.settings import Emisor, Settings, save_settings, set_active_emisor
from tests.conftest import EMISOR_PRUEBA, TEST_CUIT

_HAS_AGE = shutil.which("age") is not None and shutil.which("age-keygen") is not None
requires_age = pytest.mark.skipif(
    not _HAS_AGE,
    reason="age/age-keygen no están en PATH (CI los instala; ver workflow)",
)


def _age_keygen(path: Path) -> str:
    """Genera identidad age en ``path`` y devuelve la clave pública."""
    result = subprocess.run(
        ["age-keygen", "-o", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    # La pubkey va a stderr: "Public key: age1…"
    for line in result.stderr.splitlines():
        if "age1" in line:
            return line.strip().split()[-1]
    raise AssertionError("age-keygen no imprimió una clave pública")


@pytest.fixture
def perfil_listo(tmp_path) -> tuple[ProfilePaths, sqlite3.Connection]:
    paths = ProfilePaths(root=tmp_path / "perfil")
    paths.ensure_layout()
    # Secretos y DB "sucia" existen en disco pero NO deben entrar al seed.
    paths.key.write_text("BEGIN PRIVATE KEY\nSECRET\n", encoding="utf-8")
    paths.key.chmod(0o600)
    paths.cert.write_text("CERT", encoding="utf-8")
    paths.pdf_dir.mkdir()
    (paths.pdf_dir / "factura.pdf").write_bytes(b"%PDF-fake")

    conn = connect(paths.db)
    seal_fiscal_cuit(conn, TEST_CUIT)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(**EMISOR_PRUEBA, ambiente="homo", puntos_venta=(4, 5)),
            backup_s3_bucket="mi-bucket",
            backup_s3_prefix="pfx",
        ),
    )
    emisor_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, emisor_id)
    repo.create_client(
        conn,
        {
            "razon_social": "CLIENTE DEFAULT SA",
            "domicilio": "Montevideo",
            "pais_dst": 225,
            "cuit_pais": 55000002002,
            "id_impositivo": "218888888888",
            "moneda_default": "DOL",
            "incoterms_default": "",
            "idioma_default": 1,
            "forma_pago_default": "WIRE TRANSFER",
            "descripcion_default": "Servicios",
            "is_default": 1,
        },
    )
    # Fila de factura: el seed no debe referenciarla.
    conn.execute(
        "INSERT INTO invoices ("
        " id, emisor_id, cbte_tipo, punto_venta, cbte_nro, status, source,"
        " fecha_cbte, fecha_pago, tipo_expo, dst_cmp, cliente,"
        " cuit_pais_cliente, moneda_ctz, imp_total, environment,"
        " created_at, updated_at"
        ") VALUES ("
        " 'inv-1', ?, 19, 4, 99, 'authorized', 'wsfex',"
        " '20260718', '20260718', 2, 225, 'X',"
        " 55000002002, '1', '100', 'homo',"
        " 't', 't')"
        ,
        (emisor_id,),
    )
    conn.commit()
    return paths, conn


def test_parse_recipients_ignora_comentarios_y_deduplica():
    text = """
# laptop windows
age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# mac
age1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""
    assert parse_recipients(text) == [
        "age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "age1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]


def test_parse_recipients_rechaza_basura():
    with pytest.raises(SeedBackupError, match="age1"):
        parse_recipients("ssh-rsa AAAA\n")


def test_object_keys_son_fijos_overwrite():
    assert seed_object_key("pfx", TEST_CUIT, "homo") == (
        f"pfx/{TEST_CUIT}/homo/seed.age"
    )
    assert recipients_object_key("pfx/", TEST_CUIT, "prod") == (
        f"pfx/{TEST_CUIT}/prod/recipients.txt"
    )


def test_assemble_seed_tiene_config_y_excluye_comprobantes(perfil_listo):
    paths, conn = perfil_listo
    seed = assemble_seed(conn, environment="homo")

    assert seed["schema_version"] == 1
    assert seed["environment"] == "homo"
    assert seed["fiscal_cuit"] == TEST_CUIT
    assert seed["comprobante_tipos"] == [19, 20, 21]
    assert seed["emisores"][0]["razon_social"] == EMISOR_PRUEBA["razon_social"]
    assert seed["emisores"][0]["puntos_venta"] == [4, 5]
    assert seed["default_client"]["razon_social"] == "CLIENTE DEFAULT SA"
    assert seed["ui"]["backup_s3_bucket"] == "mi-bucket"
    assert seed["ui"]["backup_s3_prefix"] == "pfx"
    assert seed["ui"]["pdf_render_version"] == 1

    blob = json.dumps(seed)
    assert "inv-1" not in blob
    assert "cbte_nro" not in blob
    assert "BEGIN PRIVATE KEY" not in blob
    assert '"cae"' not in blob

    manifest = build_manifest(seed, device_id=ensure_device_id(paths))
    envelope = build_envelope(seed, manifest)
    assert_seed_has_no_secrets(envelope)
    assert set(manifest) == {
        "schema_version",
        "device_id",
        "timestamp",
        "checksum",
    }
    assert "last_cmp" not in manifest
    assert manifest["checksum"] == seed_checksum(seed)
    assert manifest["checksum"].startswith("sha256:")


@requires_age
def test_encrypt_to_all_recipients_and_roundtrip(perfil_listo, tmp_path):
    paths, conn = perfil_listo
    key1 = tmp_path / "id1.txt"
    key2 = tmp_path / "id2.txt"
    pub1 = _age_keygen(key1)
    pub2 = _age_keygen(key2)
    # Identidades age son secretas: no deben aparecer en fixtures de seed.
    assert "AGE-SECRET-KEY-" in key1.read_text(encoding="utf-8")

    recipients_path(paths).write_text(
        f"# máquina A\n{pub1}\n# máquina B\n{pub2}\n",
        encoding="utf-8",
    )

    archive, envelope = create_encrypted_seed(
        conn, paths, environment="homo"
    )
    ciphertext = archive.read_bytes()
    assert archive == seed_archive_path(paths)
    assert ciphertext.startswith(b"age-encryption.org/v1")
    assert archive.stat().st_size < 8_192  # low-kilobyte range
    # Overwrite semantics: segundo backup reemplaza el mismo path.
    archive2, _ = create_encrypted_seed(conn, paths, environment="homo")
    assert archive2 == archive

    plain1 = decrypt_with_identity(ciphertext, key1)
    plain2 = decrypt_with_identity(ciphertext, key2)
    assert plain1 == plain2
    loaded = json.loads(plain1.decode("utf-8"))
    assert loaded["seed"]["fiscal_cuit"] == TEST_CUIT
    assert loaded["manifest"]["checksum"] == envelope["manifest"]["checksum"]
    # El ciphertext no lleva el JSON en claro ni el material privado.
    assert b"fiscal_cuit" not in ciphertext
    assert b"BEGIN PRIVATE KEY" not in ciphertext
    assert b"AGE-SECRET-KEY-" not in ciphertext
    assert paths.key.read_text(encoding="utf-8")  # secrets intactos en disco


@requires_age
def test_agregar_recipient_alcanza_para_el_proximo_backup(perfil_listo, tmp_path):
    paths, conn = perfil_listo
    key1 = tmp_path / "id1.txt"
    key2 = tmp_path / "id2.txt"
    pub1 = _age_keygen(key1)
    pub2 = _age_keygen(key2)
    recipients_path(paths).write_text(f"{pub1}\n", encoding="utf-8")
    create_encrypted_seed(conn, paths, environment="homo")
    with pytest.raises(SeedBackupError):
        decrypt_with_identity(seed_archive_path(paths).read_bytes(), key2)

    # Solo agregar la línea basta para el próximo backup.
    recipients_path(paths).write_text(f"{pub1}\n{pub2}\n", encoding="utf-8")
    create_encrypted_seed(conn, paths, environment="homo")
    plain = decrypt_with_identity(seed_archive_path(paths).read_bytes(), key2)
    assert json.loads(plain)["seed"]["fiscal_cuit"] == TEST_CUIT


def test_load_recipients_exige_archivo_con_claves(tmp_path):
    missing = tmp_path / "recipients.txt"
    with pytest.raises(SeedBackupError, match="No hay"):
        load_recipients(missing)
    missing.write_text("# solo comentarios\n", encoding="utf-8")
    with pytest.raises(SeedBackupError, match="ninguna clave"):
        load_recipients(missing)


@requires_age
def test_cli_backup_escribe_seed_age(perfil_listo, tmp_path, capsys):
    paths, conn = perfil_listo
    conn.close()
    key = tmp_path / "id.txt"
    pub = _age_keygen(key)
    recipients_path(paths).write_text(f"{pub}\n", encoding="utf-8")

    assert backup_main(["--root", str(paths.root)]) == 0
    out = capsys.readouterr().out
    assert "Seed cifrado:" in out
    assert seed_archive_path(paths).is_file()
    assert f"pfx/{TEST_CUIT}/homo/seed.age" in out
    # No sube a S3 en FAC-44.
    assert "FAC-45" in out


def test_cli_backup_falla_sin_recipients(perfil_listo, capsys):
    paths, conn = perfil_listo
    conn.close()
    assert backup_main(["--root", str(paths.root)]) == 1
    assert "recipients.txt" in capsys.readouterr().err


def test_serialize_envelope_es_json_estable(perfil_listo):
    paths, conn = perfil_listo
    seed = assemble_seed(conn, environment="homo")
    manifest = build_manifest(seed, device_id="device-test")
    raw = serialize_envelope(build_envelope(seed, manifest))
    # Bytes estables + newline final; apto para checksum/diff.
    assert raw.endswith(b"\n")
    assert json.loads(raw)["format"] == "facturador.seed"

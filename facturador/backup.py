"""Backup cifrado del seed de UN perfil (design.md §2.5, ADR 0001).

ARCA es el ledger autoritativo. Este módulo respalda solo el **seed** de
configuración que ARCA no puede reproducir (emisores, CUIT fiscal, ambiente,
PV/tipos, cliente default / UI, versión de esquema + manifiesto). La DB
local, PDFs, certificados e identidades ``age`` **no** viajan en el bundle;
el registro se reconstruye desde ARCA.

Pasos:

1. Armar el seed JSON desde la DB del perfil (sin filas de comprobantes).
2. Adjuntar manifiesto (schema version, device-id, timestamp, checksum).
3. Cifrar con ``age`` a **todas** las claves de ``backups/recipients.txt``.
4. Sobrescribir ``backups/seed.age`` (nombre fijo). El upload a S3 lo hace
   seed_backup_sync.

Uso:  uv run python -m facturador.backup --env homo|prod
      uv run python -m facturador.backup --root <raíz-del-perfil>

El perfil se elige EXPLÍCITO (--env o --root). Sin default silencioso.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

from .db import connect
from .profile import (
    DB_FILENAME as DB_NAME,
)
from .profile import (
    EnvironmentProfile,
    ProfileError,
    ProfilePaths,
    parse_environment,
)
from .seed_backup import (
    SeedBackupError,
    create_encrypted_seed,
    recipients_path,
    seed_archive_path,
    seed_object_key,
)
from .settings import BACKUP_PREFIX_DEFAULT


class BackupError(RuntimeError):
    pass


def resolve_profile_paths(
    env: str | None,
    root: str | None,
    create: bool = False,
) -> ProfilePaths:
    """Paths del perfil elegido EXPLÍCITO: --env (app-data del SO) o --root.

    Nunca un directorio implícito (CWD, home compartido): backup/restore
    sobre un perfil equivocado son silenciosamente destructivos; el guard de
    secrets/ en main() corta el resto de los casos.

    ``create=True`` (restore): la máquina secundaria puede no tener el
    perfil todavía.
    """
    if bool(env) == bool(root):
        raise BackupError(
            "Indicar el perfil explícito: --env homo|prod O --root "
            "<raíz-del-perfil> (exactamente uno)."
        )
    if root:
        paths = ProfilePaths(root=Path(root).expanduser())
    else:
        assert env is not None
        try:
            paths = EnvironmentProfile.resolve(parse_environment(env)).paths
        except ProfileError as exc:
            raise BackupError(str(exc)) from exc
    if create:
        paths.ensure_layout()
    if not paths.root.is_dir():
        raise BackupError(f"La raíz del perfil no existe: {paths.root}")
    return paths


def backup_s3_settings(snapshot: bytes | None) -> tuple[str, str]:
    """Bucket/prefijo de S3 desde la tabla settings del snapshot de la DB.

    Legacy helper (restore / tests). El seed nuevo lee settings vía
    ``assemble_seed``; el upload lo hace seed_backup_sync.
    """
    if snapshot is None:
        return "", BACKUP_PREFIX_DEFAULT
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(snapshot)
        try:
            rows = dict(conn.execute("SELECT key, value FROM settings"))
        except sqlite3.OperationalError:
            return "", BACKUP_PREFIX_DEFAULT
    finally:
        conn.close()
    bucket = (rows.get("backup_s3_bucket") or "").strip()
    prefix = (rows.get("backup_s3_prefix") or "").strip() or BACKUP_PREFIX_DEFAULT
    return bucket, prefix


def snapshot_db(db_path: Path) -> bytes:
    """Copia consistente de la DB vía sqlite3 Connection.backup."""
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(":memory:")
        try:
            src.backup(dst)
            return dst.serialize()
        finally:
            dst.close()
    finally:
        src.close()


def build_tar(paths: ProfilePaths, db_snapshot: bytes | None) -> bytes:
    """Tarball gz en memoria de secrets/ + data/ del perfil (DB = snapshot).

    Legacy: el backup normal ya no empaqueta DB ni secrets.
    Conservado para restore.py / tests (el restore actual reconstruye desde ARCA).
    """

    def _skip(name: str, path: Path) -> bool:
        if path.name == DB_NAME or path.name.startswith(DB_NAME + "-"):
            return True  # la DB viva y sus derivados; va el snapshot
        if name.startswith("data/logs"):
            return True
        # Cache local de PDFs: regenerable; no forma parte del backup.
        if name == "data/pdfs" or name.startswith("data/pdfs/"):
            return True
        return False

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for top, root in (("secrets", paths.secrets_dir), ("data", paths.data_dir)):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                name = f"{top}/{path.relative_to(root)}".replace("\\", "/")
                if not _skip(name, path):
                    tar.add(path, arcname=name, recursive=False)
        if db_snapshot is not None:
            info = tarfile.TarInfo(f"data/{DB_NAME}")
            info.size = len(db_snapshot)
            info.mtime = int(dt.datetime.now().timestamp())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(db_snapshot))
    return buf.getvalue()


def encrypt_age(plaintext: bytes, out_path: Path) -> None:
    """``age -p``: pide la passphrase en la terminal, jamás por argv/env.

    Legacy (archives con passphrase). El seed actual usa recipients.
    """
    if shutil.which("age") is None:
        raise BackupError(
            "No se encontró `age` en el PATH. Instalar: winget install "
            "FiloSottile.age (Windows) / brew install age (macOS)."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["age", "-p", "-o", str(out_path)], input=plaintext, check=True
    )


def upload_s3(archive: Path, bucket: str, prefix: str) -> str:
    """Legacy upload por nombre timestamped. Reemplazado por el upload del seed."""
    if shutil.which("aws") is None:
        raise BackupError("BACKUP_S3_BUCKET definido pero no hay `aws` CLI en PATH.")
    dest = f"s3://{bucket}/{prefix.strip('/')}/{archive.name}"
    subprocess.run(["aws", "s3", "cp", str(archive), dest], check=True)
    return dest


def _environment_from_db(conn: sqlite3.Connection) -> str:
    """Sello de ambiente del perfil: emisor activo o primer emisor."""
    from .settings import get_active_emisor_id, load_emisor

    active_id = get_active_emisor_id(conn)
    if active_id:
        return load_emisor(conn, active_id).ambiente
    rows = list(conn.execute("SELECT ambiente FROM emisores ORDER BY created_at, id"))
    if rows:
        return str(rows[0][0])
    raise BackupError(
        "Perfil incompleto: no hay emisores configurados. "
        "Completar el setup antes de respaldar el seed."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env", help="ambiente del perfil a respaldar (homo | prod)"
    )
    parser.add_argument(
        "--root", help="raíz explícita del perfil (alternativa a --env)"
    )
    args = parser.parse_args(argv)

    try:
        paths = resolve_profile_paths(args.env, args.root)
        if not paths.db.is_file():
            raise BackupError(
                f"No hay DB en {paths.db}; el seed necesita la config del "
                "perfil (emisores / settings)."
            )
        recipients = recipients_path(paths)
        if not recipients.is_file():
            raise BackupError(
                f"Falta {recipients}. Crear el archivo con una clave "
                "pública age por línea (una por máquina) antes de respaldar."
            )

        print(f"Perfil: {paths.root}")

        conn = connect(paths.db)
        try:
            if args.env:
                environment = parse_environment(args.env).value
            else:
                environment = _environment_from_db(conn)
            print(f"Ambiente: {environment}")
            archive, envelope = create_encrypted_seed(
                conn, paths, environment=environment
            )
        finally:
            conn.close()

        seed = envelope["seed"]
        cuit = seed["fiscal_cuit"]
        prefix = seed["ui"]["backup_s3_prefix"]
        logical = seed_object_key(prefix, cuit, environment)
        print(f"Seed cifrado: {archive} ({archive.stat().st_size} bytes)")
        print(f"Clave lógica (S3): {logical}")
        print(
            "Upload a S3: lo hace la app al cambiar la configuración "
            f"(recipients locales: {recipients_path(paths)})."
        )
        # Evitar que un seed.age viejo con otro nombre confunda: el único
        # artefacto es backups/seed.age.
        assert archive == seed_archive_path(paths)
        return 0
    except (
        BackupError,
        SeedBackupError,
        ProfileError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

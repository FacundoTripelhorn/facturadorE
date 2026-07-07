"""Backup cifrado del estado completo (design.md §2.5).

El estado es ``data/`` + ``secrets/`` + ``.env`` bajo FACTURADOR_HOME. Pasos:

1. Snapshot de la DB con la API de backup de sqlite3 (nunca copiar el
   archivo en caliente).
2. Tarball en memoria con el snapshot en lugar de la DB viva. Los logs y
   los archivos -wal/-shm/-journal quedan afuera. El tar nunca toca el
   disco en claro: el backup contiene la clave fiscal.
3. Cifrado del lado del cliente con ``age -p`` (passphrase interactiva del
   usuario; nunca en el repo, el entorno ni AWS). El SSE de S3 NO alcanza.
4. Upload a S3 con ``aws s3 cp`` si BACKUP_S3_BUCKET está definido; si no,
   el ``.tar.gz.age`` queda solo en ``backups/``.

Corre en el HOST (no en el contenedor): requiere ``age`` en el PATH y, para
el upload, ``aws`` CLI con credenciales del IAM user dedicado al bucket.
La emisión nunca depende de esto (S3 caído solo degrada portabilidad).

Uso:  uv run python -m facturador.backup --home ~/facturador
(--home puede omitirse si FACTURADOR_HOME está en el entorno o en el .env
del directorio actual; no hay fallback implícito al CWD)
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

from dotenv import load_dotenv

DB_NAME = "facturador.db"
# Derivados de SQLite que no tiene sentido llevar (el snapshot ya es
# consistente) y logs, que no son estado.
_EXCLUDE_SUFFIXES = ("-wal", "-shm", "-journal")


class BackupError(RuntimeError):
    pass


def resolve_home(cli_home: str | None = None) -> Path:
    """Home EXPLÍCITO: --home o FACTURADOR_HOME (del entorno o del .env del
    CWD). A diferencia del servidor, acá no hay fallback al directorio
    actual: backup/restore sobre un directorio implícito equivocado son
    silenciosamente destructivos (review del PR #8). Además carga el .env
    del home, donde vive BACKUP_S3_* en el layout Docker."""
    load_dotenv(".env")
    raw = cli_home or os.environ.get("FACTURADOR_HOME")
    if not raw:
        raise BackupError(
            "Indicar el directorio de datos con --home o FACTURADOR_HOME "
            "(no hay default: operar sobre un directorio implícito "
            "equivocado dejaría un backup/restore inservible)."
        )
    home = Path(raw).expanduser()
    if not home.is_dir():
        raise BackupError(f"FACTURADOR_HOME no existe: {home}")
    # No pisa variables ya definidas (el entorno y el .env del CWD ganan).
    load_dotenv(home / ".env")
    return home


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


def build_tar(home: Path, db_snapshot: bytes | None) -> bytes:
    """Tarball gz en memoria de .env + secrets/ + data/ (DB = snapshot)."""

    def _skip(name: str, path: Path) -> bool:
        if path.name == DB_NAME or path.name.startswith(DB_NAME + "-"):
            return True  # la DB viva y sus derivados; va el snapshot
        return name.startswith("data/logs")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if (home / ".env").is_file():
            tar.add(home / ".env", arcname=".env")
        for top in ("secrets", "data"):
            root = home / top
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
    """``age -p``: pide la passphrase en la terminal, jamás por argv/env."""
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
    if shutil.which("aws") is None:
        raise BackupError("BACKUP_S3_BUCKET definido pero no hay `aws` CLI en PATH.")
    dest = f"s3://{bucket}/{prefix.strip('/')}/{archive.name}"
    subprocess.run(["aws", "s3", "cp", str(archive), dest], check=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", help="FACTURADOR_HOME (default: env o CWD)")
    args = parser.parse_args(argv)

    try:
        home = resolve_home(args.home)
        # Guardia contra respaldar el directorio equivocado (p.ej. correr
        # desde el repo sin FACTURADOR_HOME apuntando a ~/facturador): sin
        # secrets/ esto no es un FACTURADOR_HOME y el archivo saldría vacío.
        if not (home / "secrets").is_dir():
            raise BackupError(
                f"{home} no parece un FACTURADOR_HOME (no tiene secrets/). "
                "Definir FACTURADOR_HOME o pasar --home apuntando al "
                "directorio de datos real."
            )
        print(f"FACTURADOR_HOME: {home}")
        db_path = home / "data" / DB_NAME
        snapshot = snapshot_db(db_path) if db_path.is_file() else None
        if snapshot is None:
            print(f"AVISO: no hay DB en {db_path}; se respalda el resto.")

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = home / "backups" / f"facturador-{stamp}.tar.gz.age"
        encrypt_age(build_tar(home, snapshot), archive)
        print(f"Backup cifrado: {archive}")

        bucket = os.environ.get("BACKUP_S3_BUCKET", "").strip()
        if bucket:
            prefix = os.environ.get("BACKUP_S3_PREFIX", "facturador").strip()
            print(f"Subido a {upload_s3(archive, bucket, prefix)}")
        else:
            print("BACKUP_S3_BUCKET no definido: el backup queda solo local.")
        return 0
    except (BackupError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

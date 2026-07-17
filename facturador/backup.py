"""Backup cifrado del estado completo de UN perfil (design.md §2.5, ADR 0001).

El estado es ``data/`` + ``secrets/`` bajo la raíz del perfil del ambiente
elegido (FAC-25: todo archivo de runtime pasa por ``ProfilePaths``; el
``.env`` de bootstrap no es estado del perfil y queda afuera). Pasos:

1. Snapshot de la DB con la API de backup de sqlite3 (nunca copiar el
   archivo en caliente).
2. Tarball en memoria con el snapshot en lugar de la DB viva. Los logs,
   los PDFs generados (cache descartable FAC-53) y los archivos
   -wal/-shm/-journal quedan afuera. El tar nunca toca el disco en
   claro: el backup contiene la clave fiscal.
3. Cifrado del lado del cliente con ``age -p`` (passphrase interactiva del
   usuario; nunca en el repo, el entorno ni AWS). El SSE de S3 NO alcanza.
4. Upload a S3 con ``aws s3 cp`` si el bucket está configurado en la app
   (página Configuración → tabla ``settings`` de la DB del perfil); si no,
   el ``.tar.gz.age`` queda solo en el ``backups/`` del perfil.

Corre en el HOST (no en el contenedor): requiere ``age`` en el PATH y, para
el upload, ``aws`` CLI con credenciales del IAM user dedicado al bucket.
La emisión nunca depende de esto (S3 caído solo degrada portabilidad).

Uso:  uv run python -m facturador.backup --env homo|prod
      uv run python -m facturador.backup --root <raíz-del-perfil>

El perfil se elige EXPLÍCITO (--env resuelve la raíz en el app-data del SO;
--root la fija a mano, p.ej. para el layout de Docker o tests). Sin default
silencioso, igual que el arranque (FAC-24). No se lee ningún .env: el
bucket/prefijo de S3 salen de la DB, dentro del propio backup.
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

from .profile import (
    DB_FILENAME as DB_NAME,
)
from .profile import (
    EnvironmentProfile,
    ProfileError,
    ProfilePaths,
    parse_environment,
)
from .settings import BACKUP_PREFIX_DEFAULT

# Derivados de SQLite que no tiene sentido llevar (el snapshot ya es
# consistente), logs (no son estado) y PDFs generados (cache descartable
# FAC-53: se regeneran desde el snapshot inmutable de la factura).
_EXCLUDE_SUFFIXES = ("-wal", "-shm", "-journal")


class BackupError(RuntimeError):
    pass


def resolve_profile_paths(
    env: str | None,
    root: str | None,
    create: bool = False,
) -> ProfilePaths:
    """Paths del perfil elegido EXPLÍCITO: --env (app-data del SO) o --root.

    Nunca un directorio implícito (CWD, home compartido): backup/restore
    sobre un perfil equivocado son silenciosamente destructivos (review del
    PR #8); el guard de secrets/ en main() corta el resto de los casos.

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

    La config de backups vive en la app (DB del perfil), no en el entorno:
    se lee del mismo snapshot que se está respaldando. Sin DB, o con una DB
    anterior a la tabla settings, el backup queda solo local."""
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

    Excluye logs y ``data/pdfs/`` (FAC-53: los PDF generados no son
    fuente de verdad ni viajan en el backup normal).
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
    parser.add_argument(
        "--env", help="ambiente del perfil a respaldar (homo | prod)"
    )
    parser.add_argument(
        "--root", help="raíz explícita del perfil (alternativa a --env)"
    )
    args = parser.parse_args(argv)

    try:
        paths = resolve_profile_paths(args.env, args.root)
        # Guardia contra respaldar el directorio equivocado (p.ej. un typo
        # en --root): sin secrets/ esto no es la raíz de un perfil y el
        # archivo saldría vacío.
        if not paths.secrets_dir.is_dir():
            raise BackupError(
                f"{paths.root} no parece la raíz de un perfil (no tiene "
                "secrets/). Pasar --env homo|prod o --root apuntando a la "
                "raíz real del perfil."
            )
        print(f"Perfil: {paths.root}")
        snapshot = snapshot_db(paths.db) if paths.db.is_file() else None
        if snapshot is None:
            print(f"AVISO: no hay DB en {paths.db}; se respalda el resto.")

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = paths.backups_dir / f"facturador-{stamp}.tar.gz.age"
        encrypt_age(build_tar(paths, snapshot), archive)
        print(f"Backup cifrado: {archive}")

        # El destino S3 sale de la config guardada en la app (en la DB).
        bucket, prefix = backup_s3_settings(snapshot)
        if bucket:
            print(f"Subido a {upload_s3(archive, bucket, prefix)}")
        else:
            print(
                "Sin bucket S3 configurado (página Configuración): "
                "el backup queda solo local."
            )
        return 0
    except (BackupError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

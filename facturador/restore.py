"""Restore de un backup cifrado (design.md §2.5) en la máquina secundaria.

Inverso de facturador.backup: descarga (opcional) el último ``.tar.gz.age``
del bucket, lo descifra con ``age -d`` (passphrase interactiva) y extrae
``secrets/`` + ``data/`` en la raíz del perfil del ambiente elegido
(FAC-25). La numeración se resincroniza sola contra ARCA (FEXGetLast_CMP)
y el chequeo de DB desactualizada bloquea la emisión si el restore quedó
viejo.

Se niega a pisar una DB existente sin ``--force``: restaurar arriba de la
máquina primaria por error destruiría el registro bueno.

El perfil se elige EXPLÍCITO, igual que en backup (--env homo|prod o
--root; nunca el CWD ni un default silencioso). Con ``--latest`` el bucket
se pasa por ``--bucket``: en una máquina recién estrenada todavía no hay DB
de la cual leer la config de backups (que vive en la app).

Uso:
  uv run python -m facturador.restore --env homo <backup.tar.gz.age>
  uv run python -m facturador.restore --env prod --latest --bucket mi-bucket
"""

from __future__ import annotations

import argparse
import io
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

from .backup import BackupError, resolve_profile_paths
from .profile import ProfilePaths
from .settings import BACKUP_PREFIX_DEFAULT


def download_latest(bucket: str, prefix: str, dest_dir: Path) -> Path:
    """Trae el .tar.gz.age más reciente del bucket (por nombre = timestamp)."""
    if shutil.which("aws") is None:
        raise BackupError("--latest necesita `aws` CLI en el PATH.")
    listado = subprocess.run(
        ["aws", "s3", "ls", f"s3://{bucket}/{prefix.strip('/')}/"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    nombres = sorted(
        line.split()[-1]
        for line in listado.splitlines()
        if line.strip().endswith(".tar.gz.age")
    )
    if not nombres:
        raise BackupError(f"No hay backups en s3://{bucket}/{prefix}/")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / nombres[-1]
    subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{prefix.strip('/')}/{nombres[-1]}",
         str(dest)],
        check=True,
    )
    return dest


def decrypt_age(archive: Path) -> bytes:
    if shutil.which("age") is None:
        raise BackupError("No se encontró `age` en el PATH.")
    return subprocess.run(
        ["age", "-d", str(archive)], check=True, capture_output=True
    ).stdout


def extract(tar_bytes: bytes, root: Path) -> list[str]:
    """Extrae el tarball en la raíz del perfil con el filtro "data" de
    tarfile (bloquea paths absolutos, ``..`` y symlinks fuera del árbol)."""
    extraidos: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            extraidos.append(member.name)
        tar.extractall(root, filter="data")
    _endurecer_secrets(ProfilePaths(root=root).secrets_dir)
    return extraidos


def _endurecer_secrets(secrets_dir: Path) -> None:
    # En POSIX (macOS/Linux) dejar la key como la exige el chequeo de
    # arranque; en Windows los bits de modo no aplican (igual que config.py).
    if sys.platform == "win32" or not secrets_dir.is_dir():
        return
    secrets_dir.chmod(0o700)
    for path in secrets_dir.iterdir():
        if path.is_file():
            path.chmod(stat.S_IRUSR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", nargs="?", help="path a un .tar.gz.age local")
    parser.add_argument(
        "--latest", action="store_true", help="bajar el último backup del bucket"
    )
    parser.add_argument(
        "--bucket", help="bucket S3 del backup (requerido con --latest)"
    )
    parser.add_argument(
        "--prefix", default=BACKUP_PREFIX_DEFAULT, help="prefijo en el bucket"
    )
    parser.add_argument(
        "--env", help="ambiente del perfil destino (homo | prod)"
    )
    parser.add_argument(
        "--root", help="raíz explícita del perfil destino (alternativa a --env)"
    )
    parser.add_argument(
        "--force", action="store_true", help="pisar una DB local existente"
    )
    args = parser.parse_args(argv)

    try:
        paths = resolve_profile_paths(args.env, args.root, create=True)

        if args.latest == bool(args.archive):
            raise BackupError("Indicar un archivo O --latest (exactamente uno).")
        if args.latest:
            if not args.bucket:
                raise BackupError(
                    "--latest necesita --bucket: en una máquina nueva no hay "
                    "DB todavía de la cual leer la config de backups."
                )
            archive = download_latest(args.bucket, args.prefix, paths.backups_dir)
            print(f"Descargado: {archive}")
        else:
            archive = Path(args.archive)
            if not archive.is_file():
                raise BackupError(f"No existe: {archive}")

        if paths.db.is_file() and not args.force:
            raise BackupError(
                f"Ya hay una DB en {paths.db}. Si esta máquina NO es la "
                "primaria y el backup es más nuevo, repetir con --force."
            )

        extraidos = extract(decrypt_age(archive), paths.root)
        print(f"Restaurados {len(extraidos)} archivos en {paths.root}:")
        for name in extraidos:
            print(f"  {name}")
        return 0
    except (BackupError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

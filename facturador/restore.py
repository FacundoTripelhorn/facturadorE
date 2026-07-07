"""Restore de un backup cifrado (design.md §2.5) en la máquina secundaria.

Inverso de facturador.backup: descarga (opcional) el último ``.tar.gz.age``
del bucket, lo descifra con ``age -d`` (passphrase interactiva) y extrae
``.env`` + ``secrets/`` + ``data/`` en FACTURADOR_HOME. La numeración se
resincroniza sola contra ARCA (FEXGetLast_CMP) y el chequeo de DB
desactualizada bloquea la emisión si el restore quedó viejo.

Se niega a pisar una DB existente sin ``--force``: restaurar arriba de la
máquina primaria por error destruiría el registro bueno.

Uso:
  uv run python -m facturador.restore backups/facturador-XXXX.tar.gz.age
  uv run python -m facturador.restore --latest   # último del bucket S3
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

from .backup import DB_NAME, BackupError, resolve_home


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


def extract(tar_bytes: bytes, home: Path) -> list[str]:
    """Extrae el tarball en home con el filtro "data" de tarfile (bloquea
    paths absolutos, ``..`` y symlinks fuera del árbol)."""
    extraidos: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            extraidos.append(member.name)
        tar.extractall(home, filter="data")
    _endurecer_secrets(home / "secrets")
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
    parser.add_argument("--home", help="FACTURADOR_HOME (default: env o CWD)")
    parser.add_argument(
        "--force", action="store_true", help="pisar una DB local existente"
    )
    args = parser.parse_args(argv)

    try:
        home = resolve_home(args.home)

        if args.latest == bool(args.archive):
            raise BackupError("Indicar un archivo O --latest (exactamente uno).")
        if args.latest:
            bucket = os.environ.get("BACKUP_S3_BUCKET", "").strip()
            if not bucket:
                raise BackupError("--latest necesita BACKUP_S3_BUCKET definido.")
            prefix = os.environ.get("BACKUP_S3_PREFIX", "facturador").strip()
            archive = download_latest(bucket, prefix, home / "backups")
            print(f"Descargado: {archive}")
        else:
            archive = Path(args.archive)
            if not archive.is_file():
                raise BackupError(f"No existe: {archive}")

        db_path = home / "data" / DB_NAME
        if db_path.is_file() and not args.force:
            raise BackupError(
                f"Ya hay una DB en {db_path}. Si esta máquina NO es la "
                "primaria y el backup es más nuevo, repetir con --force."
            )

        extraidos = extract(decrypt_age(archive), home)
        print(f"Restaurados {len(extraidos)} archivos en {home}:")
        for name in extraidos:
            print(f"  {name}")
        return 0
    except (BackupError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Restore FAC-65: importar seed + reconstruir el registro desde ARCA.

Reemplaza el restore legacy de tarball+DB. El bundle es solo el seed
cifrado (FAC-44); la historia fiscal se reconsulta con FEXGetCMP.

Uso (backend detenido; perfil ya ``ready``)::

  uv run python -m facturador.restore --env homo \\
      --seed backups/seed.age --identity ~/.age/key.txt

Completar setup (cert, emisor, PV) **antes** de restaurar: ``apply_seed``
sobrescribe la config de emisor/UI con la del seed. El launcher expone
``--restore``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from .arca.wsaa import WsaaClient
from .arca.wsfex import WsfexClient
from .backup import BackupError, resolve_profile_paths
from .certs import CertificateError, load_certificate_metadata
from .config import Config
from .db import connect
from .fiscal_identity import FiscalIdentityError, resolve_profile_fiscal_cuit
from .profile import EnvironmentProfile, ProfileError, parse_environment
from .reconstruct import (
    ProfileSnapshot,
    ReconstructError,
    ReconstructMode,
    ReconstructReport,
    profile_snapshot_from_conn,
    reconstruct_register,
    targets_from_conn,
    targets_from_seed,
)
from .seed_backup import SeedBackupError, seed_archive_path
from .seed_import import (
    SeedIdentityError,
    apply_seed,
    assert_profile_ready,
    load_envelope_from_age,
    parse_envelope,
    validate_seed_identity,
)


def extract(tar_bytes: bytes, root: Path) -> list[str]:
    """Legacy: extrae un tarball gz en la raíz del perfil (tests / backup.py).

    Usa el filtro ``data`` de tarfile (bloquea paths absolutos, ``..`` y
    symlinks fuera del árbol). Conservado mientras ``build_tar`` exista.
    """
    import io
    import stat
    import tarfile

    from .profile import ProfilePaths

    extraidos: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            extraidos.append(member.name)
        tar.extractall(root, filter="data")
    secrets_dir = ProfilePaths(root=root).secrets_dir
    if sys.platform != "win32" and secrets_dir.is_dir():
        secrets_dir.chmod(0o700)
        for path in secrets_dir.iterdir():
            if path.is_file():
                path.chmod(stat.S_IRUSR)
    return extraidos


def _build_wsfex(profile: EnvironmentProfile) -> WsfexClient:
    config = Config(env=profile.environment, paths=profile.paths)
    wsaa = WsaaClient(config)
    return WsfexClient(config, wsaa=wsaa)


def _snapshot_from_seed(
    seed: dict, *, environment: str, cuit_emisor: str
) -> ProfileSnapshot:
    active = seed.get("active_emisor_id")
    chosen = None
    for emisor in seed.get("emisores") or []:
        if active and emisor.get("id") == active:
            chosen = emisor
            break
    if chosen is None and seed.get("emisores"):
        chosen = seed["emisores"][0]
    if chosen is None:
        return ProfileSnapshot(
            environment=environment,
            cuit_emisor=cuit_emisor,
            emisor_id=None,
            emisor_razon_social="",
            emisor_domicilio="",
            emisor_condicion_iva="",
            emisor_iibb="",
            emisor_inicio_actividades="",
        )
    return ProfileSnapshot(
        environment=environment,
        cuit_emisor=cuit_emisor,
        emisor_id=str(chosen["id"]),
        emisor_razon_social=str(chosen.get("razon_social") or ""),
        emisor_domicilio=str(chosen.get("domicilio") or ""),
        emisor_condicion_iva=str(chosen.get("condicion_iva") or ""),
        emisor_iibb=str(chosen.get("iibb") or ""),
        emisor_inicio_actividades=str(
            chosen.get("inicio_actividades") or ""
        ),
    )


def restore_profile_from_seed(
    profile: EnvironmentProfile,
    *,
    seed_path: Path,
    identity_path: Path,
    wsfex: WsfexClient | None = None,
    retries: int = 3,
    abort_on_any_gap: bool = False,
    batch_size: int = 25,
) -> ReconstructReport:
    """Valida identidad → aplica seed → rebuild full por lotes.

    Fail-fast de identidad y setup ``ready`` **antes** de cualquier llamada
    a ARCA o wipe. Usa una conexión dedicada (no la del request app).
    """
    envelope = load_envelope_from_age(seed_path, identity_path)
    seed, manifest = parse_envelope(envelope)

    # Conexión dedicada: no compartir con el threadpool de FastAPI.
    conn = connect(profile.paths.db)
    try:
        assert_profile_ready(profile, conn)
        cuit = validate_seed_identity(
            seed, manifest, profile, conn=conn, require_certificate=True
        )
        meta = load_certificate_metadata(profile)
        if meta is None:
            raise SeedIdentityError(
                "Falta el certificado del perfil tras validar identidad."
            )

        client = wsfex or _build_wsfex(profile)
        snapshot = _snapshot_from_seed(
            seed,
            environment=profile.environment.value,
            cuit_emisor=cuit,
        )
        return reconstruct_register(
            conn,
            client,
            mode=ReconstructMode.FULL,
            targets=targets_from_seed(seed),
            snapshot=snapshot,
            retries=retries,
            abort_on_any_gap=abort_on_any_gap,
            batch_size=batch_size,
            on_begin=lambda c: apply_seed(c, seed),
        )
    finally:
        conn.close()


def catch_up_register(
    profile: EnvironmentProfile,
    wsfex: WsfexClient,
    *,
    retries: int = 3,
    abort_on_any_gap: bool = False,
    batch_size: int = 25,
) -> ReconstructReport:
    """Catch-up FAC-48: append N_local+1..N_arca en lotes (conexión dedicada)."""
    conn = connect(profile.paths.db)
    try:
        assert_profile_ready(profile, conn)
        try:
            cuit = resolve_profile_fiscal_cuit(conn, profile)
        except (FiscalIdentityError, CertificateError) as exc:
            raise SeedIdentityError(str(exc)) from exc
        snapshot = profile_snapshot_from_conn(
            conn, environment=profile.environment.value, cuit_emisor=cuit
        )
        return reconstruct_register(
            conn,
            wsfex,
            mode=ReconstructMode.CATCH_UP,
            targets=targets_from_conn(conn),
            snapshot=snapshot,
            retries=retries,
            abort_on_any_gap=abort_on_any_gap,
            batch_size=batch_size,
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restaurar perfil: seed FAC-44 + rebuild desde ARCA (FAC-65)."
    )
    parser.add_argument(
        "--env", help="ambiente del perfil destino (homo | prod)"
    )
    parser.add_argument(
        "--root", help="raíz explícita del perfil destino (alternativa a --env)"
    )
    parser.add_argument(
        "--seed",
        help="ruta a seed.age (default: backups/seed.age del perfil)",
    )
    parser.add_argument(
        "--identity",
        required=True,
        help="ruta a la identidad age privada de esta máquina",
    )
    parser.add_argument(
        "--abort-on-any-gap",
        action="store_true",
        help="si ARCA confirma un hueco, abortar en vez de registrarlo",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="reintentos ante error transitorio de ARCA (default: 3)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="tamaño de lote de FEXGetCMP por transacción (default: 25)",
    )
    args = parser.parse_args(argv)

    try:
        if not args.env:
            raise BackupError(
                "Indicar --env homo|prod (perfil destino del restore)."
            )
        paths = resolve_profile_paths(args.env, args.root, create=True)
        profile = EnvironmentProfile(
            environment=parse_environment(args.env), paths=paths
        )

        seed_path = Path(args.seed) if args.seed else seed_archive_path(paths)
        identity_path = Path(args.identity)

        print(f"Perfil: {paths.root}")
        print(f"Ambiente: {profile.environment.value}")
        print(f"Seed: {seed_path}")
        print(
            "Validando identidad (cert → env → CUIT → schema → integrity) "
            "y setup ready…"
        )

        report = restore_profile_from_seed(
            profile,
            seed_path=seed_path,
            identity_path=identity_path,
            retries=args.retries,
            abort_on_any_gap=args.abort_on_any_gap,
            batch_size=args.batch_size,
        )
        print(
            f"Rebuild OK: {report.inserted} comprobantes insertados; "
            f"{len(report.gaps)} huecos conocidos."
        )
        for gap in report.gaps:
            print(
                f"  gap: PV {gap.punto_venta} tipo {gap.cbte_tipo} "
                f"nro {gap.cbte_nro}"
            )
        for (pv, tipo), last in sorted(report.last_cmp.items()):
            start, end = report.ranges.get((pv, tipo), (0, 0))
            print(
                f"  PV {pv} tipo {tipo}: rango {start}..{end}, last_CMP={last}"
            )
        return 0
    except (
        BackupError,
        SeedBackupError,
        SeedIdentityError,
        ReconstructError,
        FiscalIdentityError,
        CertificateError,
        ProfileError,
        httpx.HTTPError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def run_restore(
    env: str,
    *,
    seed_path: Path | None = None,
    identity_path: Path,
    root: str | None = None,
    wsfex_factory: Callable[[EnvironmentProfile], WsfexClient] | None = None,
) -> ReconstructReport:
    """Entrypoint programático (launcher)."""
    paths = resolve_profile_paths(env, root, create=True)
    profile = EnvironmentProfile(
        environment=parse_environment(env), paths=paths
    )
    archive = seed_path or seed_archive_path(paths)
    wsfex = wsfex_factory(profile) if wsfex_factory else None
    return restore_profile_from_seed(
        profile,
        seed_path=archive,
        identity_path=identity_path,
        wsfex=wsfex,
    )


if __name__ == "__main__":
    raise SystemExit(main())

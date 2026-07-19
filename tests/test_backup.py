"""Backup/restore (design.md §2.5): snapshot consistente, contenido del
tarball y extracción segura, ahora sobre la raíz de UN perfil (FAC-25).
El cifrado age y el upload S3 quedan afuera (son subprocesos de CLIs
externas); acá se cubre todo lo que arma la app.
"""

import io
import sqlite3
import tarfile

import pytest

from facturador.backup import (
    BackupError,
    backup_s3_settings,
    build_tar,
    resolve_profile_paths,
    snapshot_db,
)
from facturador.profile import ProfilePaths
from facturador.restore import extract


@pytest.fixture
def perfil(tmp_path) -> ProfilePaths:
    """Raíz de perfil con el layout de ProfilePaths y contenido marcado."""
    paths = ProfilePaths(root=tmp_path / "perfil")
    paths.ensure_layout()
    paths.key.write_text("KEY-PRIVADA", encoding="utf-8")
    paths.cert.write_text("CERT", encoding="utf-8")
    paths.pdf_dir.mkdir()
    (paths.pdf_dir / "factura-E-19-00001-00000001-homo.pdf").write_bytes(b"%PDF-")
    paths.logs_dir.mkdir()
    paths.log_file.write_text("ruido", encoding="utf-8")
    conn = sqlite3.connect(paths.db)
    conn.execute("CREATE TABLE invoices (id TEXT)")
    conn.execute("INSERT INTO invoices VALUES ('inv-1')")
    conn.commit()
    conn.close()
    return paths


def _members(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() if m.isfile() else None
                for m in tar.getmembers()}


def test_snapshot_es_una_db_consistente(perfil):
    snapshot = snapshot_db(perfil.db)

    conn = sqlite3.connect(":memory:")
    conn.deserialize(snapshot)
    assert conn.execute("SELECT id FROM invoices").fetchone() == ("inv-1",)
    conn.close()


def test_tar_lleva_estado_del_perfil_sin_db_viva_ni_logs(perfil):
    snapshot = snapshot_db(perfil.db)
    # La DB viva cambia DESPUÉS del snapshot: el tar debe llevar el snapshot.
    conn = sqlite3.connect(perfil.db)
    conn.execute("INSERT INTO invoices VALUES ('inv-post-snapshot')")
    conn.commit()
    conn.close()

    members = _members(build_tar(perfil, snapshot))

    archivos = {k for k, v in members.items() if v is not None}
    # Sin .env: el bootstrap no es estado del perfil (FAC-25).
    # Sin PDFs generados: cache descartable (FAC-53); la DB basta.
    assert archivos == {
        "secrets/cert.key",
        "secrets/cert.crt",
        "data/facturador.db",
    }
    assert members["data/facturador.db"] == snapshot
    assert not any(n.startswith("data/logs") for n in members)
    assert not any(n == "data/pdfs" or n.startswith("data/pdfs/") for n in members)


def test_tar_sin_db_respalda_el_resto(perfil):
    perfil.db.unlink()
    members = _members(build_tar(perfil, None))
    assert "data/facturador.db" not in members
    assert "secrets/cert.key" in members
    assert not any(n.startswith("data/pdfs/") for n in members)


def test_tar_excluye_pdfs_generados_aunque_existan(perfil):
    """FAC-53: el backup normal no incluye el cache local de PDFs."""
    pdf = perfil.pdf_dir / "factura-E-19-00001-00000001-homo.pdf"
    assert pdf.is_file()
    members = _members(build_tar(perfil, snapshot_db(perfil.db)))
    assert "data/facturador.db" in members
    assert "data/pdfs/factura-E-19-00001-00000001-homo.pdf" not in members
    assert pdf.read_bytes() == b"%PDF-"  # el cache local sigue en disco


def test_extract_reconstruye_el_layout_del_perfil(perfil, tmp_path_factory):
    tar_bytes = build_tar(perfil, snapshot_db(perfil.db))
    destino = ProfilePaths(root=tmp_path_factory.mktemp("secundaria"))

    extraidos = extract(tar_bytes, destino.root)

    assert destino.key.read_text(encoding="utf-8") == "KEY-PRIVADA"
    conn = sqlite3.connect(destino.db)
    assert conn.execute("SELECT id FROM invoices").fetchone() == ("inv-1",)
    conn.close()
    assert "secrets/cert.key" in extraidos


def test_extract_rechaza_paths_hostiles(tmp_path):
    """El filtro "data" de tarfile corta path traversal en backups adulterados."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../fuera-del-perfil.txt")
        payload = b"escape"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    destino = tmp_path / "perfil"
    destino.mkdir()
    with pytest.raises(tarfile.OutsideDestinationError):
        extract(buf.getvalue(), destino)
    assert not (tmp_path / "fuera-del-perfil.txt").exists()


# --- resolución explícita del perfil (--env / --root, nunca implícita) ---


def test_resolver_perfil_exige_exactamente_una_eleccion(tmp_path):
    with pytest.raises(BackupError, match="exactamente uno"):
        resolve_profile_paths(None, None)
    with pytest.raises(BackupError, match="exactamente uno"):
        resolve_profile_paths("homo", str(tmp_path))


def test_resolver_perfil_por_root_exige_directorio_existente(tmp_path):
    with pytest.raises(BackupError, match="no existe"):
        resolve_profile_paths(None, str(tmp_path / "no-existe"))


def test_resolver_perfil_por_env_usa_el_app_data(tmp_path, monkeypatch):
    """--env resuelve la MISMA raíz de perfil que la app (app-data del SO);
    el CWD no participa de la resolución."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "facturador.profile.resolve_app_data_root",
        lambda: tmp_path / "appdata",
    )
    (tmp_path / "appdata" / "homo").mkdir(parents=True)

    assert resolve_profile_paths("homo", None).root == tmp_path / "appdata" / "homo"


def test_resolver_perfil_rechaza_ambiente_invalido(tmp_path):
    with pytest.raises(BackupError, match="Ambiente inválido"):
        resolve_profile_paths("staging", None)


def test_resolver_perfil_create_para_la_maquina_secundaria(tmp_path):
    """El restore puede correr en una máquina sin el perfil todavía."""
    destino = tmp_path / "perfil-nuevo"
    paths = resolve_profile_paths(None, str(destino), create=True)
    assert paths.root == destino
    assert paths.secrets_dir.is_dir()


def test_bucket_y_prefijo_salen_de_la_db(perfil):
    """La config de backups vive en la app (tabla settings), no en el
    entorno: se lee del mismo snapshot que se respalda."""
    conn = sqlite3.connect(perfil.db)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO settings VALUES (?, ?)",
        [("backup_s3_bucket", "bucket-de-prueba"), ("backup_s3_prefix", "pfx")],
    )
    conn.commit()
    conn.close()

    snapshot = snapshot_db(perfil.db)
    assert backup_s3_settings(snapshot) == ("bucket-de-prueba", "pfx")


def test_db_vieja_sin_tabla_settings_deja_el_backup_solo_local(perfil):
    snapshot = snapshot_db(perfil.db)
    assert backup_s3_settings(snapshot) == ("", "facturador")
    assert backup_s3_settings(None) == ("", "facturador")


def test_backup_rechaza_una_raiz_sin_db(tmp_path, capsys):
    """FAC-44: el seed necesita la DB del perfil; sin ella falla en claro."""
    from facturador.backup import main

    assert main(["--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "No hay DB" in err or "no existe" in err

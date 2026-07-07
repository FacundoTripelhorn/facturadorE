"""Backup/restore (design.md §2.5): snapshot consistente, contenido del
tarball y extracción segura. El cifrado age y el upload S3 quedan afuera
(son subprocesos de CLIs externas); acá se cubre todo lo que arma la app.
"""

import io
import sqlite3
import tarfile

import pytest

from facturador.backup import BackupError, build_tar, resolve_home, snapshot_db
from facturador.restore import extract


@pytest.fixture
def home(tmp_path):
    """FACTURADOR_HOME con el layout de design.md §2.5 y contenido marcado."""
    (tmp_path / ".env").write_text("ARCA_ENV=homo\n", encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "homo.key").write_text("KEY-PRIVADA", encoding="utf-8")
    (secrets / "homo.crt").write_text("CERT", encoding="utf-8")
    data = tmp_path / "data"
    (data / "pdfs").mkdir(parents=True)
    (data / "pdfs" / "factura-E-00001-00000001-homo.pdf").write_bytes(b"%PDF-")
    (data / "logs").mkdir()
    (data / "logs" / "facturador.log").write_text("ruido", encoding="utf-8")
    conn = sqlite3.connect(data / "facturador.db")
    conn.execute("CREATE TABLE invoices (id TEXT)")
    conn.execute("INSERT INTO invoices VALUES ('inv-1')")
    conn.commit()
    conn.close()
    return tmp_path


def _members(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() if m.isfile() else None
                for m in tar.getmembers()}


def test_snapshot_es_una_db_consistente(home):
    snapshot = snapshot_db(home / "data" / "facturador.db")

    conn = sqlite3.connect(":memory:")
    conn.deserialize(snapshot)
    assert conn.execute("SELECT id FROM invoices").fetchone() == ("inv-1",)
    conn.close()


def test_tar_lleva_estado_completo_sin_db_viva_ni_logs(home):
    snapshot = snapshot_db(home / "data" / "facturador.db")
    # La DB viva cambia DESPUÉS del snapshot: el tar debe llevar el snapshot.
    conn = sqlite3.connect(home / "data" / "facturador.db")
    conn.execute("INSERT INTO invoices VALUES ('inv-post-snapshot')")
    conn.commit()
    conn.close()

    members = _members(build_tar(home, snapshot))

    archivos = {k for k, v in members.items() if v is not None}
    assert archivos == {
        ".env",
        "secrets/homo.key",
        "secrets/homo.crt",
        "data/facturador.db",
        "data/pdfs/factura-E-00001-00000001-homo.pdf",
    }
    assert members["data/facturador.db"] == snapshot
    assert not any(n.startswith("data/logs") for n in members)


def test_tar_sin_db_respalda_el_resto(home):
    (home / "data" / "facturador.db").unlink()
    members = _members(build_tar(home, None))
    assert "data/facturador.db" not in members
    assert "secrets/homo.key" in members


def test_extract_reconstruye_el_layout(home, tmp_path_factory):
    tar_bytes = build_tar(home, snapshot_db(home / "data" / "facturador.db"))
    destino = tmp_path_factory.mktemp("secundaria")

    extraidos = extract(tar_bytes, destino)

    assert (destino / ".env").read_text(encoding="utf-8") == "ARCA_ENV=homo\n"
    assert (destino / "secrets" / "homo.key").read_text(
        encoding="utf-8"
    ) == "KEY-PRIVADA"
    conn = sqlite3.connect(destino / "data" / "facturador.db")
    assert conn.execute("SELECT id FROM invoices").fetchone() == ("inv-1",)
    conn.close()
    assert "secrets/homo.key" in extraidos


def test_extract_rechaza_paths_hostiles(tmp_path):
    """El filtro "data" de tarfile corta path traversal en backups adulterados."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../fuera-del-home.txt")
        payload = b"escape"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    destino = tmp_path / "home"
    destino.mkdir()
    with pytest.raises(tarfile.OutsideDestinationError):
        extract(buf.getvalue(), destino)
    assert not (tmp_path / "fuera-del-home.txt").exists()


def test_resolve_home_exige_directorio_existente(tmp_path):
    with pytest.raises(BackupError):
        resolve_home(str(tmp_path / "no-existe"))

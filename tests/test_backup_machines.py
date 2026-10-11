"""Configuración → Backups: identidad age de esta máquina, máquinas con
acceso (recipients.txt) y "Hacer backup ahora".

La clave privada de la máquina nunca aparece en respuestas HTTP, logs, el
seed ni los objetos de S3.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db, repo
from facturador.age_identity import AgeIdentityError, MachineIdentity, is_public_key
from facturador.api import create_app
from facturador.api.localhost_policy import loopback_base_url
from facturador.arca.wsfex import WsfexClient
from facturador.backup_machines import (
    MaquinasError,
    agregar_maquina,
    listar_maquinas,
    quitar_maquina,
)
from facturador.diagnostics import Nivel, diagnosticar_perfil
from facturador.fiscal_identity import seal_fiscal_cuit
from facturador.profile import machine_identity_path
from facturador.s3_seed import MemoryObjectStore
from facturador.seed_backup import (
    SeedBackupError,
    decrypt_with_identity,
    recipients_path,
    seed_archive_path,
)
from facturador.seed_backup_sync import (
    SeedBackupCoordinator,
    SeedBackupStatus,
    load_seed_backup_state,
)
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import TEST_CUIT, seed_params, seed_settings, with_csrf

KEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
KEY_B = "age1lggyhqrw2nlhcxprm67z43rta597azn8gknawjehu9d9dl0jq3yqqvfafg"

requires_age = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen no están en PATH (CI los instala; ver workflow)",
)


# --- recipients.txt: nombres, alta y baja ---


def test_listar_toma_el_nombre_de_la_linea_de_arriba():
    texto = (
        "# Lista del perfil\n"
        "\n"
        "# Notebook Facu\n"
        f"{KEY_A}\n"
        f"{KEY_B}\n"
    )
    maquinas = listar_maquinas(texto)
    assert [(m.nombre, m.clave) for m in maquinas] == [
        ("Notebook Facu", KEY_A),
        (None, KEY_B),
    ]
    assert maquinas[0].clave_corta == f"{KEY_A[:12]}…{KEY_A[-6:]}"


def test_listar_acepta_comentario_al_final_y_no_repite():
    maquinas = listar_maquinas(f"{KEY_A}  # PC oficina\n{KEY_A}\n")
    assert [(m.nombre, m.clave) for m in maquinas] == [("PC oficina", KEY_A)]


def test_listar_falla_igual_que_el_backup_con_un_archivo_roto():
    with pytest.raises(SeedBackupError):
        listar_maquinas("clave-que-no-es-age\n")


def test_agregar_conserva_el_archivo_y_suma_nombre_y_clave():
    original = f"# Comentario general\n# Notebook\n{KEY_A}\n"
    nuevo = agregar_maquina(original, nombre="  PC   oficina ", clave=f" {KEY_B} ")
    assert nuevo == original + f"# PC oficina\n{KEY_B}\n"
    assert agregar_maquina("", nombre="", clave=KEY_B) == f"{KEY_B}\n"
    # Sin salto de línea final, la clave nueva no se pega a la anterior.
    assert agregar_maquina(KEY_A, nombre="x", clave=KEY_B) == (
        f"{KEY_A}\n# x\n{KEY_B}\n"
    )


def test_agregar_normaliza_mayusculas():
    nuevo = agregar_maquina("", nombre="PC", clave=KEY_B.upper())
    assert nuevo == f"# PC\n{KEY_B}\n"


@pytest.mark.parametrize(
    "clave",
    [
        "",
        "hola",
        "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ",
        KEY_B[:-1],
        KEY_B + "q",
        KEY_B[:-1] + "b",  # fuera del alfabeto bech32
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9ok",
    ],
)
def test_agregar_rechaza_lo_que_no_es_una_clave_publica_age(clave):
    with pytest.raises(MaquinasError, match="clave pública age"):
        agregar_maquina(f"{KEY_A}\n", nombre="x", clave=clave)


def test_agregar_rechaza_una_clave_repetida():
    with pytest.raises(MaquinasError, match="ya está"):
        agregar_maquina(f"# A\n{KEY_A}\n", nombre="otra", clave=KEY_A)


def test_quitar_saca_la_clave_y_su_nombre_y_deja_el_resto():
    texto = (
        "# Lista del perfil\n"
        "\n"
        "# Notebook\n"
        f"{KEY_A}\n"
        "# PC oficina\n"
        f"{KEY_B}\n"
    )
    assert quitar_maquina(texto, clave=KEY_B) == (
        "# Lista del perfil\n\n# Notebook\n" f"{KEY_A}\n"
    )
    assert quitar_maquina(texto, clave=KEY_A) == (
        "# Lista del perfil\n\n# PC oficina\n" f"{KEY_B}\n"
    )


def test_no_se_puede_quitar_la_ultima_ni_una_que_no_esta():
    with pytest.raises(MaquinasError, match="última"):
        quitar_maquina(f"# A\n{KEY_A}\n", clave=KEY_A)
    with pytest.raises(MaquinasError, match="ya no está"):
        quitar_maquina(f"{KEY_A}\n", clave=KEY_B)


# --- identidad de la máquina ---


def test_la_identidad_vive_en_el_app_data_fuera_de_los_perfiles(test_profile):
    path = machine_identity_path(test_profile.paths)
    assert path == test_profile.paths.root.parent / "age" / "key.txt"
    assert test_profile.paths.root not in path.parents


def test_sin_identidad_no_hay_clave(tmp_path):
    assert MachineIdentity(tmp_path / "age" / "key.txt").public_key() is None


def test_sin_age_keygen_el_error_es_claro(tmp_path):
    identidad = MachineIdentity(
        tmp_path / "age" / "key.txt", keygen_bin="no-existe-age-keygen"
    )
    with pytest.raises(AgeIdentityError, match="No se encontró age-keygen"):
        identidad.create()
    assert not identidad.exists()


@requires_age
def test_crear_identidad_y_no_pisarla(tmp_path):
    identidad = MachineIdentity(tmp_path / "age" / "key.txt")
    clave = identidad.create()
    assert is_public_key(clave)
    assert identidad.public_key() == clave
    contenido = identidad.path.read_bytes()
    if sys.platform != "win32":
        assert identidad.path.stat().st_mode & 0o777 == 0o600
        assert identidad.path.parent.stat().st_mode & 0o777 == 0o700
    # Ya existe: se devuelve la misma y el archivo queda intacto.
    assert identidad.create() == clave
    assert identidad.path.read_bytes() == contenido
    # El temporal de la generación no queda tirado.
    assert sorted(p.name for p in identidad.path.parent.iterdir()) == ["key.txt"]


# --- diagnóstico ---


def _identidad(api) -> MachineIdentity:
    return MachineIdentity.for_profile(api.app.state.profile.paths)


def _chequeo_identidad(api):
    chequeos = diagnosticar_perfil(api.app.state.profile, api.conn)
    return next(c for c in chequeos if c.titulo == "Identidad de esta máquina")


def test_diagnostico_sin_identidad_es_aviso(api):
    chequeo = _chequeo_identidad(api)
    assert chequeo.nivel is Nivel.AVISO
    assert "no tiene identidad" in chequeo.detalle
    assert chequeo.accion is not None
    assert chequeo.accion.enlace == "/configuracion#backups"


@requires_age
def test_diagnostico_identidad_fuera_y_dentro_de_la_lista(api):
    clave = _identidad(api).create()
    recipients_path(api.app.state.profile.paths).write_text(
        f"{KEY_A}\n", encoding="utf-8"
    )
    chequeo = _chequeo_identidad(api)
    assert chequeo.nivel is Nivel.AVISO
    assert "no puede restaurar el backup de este ambiente" in chequeo.detalle

    recipients_path(api.app.state.profile.paths).write_text(
        f"{KEY_A}\n{clave}\n", encoding="utf-8"
    )
    chequeo = _chequeo_identidad(api)
    assert chequeo.nivel is Nivel.OK
    # Es un aviso, no bloquea: la página sigue diciendo que se puede emitir.
    assert "No se puede emitir." not in api.get("/diagnostico").text


# --- Configuración → Backups ---


class _TimerInmediato:
    """Reemplaza el debounce: el backup corre en el acto, en el mismo hilo."""

    def __init__(self, interval, function):
        self.function = function
        self.daemon = True

    def start(self):
        self.function()

    def cancel(self):
        pass


@pytest.fixture
def backups(test_profile, test_config):
    """App con un coordinator real (age de verdad) contra un S3 en memoria.

    ``corridas`` cuenta los backups que corrieron.
    """
    conn = db.connect(test_profile.paths.db)
    seed_params(conn)
    seed_settings(conn)
    seal_fiscal_cuit(conn, TEST_CUIT)
    store = MemoryObjectStore()
    corridas: list[str] = []

    class Coordinator(SeedBackupCoordinator):
        def _execute(self):
            corridas.append("backup")
            return super()._execute()

    coord = Coordinator(
        conn,
        test_profile.paths,
        environment="homo",
        store=store,
        timer_factory=_TimerInmediato,
    )
    arca = FakeArca()
    wsfex = WsfexClient(
        test_config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(
        test_profile, config=test_config, conn=conn, wsfex=wsfex, seed_backup=coord
    )
    client = TestClient(app, base_url=loopback_base_url())
    client.conn = conn
    client.store = store
    client.corridas = corridas
    client.paths = test_profile.paths
    return client


def _post(client, path, data=None):
    return client.post(path, data=with_csrf(client, data), follow_redirects=True)


def test_pagina_sin_identidad_ofrece_crearla(backups):
    r = backups.get("/configuracion")
    assert r.status_code == 200
    assert "Crear identidad de esta máquina" in r.text
    assert "Todavía no hay máquinas con acceso" in r.text
    assert "Hacer backup ahora" not in r.text
    assert "passphrase" not in r.text
    assert "con las claves públicas" in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/ui/configuracion/backup/identidad",
        "/ui/configuracion/backup/esta-maquina",
        "/ui/configuracion/backup/maquinas",
        "/ui/configuracion/backup/maquinas/quitar",
        "/ui/configuracion/backup/ahora",
    ],
)
def test_formularios_exigen_csrf(backups, path):
    r = backups.post(path, data={"clave": KEY_A, "nombre": "x"})
    assert r.status_code == 403
    assert not machine_identity_path(backups.paths).exists()
    assert not recipients_path(backups.paths).exists()
    assert backups.corridas == []


@requires_age
def test_crear_agregar_y_hacer_backup_sin_terminal(backups):
    r = _post(backups, "/ui/configuracion/backup/identidad")
    assert r.status_code == 200
    assert "Identidad de esta máquina creada." in r.text
    clave = MachineIdentity.for_profile(backups.paths).public_key()
    assert clave and clave in r.text
    assert "Esta máquina no puede restaurar el backup de este ambiente" in r.text
    assert "Agregar esta máquina" in r.text
    assert backups.corridas == []

    r = _post(backups, "/ui/configuracion/backup/esta-maquina")
    assert r.status_code == 200
    assert "Máquina agregada." in r.text
    assert "Esta máquina no puede restaurar" not in r.text
    assert "Esta máquina</span>" in r.text
    assert backups.corridas == ["backup"]  # un solo backup por cambio
    texto = recipients_path(backups.paths).read_text(encoding="utf-8")
    assert texto.endswith(f"{clave}\n")
    assert texto.startswith("# ")  # con el nombre del equipo arriba

    estado = load_seed_backup_state(backups.conn)
    assert estado.status is SeedBackupStatus.OK
    assert estado.last_destination == "local"  # sin bucket
    assert seed_archive_path(backups.paths).is_file()
    assert "guardado solo en esta máquina" in r.text

    r = _post(backups, "/ui/configuracion/backup/ahora")
    assert "Backup hecho." in r.text
    assert backups.corridas == ["backup", "backup"]


@requires_age
def test_backup_ahora_sube_a_s3_con_bucket(backups):
    repo.save_settings(backups.conn, {"backup_s3_bucket": "mi-bucket"})
    recipients_path(backups.paths).parent.mkdir(parents=True, exist_ok=True)
    recipients_path(backups.paths).write_text(f"{KEY_A}\n", encoding="utf-8")
    r = _post(backups, "/ui/configuracion/backup/ahora")
    assert "Backup hecho." in r.text
    assert "subido a S3" in r.text
    assert load_seed_backup_state(backups.conn).last_destination == "s3"
    claves = {k for (_, k) in backups.store.objects}
    assert any(k.endswith("/seed.age") for k in claves)
    assert any(k.endswith("/recipients.txt") for k in claves)
    # El API devuelve lo mismo.
    assert backups.get("/backup/seed").json()["last_destination"] == "s3"


def test_backup_ahora_fallido_muestra_error_sin_detalle(backups):
    recipients_path(backups.paths).parent.mkdir(parents=True, exist_ok=True)
    recipients_path(backups.paths).write_text(f"{KEY_A}\n", encoding="utf-8")
    shutil_which = shutil.which

    def sin_age(cmd, *args, **kwargs):
        return None if cmd == "age" else shutil_which(cmd, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "which", sin_age)
        r = _post(backups, "/ui/configuracion/backup/ahora")
    assert "No se pudo hacer el backup." in r.text
    assert "Falló el último intento" in r.text
    assert str(backups.paths.root) not in r.text
    assert load_seed_backup_state(backups.conn).status is SeedBackupStatus.FAILED


def test_agregar_otra_maquina_y_quitarla(backups):
    recipients_path(backups.paths).parent.mkdir(parents=True, exist_ok=True)
    recipients_path(backups.paths).write_text(
        f"# No tocar este comentario\n# Notebook\n{KEY_A}\n", encoding="utf-8"
    )
    r = _post(
        backups,
        "/ui/configuracion/backup/maquinas",
        {"nombre": "PC oficina", "clave": KEY_B},
    )
    assert r.status_code == 200
    assert "PC oficina" in r.text and "Notebook" in r.text
    assert len(backups.corridas) == 1
    assert recipients_path(backups.paths).read_text(encoding="utf-8") == (
        f"# No tocar este comentario\n# Notebook\n{KEY_A}\n# PC oficina\n{KEY_B}\n"
    )

    r = _post(backups, "/ui/configuracion/backup/maquinas/quitar", {"clave": KEY_A})
    assert r.status_code == 200
    assert "Máquina quitada." in r.text
    assert len(backups.corridas) == 2
    assert recipients_path(backups.paths).read_text(encoding="utf-8") == (
        f"# No tocar este comentario\n# PC oficina\n{KEY_B}\n"
    )
    # Con una sola máquina no se ofrece quitarla, y el POST se rechaza.
    assert "Quitar</button>" not in r.text
    r = _post(backups, "/ui/configuracion/backup/maquinas/quitar", {"clave": KEY_B})
    assert r.status_code == 422
    assert "No se puede quitar la última máquina" in r.text
    assert len(backups.corridas) == 2


def test_agregar_invalida_o_repetida_no_escribe_ni_dispara_backup(backups):
    recipients_path(backups.paths).parent.mkdir(parents=True, exist_ok=True)
    recipients_path(backups.paths).write_text(f"{KEY_A}\n", encoding="utf-8")
    r = _post(
        backups, "/ui/configuracion/backup/maquinas",
        {"nombre": "Mi PC", "clave": "age1nolaes"},
    )
    assert r.status_code == 422
    assert "clave pública age" in r.text
    assert 'value="age1nolaes"' in r.text  # el formulario conserva lo cargado
    assert 'value="Mi PC"' in r.text
    r = _post(
        backups, "/ui/configuracion/backup/maquinas", {"nombre": "x", "clave": KEY_A}
    )
    assert r.status_code == 422
    assert "Esa clave ya está en la lista." in r.text
    assert recipients_path(backups.paths).read_text(encoding="utf-8") == f"{KEY_A}\n"
    assert backups.corridas == []


def test_quitar_la_de_esta_maquina_avisa_que_deja_de_poder_restaurar(backups):
    class Fija(MachineIdentity):
        def public_key(self):
            return KEY_A

    backups.app.state.service.machine_identity = Fija(Path("no-se-usa"))
    recipients_path(backups.paths).parent.mkdir(parents=True, exist_ok=True)
    recipients_path(backups.paths).write_text(f"{KEY_A}\n{KEY_B}\n", encoding="utf-8")
    r = backups.get("/configuracion")
    assert "Es la clave de esta máquina" in r.text
    assert r.text.count("¿Quitar esta máquina?") == 1  # la otra fila


def test_lista_rota_se_muestra_sin_romper_la_pagina(backups):
    recipients_path(backups.paths).parent.mkdir(parents=True, exist_ok=True)
    recipients_path(backups.paths).write_text("esto-no-es-age\n", encoding="utf-8")
    r = backups.get("/configuracion")
    assert r.status_code == 200
    assert "se esperaba una clave" in r.text


# --- la clave privada no sale de su archivo ---


@requires_age
def test_la_clave_privada_no_aparece_en_http_logs_seed_ni_s3(backups, caplog):
    caplog.set_level(logging.DEBUG)
    repo.save_settings(backups.conn, {"backup_s3_bucket": "mi-bucket"})
    respuestas = [
        _post(backups, "/ui/configuracion/backup/identidad"),
        _post(backups, "/ui/configuracion/backup/esta-maquina"),
        _post(
            backups, "/ui/configuracion/backup/maquinas",
            {"nombre": "Otra", "clave": KEY_A},
        ),
        _post(backups, "/ui/configuracion/backup/ahora"),
        _post(backups, "/ui/configuracion/backup/maquinas/quitar", {"clave": KEY_A}),
        backups.get("/configuracion"),
        backups.get("/diagnostico"),
        backups.get("/backup/seed"),
        backups.post("/backup/seed"),
        backups.get("/setup"),
        backups.get("/health"),
    ]
    identidad = machine_identity_path(backups.paths)
    secreto = next(
        line.strip()
        for line in identidad.read_text(encoding="utf-8").splitlines()
        if line.startswith("AGE-SECRET-KEY-")
    )
    clave = MachineIdentity(identidad).public_key()
    assert load_seed_backup_state(backups.conn).status is SeedBackupStatus.OK

    def sin_secreto(texto: str) -> bool:
        return secreto not in texto and secreto.lower() not in texto.lower() and (
            "AGE-SECRET-KEY" not in texto.upper()
        )

    # HTTP: ni la clave privada ni el marcador; la pública sí se muestra.
    for r in respuestas:
        assert r.status_code == 200, r.url
        assert sin_secreto(r.text), r.url
        assert all(sin_secreto(v) for v in r.headers.values()), r.url
    assert clave in respuestas[-6].text

    # Logs.
    assert sin_secreto(caplog.text)

    # S3: seed cifrado y recipients en claro, ninguno con la clave privada.
    assert backups.store.objects
    for (_, key), body in backups.store.objects.items():
        assert sin_secreto(body.decode("latin-1")), key

    # Seed: abrirlo con la identidad de esta máquina (prueba que la clave
    # está en la lista) y revisar el contenido en claro.
    seed = decrypt_with_identity(
        seed_archive_path(backups.paths).read_bytes(), identidad
    )
    assert b'"format": "facturador.seed"' in seed
    assert sin_secreto(seed.decode("utf-8"))

    # Ningún archivo del perfil (DB, backups, logs) la contiene.
    for path in backups.paths.root.rglob("*"):
        if path.is_file():
            assert sin_secreto(path.read_bytes().decode("latin-1")), path

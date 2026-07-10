"""Config de arranque: ambiente explícito inyectado (FAC-24), derivación
atómica de URLs por ambiente (checklist §2.1.1 punto 1), paths de runtime
saliendo SOLO del perfil (FAC-25) y resolución única del home de bootstrap
(sin fallback al CWD)."""

import os

import pytest

from facturador.config import (
    Config,
    ConfigError,
    ensure_home,
    load_config,
    resolve_boot_environment,
    resolve_home,
)
from facturador.constants import ArcaEnvironment
from facturador.profile import EnvironmentProfile, resolve_isolated_profiles


@pytest.fixture(autouse=True)
def _entorno_limpio():
    """load_dotenv escribe en os.environ: aislar cada test para que un .env
    leído en uno no contamine a los demás."""
    claves = ("ARCA_ENV", "FACTURADOR_HOME")
    previo = {k: os.environ.get(k) for k in claves}
    for k in claves:
        os.environ.pop(k, None)
    yield
    for k, v in previo.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _perfil_con_certs(environment, root):
    """Perfil aislado con par cert/key de mentira y permisos válidos."""
    profile = EnvironmentProfile.for_testing(environment, root)
    profile.paths.ensure_layout()
    profile.paths.cert.write_text("CERT")
    profile.paths.key.write_text("KEY")
    profile.paths.key.chmod(0o400)
    return profile


# --- derivación por ambiente del perfil ---


def test_homo_deriva_urls_de_homologacion(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    assert "wsaahomo.afip.gov.ar" in config.wsaa_url
    assert "wswhomo.afip.gov.ar" in config.wsfex_url


def test_prod_deriva_urls_de_produccion(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    config = Config(env=ArcaEnvironment.PROD, paths=profile.paths)
    assert "wsaa.afip.gov.ar" in config.wsaa_url
    assert "servicios1.afip.gov.ar" in config.wsfex_url


def test_no_existe_forma_de_mezclar_ambiente_y_certificado(tmp_path):
    # Las URLs son propiedades derivadas del ambiente y los paths salen del
    # perfil: no hay campos independientes que permitan configurar URL de
    # prod con cert de homo. El resto de la configuración (emisor, punto de
    # venta, backups) vive en la DB del perfil.
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    assert not hasattr(config, "wsaa_url_override")
    assert config.__dataclass_fields__.keys() == {"env", "paths"}


# --- paths de runtime a través del perfil (FAC-25) ---


def test_todos_los_paths_de_runtime_salen_del_perfil(tmp_path):
    profile = _perfil_con_certs(ArcaEnvironment.HOMO, tmp_path / "perfil")
    config = load_config(profile)

    root = profile.paths.root
    for path in (
        config.paths.db,
        config.paths.cert,
        config.paths.key,
        config.paths.pdf_dir,
        config.paths.wsaa_ta_cache,
        config.paths.logs_dir,
        config.paths.backups_dir,
        config.paths.onboarding,
    ):
        # Nada cae al CWD ni a un home compartido: todo vive bajo la raíz.
        assert path.is_relative_to(root), path


def test_homo_y_prod_usan_raices_fisicamente_separadas(tmp_path):
    homo, prod = resolve_isolated_profiles(app_data_root=tmp_path / "appdata")
    config_homo = Config(env=ArcaEnvironment.HOMO, paths=homo.paths)
    config_prod = Config(env=ArcaEnvironment.PROD, paths=prod.paths)

    assert config_homo.paths.root != config_prod.paths.root
    assert not config_homo.paths.db.is_relative_to(config_prod.paths.root)
    assert not config_prod.paths.cert.is_relative_to(config_homo.paths.root)


def test_env_invalido_rechazado(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCA_ENV", "staging")
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="ARCA_ENV"):
        resolve_boot_environment()


def test_env_ausente_rechazado_sin_default_silencioso(monkeypatch, tmp_path):
    """FAC-24: sin ARCA_ENV no hay ambiente 'homo' implícito — el backend
    arranca contra exactamente un ambiente explícito o no arranca."""
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    # ensure_home respeta un .env existente: este no trae ARCA_ENV a propósito.
    (tmp_path / ".env").write_text("# sin ambiente\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ARCA_ENV no está definido"):
        resolve_boot_environment()


def test_primer_arranque_sin_eleccion_explicita_falla(monkeypatch, tmp_path):
    """Review de Codex en PR #34: en un home FRESCO el bootstrap auto-creado
    no debe activar homo en silencio — el primer arranque falla hasta que
    alguien elige ambiente."""
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path / "fresco"))

    with pytest.raises(ConfigError, match="ARCA_ENV no está definido"):
        resolve_boot_environment()


def test_arranque_rechazado_sin_certificados(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    with pytest.raises(ConfigError, match="cert.crt"):
        load_config(profile)


def test_arranque_rechazado_con_permisos_laxos_en_la_key(tmp_path):
    profile = _perfil_con_certs(ArcaEnvironment.HOMO, tmp_path / "h")
    profile.paths.key.chmod(0o644)
    with pytest.raises(ConfigError, match="Permisos laxos"):
        load_config(profile)


def test_load_config_crea_la_estructura_del_perfil(tmp_path):
    profile = _perfil_con_certs(ArcaEnvironment.HOMO, tmp_path / "h")
    config = load_config(profile)

    assert config.paths is profile.paths
    assert config.paths.data_dir.is_dir()
    assert config.paths.backups_dir.is_dir()


# --- resolución del home de bootstrap (única, sin CWD) ---


def test_home_default_es_facturador_en_el_home_del_usuario(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_home() == tmp_path / "facturador"


def test_facturador_home_del_entorno_gana_al_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path / "otro"))
    assert resolve_home() == tmp_path / "otro"


def test_primer_arranque_crea_solo_el_env_bootstrap(monkeypatch, tmp_path):
    """El home ya no aloja archivos de runtime (FAC-25): el primer arranque
    solo deja el .env de bootstrap; secrets/data/backups viven en el perfil."""
    home = tmp_path / "facturador"
    monkeypatch.setenv("FACTURADOR_HOME", str(home))
    with pytest.raises(ConfigError, match="ARCA_ENV no está definido"):
        resolve_boot_environment()
    assert not (home / "secrets").exists()
    assert not (home / "data").exists()
    assert not (home / "backups").exists()
    # El esqueleto documenta el flag pero NO activa un ambiente (FAC-24).
    bootstrap = (home / ".env").read_text(encoding="utf-8")
    assert "#ARCA_ENV=homo" in bootstrap
    assert not any(
        linea.strip().startswith("ARCA_ENV") for linea in bootstrap.splitlines()
    )


def test_ensure_home_no_pisa_un_env_existente(tmp_path):
    (tmp_path / ".env").write_text("ARCA_ENV=prod\n", encoding="utf-8")
    ensure_home(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "ARCA_ENV=prod\n"


def test_env_se_lee_del_home_nunca_del_cwd(monkeypatch, tmp_path):
    """La regla: <home>/.env es EL .env. Uno en el directorio de trabajo
    (el viejo hábito del spike) se ignora por completo."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / ".env").write_text("ARCA_ENV=prod\n", encoding="utf-8")
    monkeypatch.chdir(cwd)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("ARCA_ENV=homo\n", encoding="utf-8")
    monkeypatch.setenv("FACTURADOR_HOME", str(home))

    assert resolve_boot_environment() is ArcaEnvironment.HOMO


def test_env_del_home_elige_el_perfil_del_ambiente(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ARCA_ENV=prod\n", encoding="utf-8")
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))

    environment = resolve_boot_environment()
    assert environment is ArcaEnvironment.PROD
    profile = EnvironmentProfile.resolve(
        environment, app_data_root=tmp_path / "appdata"
    )
    assert profile.paths.root == tmp_path / "appdata" / "prod"

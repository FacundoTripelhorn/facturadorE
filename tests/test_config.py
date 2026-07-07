"""Config de arranque: derivación atómica por ARCA_ENV (checklist §2.1.1
punto 1) y resolución única del home (sin fallback al CWD)."""

import os

import pytest

from facturador.config import (
    Config,
    ConfigError,
    ensure_home,
    load_config,
    resolve_home,
)


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


def _con_certs(home, env="homo"):
    """Par cert/key de mentira con permisos válidos para pasar el arranque."""
    secrets = home / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / f"{env}.crt").write_text("CERT")
    key = secrets / f"{env}.key"
    key.write_text("KEY")
    key.chmod(0o400)
    return home


# --- derivación por ARCA_ENV ---


def test_homo_deriva_urls_y_certs_de_homologacion(tmp_path):
    config = Config(env="homo", home=tmp_path)
    assert "wsaahomo.afip.gov.ar" in config.wsaa_url
    assert "wswhomo.afip.gov.ar" in config.wsfex_url
    assert config.cert_path.name == "homo.crt"
    assert config.key_path.name == "homo.key"


def test_prod_deriva_urls_y_certs_de_produccion(tmp_path):
    config = Config(env="prod", home=tmp_path)
    assert "wsaa.afip.gov.ar" in config.wsaa_url
    assert "servicios1.afip.gov.ar" in config.wsfex_url
    assert config.cert_path.name == "prod.crt"
    assert config.key_path.name == "prod.key"


def test_no_existe_forma_de_mezclar_ambiente_y_certificado(tmp_path):
    # Las URLs y paths son propiedades derivadas: no hay campos independientes
    # que permitan configurar URL de prod con cert de homo. El resto de la
    # configuración (emisor, punto de venta, backups) vive en la DB.
    config = Config(env="homo", home=tmp_path)
    assert not hasattr(config, "wsaa_url_override")
    assert config.__dataclass_fields__.keys() == {"env", "home"}


def test_env_invalido_rechazado(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCA_ENV", "staging")
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="ARCA_ENV"):
        load_config()


def test_arranque_rechazado_sin_certificados(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="homo.crt"):
        load_config()


def test_arranque_rechazado_con_permisos_laxos_en_la_key(monkeypatch, tmp_path):
    _con_certs(tmp_path)
    (tmp_path / "secrets" / "homo.key").chmod(0o644)
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="Permisos laxos"):
        load_config()


# --- resolución del home (única, sin CWD) ---


def test_home_default_es_facturador_en_el_home_del_usuario(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_home() == tmp_path / "facturador"


def test_facturador_home_del_entorno_gana_al_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path / "otro"))
    assert resolve_home() == tmp_path / "otro"


def test_primer_arranque_crea_la_estructura_y_el_env_bootstrap(
    monkeypatch, tmp_path
):
    home = tmp_path / "facturador"
    monkeypatch.setenv("FACTURADOR_HOME", str(home))
    with pytest.raises(ConfigError, match="homo.crt"):  # certs a mano (FAC-4)
        load_config()
    assert (home / "secrets").is_dir()
    assert (home / "data").is_dir()
    assert (home / "backups").is_dir()
    assert "ARCA_ENV=homo" in (home / ".env").read_text(encoding="utf-8")


def test_ensure_home_no_pisa_un_env_existente(tmp_path):
    (tmp_path / ".env").write_text("ARCA_ENV=prod\n", encoding="utf-8")
    ensure_home(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "ARCA_ENV=prod\n"


def test_env_se_lee_del_home_nunca_del_cwd(monkeypatch, tmp_path):
    """La regla nueva: <home>/.env es EL .env. Uno en el directorio de
    trabajo (el viejo hábito del spike) se ignora por completo."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / ".env").write_text("ARCA_ENV=prod\n", encoding="utf-8")
    monkeypatch.chdir(cwd)

    home = _con_certs(tmp_path / "home")
    (home / ".env").write_text("ARCA_ENV=homo\n", encoding="utf-8")
    monkeypatch.setenv("FACTURADOR_HOME", str(home))

    assert load_config().env == "homo"


def test_env_del_home_configura_el_ambiente(monkeypatch, tmp_path):
    home = _con_certs(tmp_path, env="prod")
    (home / ".env").write_text("ARCA_ENV=prod\n", encoding="utf-8")
    monkeypatch.setenv("FACTURADOR_HOME", str(home))

    assert load_config().env == "prod"

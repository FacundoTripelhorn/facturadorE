"""Config: derivación atómica por ARCA_ENV (checklist §2.1.1 punto 1)."""

import pytest

from facturador.config import Config, ConfigError, load_config


def test_homo_deriva_urls_y_certs_de_homologacion(tmp_path):
    config = Config(env="homo", home=tmp_path, cuit=None, key_passphrase=None)
    assert "wsaahomo.afip.gov.ar" in config.wsaa_url
    assert "wswhomo.afip.gov.ar" in config.wsfex_url
    assert config.cert_path.name == "homo.crt"
    assert config.key_path.name == "homo.key"


def test_prod_deriva_urls_y_certs_de_produccion(tmp_path):
    config = Config(env="prod", home=tmp_path, cuit=None, key_passphrase=None)
    assert "wsaa.afip.gov.ar" in config.wsaa_url
    assert "servicios1.afip.gov.ar" in config.wsfex_url
    assert config.cert_path.name == "prod.crt"
    assert config.key_path.name == "prod.key"


def test_no_existe_forma_de_mezclar_ambiente_y_certificado(tmp_path):
    # Las URLs y paths son propiedades derivadas: no hay campos independientes
    # que permitan configurar URL de prod con cert de homo.
    config = Config(env="homo", home=tmp_path, cuit=None, key_passphrase=None)
    assert not hasattr(config, "wsaa_url_override")
    assert config.__dataclass_fields__.keys() == {
        "env",
        "home",
        "cuit",
        "key_passphrase",
        "punto_venta",
        "emisor",  # datos del PDF (fase 5); no afecta URLs ni certificados
    }


def test_env_invalido_rechazado(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCA_ENV", "staging")
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="ARCA_ENV"):
        load_config(env_file=None)


def test_arranque_rechazado_sin_certificados(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCA_ENV", "homo")
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="homo.crt"):
        load_config(env_file=None)


def test_cuit_invalido_rechazado(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCA_ENV", "homo")
    monkeypatch.setenv("FACTURADOR_HOME", str(tmp_path))
    monkeypatch.setenv("ARCA_CUIT", "20-11111111-2")
    with pytest.raises(ConfigError, match="ARCA_CUIT"):
        load_config(env_file=None)

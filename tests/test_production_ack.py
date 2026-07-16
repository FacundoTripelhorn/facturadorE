"""Ack de primer uso de Producción en el backend (FAC-40)."""

from __future__ import annotations

import pytest

from facturador.config import ConfigError, load_config
from facturador.constants import ArcaEnvironment
from facturador.production_ack import (
    ACK_PRODUCTION_ENV,
    ProductionAckRequired,
    is_production_acknowledged,
    require_production_ack,
    save_production_ack,
)
from facturador.profile import EnvironmentProfile, ProfilePaths


def test_require_production_ack_homo_no_op(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    require_production_ack(profile)
    assert not (tmp_path / "h" / "data" / "production_ack.json").exists()


def test_require_production_ack_bloquea_prod_sin_ack(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(ACK_PRODUCTION_ENV, raising=False)
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    with pytest.raises(ProductionAckRequired, match="validez fiscal"):
        require_production_ack(profile)


def test_require_production_ack_env_persiste_y_permite(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv(ACK_PRODUCTION_ENV, "1")
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    require_production_ack(profile)
    assert is_production_acknowledged(profile.paths)

    monkeypatch.delenv(ACK_PRODUCTION_ENV)
    # Segundo arranque: ack en disco, sin env.
    require_production_ack(profile)


def test_load_config_prod_sin_ack_fuera_de_pytest(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(ACK_PRODUCTION_ENV, raising=False)
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    profile.paths.ensure_layout()
    with pytest.raises(ConfigError, match="FACTURADOR_ACK_PRODUCTION"):
        load_config(profile)


def test_load_config_prod_con_ack_previo(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(ACK_PRODUCTION_ENV, raising=False)
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    save_production_ack(ProfilePaths(root=profile.paths.root))
    config = load_config(profile)
    assert config.env is ArcaEnvironment.PROD

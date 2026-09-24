"""Arranque contra un perfil de ambiente explícito e inmutable.

Acceptance criteria del issue: un proceso = un ambiente inmutable; los
servicios no pueden reemplazarlo; la config de ARCA deriva solo del perfil
inyectado; apps de homo y prod se crean con perfiles temporales; arrancar
sin perfil explícito falla claro.
"""

import dataclasses

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db
from facturador.api import create_app
from facturador.api.localhost_policy import loopback_base_url
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config
from facturador.constants import WSAA_URLS, WSFEX_URLS, ArcaEnvironment
from facturador.profile import EnvironmentProfile, ProfileError
from tests.arca_fake import FakeArca, FakeWsaa


def _app_para(environment: ArcaEnvironment, tmp_path):
    """App real con DB y perfil temporales y ARCA simulado, por ambiente."""
    profile = EnvironmentProfile.for_testing(
        environment, tmp_path / "profiles" / environment.value
    )
    config = Config(env=environment, paths=profile.paths)
    conn = db.connect(profile.paths.db)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(FakeArca().handler)),
    )
    return create_app(profile, config=config, conn=conn, wsfex=wsfex), profile


@pytest.mark.parametrize("environment", list(ArcaEnvironment))
def test_apps_de_homo_y_prod_con_perfiles_temporales(environment, tmp_path):
    app, profile = _app_para(environment, tmp_path)

    assert app.state.profile is profile
    assert app.state.profile.environment is environment
    respuesta = TestClient(app, base_url=loopback_base_url()).get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json()["environment"] == environment.value


@pytest.mark.parametrize("environment", list(ArcaEnvironment))
def test_urls_de_arca_derivan_solo_del_perfil(environment, tmp_path):
    app, _ = _app_para(environment, tmp_path)

    config = app.state.service.config
    assert config.env is environment
    assert config.wsaa_url == WSAA_URLS[environment.value]
    assert config.wsfex_url == WSFEX_URLS[environment.value]


def test_config_y_perfil_de_ambientes_distintos_rechazados(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, tmp_path / "p")
    otro = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    config = Config(env=ArcaEnvironment.HOMO, paths=otro.paths)

    with pytest.raises(ProfileError, match="no coinciden"):
        create_app(profile, config=config)


def test_config_con_paths_de_otro_perfil_rechazada(tmp_path):
    """Mismo ambiente no alcanza — los paths de la Config tienen que
    salir de la raíz del perfil inyectado, no de cualquier otra."""
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    otro = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h2")
    config = Config(env=ArcaEnvironment.HOMO, paths=otro.paths)

    with pytest.raises(ProfileError, match="no coinciden"):
        create_app(profile, config=config)


def test_crear_app_sin_perfil_explicito_falla(tmp_path):
    perfil = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")
    with pytest.raises(TypeError):
        create_app(config=Config(env=ArcaEnvironment.HOMO, paths=perfil.paths))


def test_el_ambiente_es_inmutable_despues_de_crear_la_app(tmp_path):
    app, profile = _app_para(ArcaEnvironment.HOMO, tmp_path)

    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.environment = ArcaEnvironment.PROD  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        app.state.service.config.env = ArcaEnvironment.PROD

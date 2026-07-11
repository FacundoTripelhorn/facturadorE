"""Perfiles de ambiente aislados (FAC-23)."""

from pathlib import Path, PureWindowsPath

import pytest

from facturador.constants import ArcaEnvironment
from facturador.profile import (
    EnvironmentProfile,
    ProfileError,
    ProfilePaths,
    ensure_profile_roots_differ,
    parse_environment,
    resolve_app_data_root,
    resolve_isolated_profiles,
    resolve_profile_root,
)


def test_parse_environment_acepta_homo_y_prod():
    assert parse_environment("homo") is ArcaEnvironment.HOMO
    assert parse_environment("  PROD ") is ArcaEnvironment.PROD


def test_parse_environment_rechaza_valores_invalidos():
    with pytest.raises(ProfileError, match="Ambiente inválido"):
        parse_environment("staging")


def test_todos_los_paths_se_derivan_de_una_raiz(tmp_path):
    root = tmp_path / "homo-profile"
    paths = ProfilePaths(root=root)

    assert paths.data_dir == root / "data"
    assert paths.secrets_dir == root / "secrets"
    assert paths.backups_dir == root / "backups"
    assert paths.db == root / "data" / "facturador.db"
    assert paths.cert == root / "secrets" / "cert.crt"
    assert paths.key == root / "secrets" / "cert.key"
    assert paths.pdf_dir == root / "data" / "pdfs"
    assert paths.wsaa_ta_cache == root / "data" / "ta-wsfex.json"
    assert paths.arca_params_cache == paths.db
    assert paths.logs_dir == root / "data" / "logs"
    assert paths.log_file == root / "data" / "logs" / "facturador.log"
    assert paths.onboarding == root / "data" / "onboarding.json"
    assert paths.launcher_lock == root / "data" / "launcher.lock"


def test_ensure_layout_crea_la_estructura_minima(tmp_path):
    paths = ProfilePaths(root=tmp_path / "perfil")
    paths.ensure_layout()

    assert paths.secrets_dir.is_dir()
    assert paths.data_dir.is_dir()
    assert paths.backups_dir.is_dir()
    # pdfs/ y logs/ los crea quien escribe en ellos.
    assert not paths.pdf_dir.exists()
    assert not paths.logs_dir.exists()


def test_for_testing_construye_perfil_aislado(tmp_path):
    root = tmp_path / "custom-homo"
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, root)

    assert profile.environment is ArcaEnvironment.HOMO
    assert profile.paths.root == root.resolve()
    assert profile.display_name == "Homologación"


def test_resolve_usa_subdirectorios_por_ambiente_bajo_app_data(tmp_path):
    app_data = tmp_path / "FacturadorE"
    homo = EnvironmentProfile.resolve(
        ArcaEnvironment.HOMO, app_data_root=app_data
    )
    prod = EnvironmentProfile.resolve(
        ArcaEnvironment.PROD, app_data_root=app_data
    )

    assert homo.paths.root == app_data / "homo"
    assert prod.paths.root == app_data / "prod"
    assert homo.paths.root != prod.paths.root


def test_resolve_isolated_profiles_valida_aislamiento(tmp_path):
    app_data = tmp_path / "FacturadorE"
    homo, prod = resolve_isolated_profiles(app_data_root=app_data)

    assert homo.environment is ArcaEnvironment.HOMO
    assert prod.environment is ArcaEnvironment.PROD
    ensure_profile_roots_differ(homo, prod)


def test_raices_iguales_rechazadas(tmp_path):
    shared = tmp_path / "shared"
    homo = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, shared)
    prod = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, shared)

    with pytest.raises(ProfileError, match="no pueden compartir"):
        ensure_profile_roots_differ(homo, prod)


def test_repr_no_expone_la_raiz_fisica(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "h")

    text = repr(profile)
    assert "EnvironmentProfile" in text
    assert "homo" in text
    assert str(profile.paths.root) not in text
    assert "root" not in text


def test_profile_paths_no_expone_root_en_repr(tmp_path):
    paths = ProfilePaths(root=tmp_path / "secret-root")

    assert "secret-root" not in repr(paths)
    assert "ProfilePaths" in repr(paths)


@pytest.mark.parametrize(
    ("platform", "env", "home", "expected_suffix"),
    [
        (
            "win32",
            {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"},
            None,
            r"C:\Users\me\AppData\Local\FacturadorE",
        ),
        (
            "win32",
            {"APPDATA": r"C:\Users\me\AppData\Roaming"},
            None,
            r"C:\Users\me\AppData\Roaming\FacturadorE",
        ),
        (
            "darwin",
            {},
            "/Users/me",
            "/Users/me/Library/Application Support/FacturadorE",
        ),
        (
            "linux",
            {"XDG_DATA_HOME": "/custom/data"},
            None,
            "/custom/data/facturadorE",
        ),
        (
            "linux",
            {},
            "/Users/me",
            "/Users/me/.local/share/facturadorE",
        ),
    ],
)
def test_resolve_app_data_root_por_plataforma(
    monkeypatch, platform, env, home, expected_suffix
):
    monkeypatch.setattr("facturador.profile.sys.platform", platform)
    if platform == "win32":
        # Solo deben regir las variables de la fila (p.ej. fallback a APPDATA
        # exige LOCALAPPDATA ausente), aun corriendo en un host Windows real.
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if home is not None:
        monkeypatch.setattr(
            "facturador.profile.Path.home",
            classmethod(lambda cls, h=home: Path(h)),
        )
    if platform == "linux" and "XDG_DATA_HOME" not in env:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    root = resolve_app_data_root()
    if platform == "win32":
        # Simulamos win32 en Linux CI: Path usa '/' al unir, PureWindowsPath
        # normaliza la comparación.
        assert PureWindowsPath(str(root).replace("/", "\\")) == PureWindowsPath(
            expected_suffix
        )
    else:
        assert root == Path(expected_suffix)


def test_resolve_app_data_root_falla_sin_localappdata_en_windows(monkeypatch):
    monkeypatch.setattr("facturador.profile.sys.platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    with pytest.raises(ProfileError, match="app-data"):
        resolve_app_data_root()


def test_xdg_data_home_relativo_rechazado(monkeypatch):
    monkeypatch.setattr("facturador.profile.sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", ".local/share")

    with pytest.raises(ProfileError, match="XDG_DATA_HOME"):
        resolve_app_data_root()


def test_resolve_profile_root_deriva_desde_app_data(tmp_path):
    app_data = tmp_path / "app"
    assert resolve_profile_root(ArcaEnvironment.PROD, app_data_root=app_data) == (
        app_data / "prod"
    )

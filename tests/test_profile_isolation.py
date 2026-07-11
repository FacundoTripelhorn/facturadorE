"""Aislamiento entre perfiles Homologación / Producción (FAC-33 / ADR 0001).

Suite de integración: dos perfiles temporales bajo un app-data aislado,
apps reales con ARCA simulado, y asserts de que datos y artefactos de
runtime no cruzan. Sin comportamiento de producto nuevo.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db, repo
from facturador.api import create_app
from facturador.arca.wsaa import Ticket, TicketCache
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config
from facturador.constants import ArcaEnvironment
from facturador.launcher import (
    ENVIRONMENT_OPTIONS,
    choose_environment,
    plan_backend_launch,
    resolve_launch_profile,
)
from facturador.launcher.__main__ import SwitchTo, _handle_change_environment_request
from facturador.launcher.switch import (
    read_change_environment_request,
    write_change_environment_request,
)
from facturador.profile import (
    EnvironmentProfile,
    ProfileError,
    ensure_profile_roots_differ,
    resolve_isolated_profiles,
)
from tests.arca_fake import FakeArca, FakeWsaa
from tests.conftest import seed_params, seed_settings

CLIENTE_BASE = {
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "RUT 219999830019",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": True,
}

_PAGINAS = ("/", "/comprobantes", "/clientes", "/configuracion")


@dataclass
class ProfileWorld:
    """Un backend completo ligado a un único perfil temporal."""

    environment: ArcaEnvironment
    profile: EnvironmentProfile
    config: Config
    client: TestClient
    arca: FakeArca


def _build_world(
    environment: ArcaEnvironment,
    app_data_root: Path,
    cert_and_key: tuple[bytes, bytes],
) -> ProfileWorld:
    """App + DB + fake ARCA bajo el perfil del ambiente (como el launcher)."""
    profile = resolve_launch_profile(environment, app_data_root=app_data_root)
    cert_pem, key_pem = cert_and_key
    profile.paths.ensure_layout()
    profile.paths.cert.write_bytes(cert_pem)
    profile.paths.key.write_bytes(key_pem)
    profile.paths.key.chmod(0o400)

    config = Config(env=environment, paths=profile.paths)
    conn = db.connect(profile.paths.db)
    seed_params(conn)
    seed_settings(conn, ambiente=environment.value)
    arca = FakeArca()
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(profile, config=config, conn=conn, wsfex=wsfex)
    client = TestClient(app)
    client.conn = conn
    return ProfileWorld(
        environment=environment,
        profile=profile,
        config=config,
        client=client,
        arca=arca,
    )


@pytest.fixture
def isolated_pair(tmp_path, test_cert_and_key):
    """Par homo/prod con raíces bajo un app-data temporal compartido."""
    app_data = tmp_path / "FacturadorE"
    homo = _build_world(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    prod = _build_world(ArcaEnvironment.PROD, app_data, test_cert_and_key)
    ensure_profile_roots_differ(homo.profile, prod.profile)
    return app_data, homo, prod


def _crear_cliente(world: ProfileWorld, razon: str) -> dict:
    r = world.client.post(
        "/clients", json={**CLIENTE_BASE, "razon_social": razon}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _autorizar_factura(world: ProfileWorld, monto: str = "1500.00") -> dict:
    draft = world.client.post("/invoices", json={"imp_total": monto})
    assert draft.status_code == 201, draft.text
    r = world.client.post(
        f"/invoices/{draft.json()['id']}/authorize?force_desync=true"
    )
    assert r.status_code == 200, r.text
    return r.json()


def _marker_ticket(environment: ArcaEnvironment, token: str) -> Ticket:
    now = dt.datetime.now(dt.UTC)
    return Ticket(
        token=token,
        sign=f"sig-{environment.value}",
        generation=now,
        expiration=now + dt.timedelta(hours=12),
        service="wsfex",
        environment=environment.value,
    )


def test_datos_creados_en_un_ambiente_ausentes_en_el_otro(isolated_pair):
    """Acceptance: data created in homo is absent in prod and vice versa."""
    _, homo, prod = isolated_pair

    cliente_homo = _crear_cliente(homo, "CLIENTE SOLO HOMO S.A.")
    factura_homo = _autorizar_factura(homo, "111.00")
    with homo.client.conn:
        homo.client.conn.execute(
            "UPDATE emisores SET razon_social = ? WHERE ambiente = ?",
            ("EMISOR HOMO S.R.L.", "homo"),
        )

    cliente_prod = _crear_cliente(prod, "CLIENTE SOLO PROD S.A.")
    factura_prod = _autorizar_factura(prod, "222.00")
    with prod.client.conn:
        prod.client.conn.execute(
            "UPDATE emisores SET razon_social = ? WHERE ambiente = ?",
            ("EMISOR PROD S.A.", "prod"),
        )

    nombres_homo = {c["razon_social"] for c in homo.client.get("/clients").json()}
    nombres_prod = {c["razon_social"] for c in prod.client.get("/clients").json()}
    assert "CLIENTE SOLO HOMO S.A." in nombres_homo
    assert "CLIENTE SOLO PROD S.A." not in nombres_homo
    assert "CLIENTE SOLO PROD S.A." in nombres_prod
    assert "CLIENTE SOLO HOMO S.A." not in nombres_prod
    assert cliente_homo["id"] not in {
        c["id"] for c in prod.client.get("/clients").json()
    }
    assert cliente_prod["id"] not in {
        c["id"] for c in homo.client.get("/clients").json()
    }

    ids_homo = {f["id"] for f in homo.client.get("/invoices").json()}
    ids_prod = {f["id"] for f in prod.client.get("/invoices").json()}
    assert factura_homo["id"] in ids_homo
    assert factura_homo["id"] not in ids_prod
    assert factura_prod["id"] in ids_prod
    assert factura_prod["id"] not in ids_homo

    assert homo.client.get(f"/invoices/{factura_prod['id']}").status_code == 404
    assert prod.client.get(f"/invoices/{factura_homo['id']}").status_code == 404

    emisores_homo = repo.list_emisores(homo.client.conn)
    emisores_prod = repo.list_emisores(prod.client.conn)
    assert any(e["razon_social"] == "EMISOR HOMO S.R.L." for e in emisores_homo)
    assert not any(e["razon_social"] == "EMISOR HOMO S.R.L." for e in emisores_prod)
    assert any(e["razon_social"] == "EMISOR PROD S.A." for e in emisores_prod)
    assert not any(e["razon_social"] == "EMISOR PROD S.A." for e in emisores_homo)


def test_artefactos_de_runtime_solo_bajo_el_perfil_seleccionado(isolated_pair):
    """Acceptance: each runtime artifact is written under the selected profile only."""
    _, homo, prod = isolated_pair
    _crear_cliente(homo, "CLIENTE HOMO PDF S.A.")
    _crear_cliente(prod, "CLIENTE PROD PDF S.A.")
    factura_homo = _autorizar_factura(homo)
    factura_prod = _autorizar_factura(prod)

    pdf_homo = homo.client.get(f"/invoices/{factura_homo['id']}/pdf")
    pdf_prod = prod.client.get(f"/invoices/{factura_prod['id']}/pdf")
    assert pdf_homo.status_code == 200, pdf_homo.text
    assert pdf_prod.status_code == 200, pdf_prod.text

    TicketCache(homo.profile.paths.wsaa_ta_cache).save(
        _marker_ticket(ArcaEnvironment.HOMO, "ta-homo-only")
    )
    TicketCache(prod.profile.paths.wsaa_ta_cache).save(
        _marker_ticket(ArcaEnvironment.PROD, "ta-prod-only")
    )

    repo.replace_params(
        homo.client.conn,
        "moneda",
        [{"code": "HOM", "description": "Moneda solo homologacion"}],
    )
    repo.replace_params(
        prod.client.conn,
        "moneda",
        [{"code": "PRD", "description": "Moneda solo produccion"}],
    )

    homo.profile.paths.onboarding.write_text(
        '{"setup":"homo-only"}', encoding="utf-8"
    )
    prod.profile.paths.onboarding.write_text(
        '{"setup":"prod-only"}', encoding="utf-8"
    )

    homo.profile.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    prod.profile.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    homo.profile.paths.log_file.write_text("log-marker-homo\n", encoding="utf-8")
    prod.profile.paths.log_file.write_text("log-marker-prod\n", encoding="utf-8")

    staging_homo = homo.profile.paths.backups_dir / "staging-homo.tar.gz.age"
    staging_prod = prod.profile.paths.backups_dir / "staging-prod.tar.gz.age"
    staging_homo.write_bytes(b"backup-staging-homo")
    staging_prod.write_bytes(b"backup-staging-prod")

    for world in (homo, prod):
        root = world.profile.paths.root.resolve()
        paths = world.profile.paths
        for path in (
            paths.db,
            paths.cert,
            paths.key,
            paths.pdf_dir,
            paths.wsaa_ta_cache,
            paths.arca_params_cache,
            paths.onboarding,
            paths.log_file,
            paths.backups_dir,
        ):
            assert path.resolve().is_relative_to(root), path

    assert homo.profile.paths.db.resolve() != prod.profile.paths.db.resolve()
    assert homo.profile.paths.cert.resolve() != prod.profile.paths.cert.resolve()

    pdfs_homo = {p.name for p in homo.profile.paths.pdf_dir.glob("*.pdf")}
    pdfs_prod = {p.name for p in prod.profile.paths.pdf_dir.glob("*.pdf")}
    assert pdfs_homo
    assert pdfs_prod
    # Nombres incluyen el ambiente; nada del otro perfil en el directorio.
    assert all("-homo.pdf" in name for name in pdfs_homo)
    assert all("-prod.pdf" in name for name in pdfs_prod)
    assert pdfs_homo.isdisjoint(pdfs_prod)
    for name in pdfs_homo:
        assert not (prod.profile.paths.pdf_dir / name).exists()
    for name in pdfs_prod:
        assert not (homo.profile.paths.pdf_dir / name).exists()

    ta_homo = TicketCache(homo.profile.paths.wsaa_ta_cache).load("homo")
    ta_prod = TicketCache(prod.profile.paths.wsaa_ta_cache).load("prod")
    assert ta_homo is not None and ta_homo.token == "ta-homo-only"
    assert ta_prod is not None and ta_prod.token == "ta-prod-only"
    assert TicketCache(homo.profile.paths.wsaa_ta_cache).load("prod") is None
    assert TicketCache(prod.profile.paths.wsaa_ta_cache).load("homo") is None
    assert not (prod.profile.paths.root / "data" / "ta-wsfex.json").samefile(
        homo.profile.paths.wsaa_ta_cache
    )

    monedas_homo = {m["code"] for m in homo.client.get("/params/moneda").json()}
    monedas_prod = {m["code"] for m in prod.client.get("/params/moneda").json()}
    assert monedas_homo == {"HOM"}
    assert monedas_prod == {"PRD"}

    assert "homo-only" in homo.profile.paths.onboarding.read_text(encoding="utf-8")
    assert "prod-only" in prod.profile.paths.onboarding.read_text(encoding="utf-8")
    assert "homo-only" not in prod.profile.paths.onboarding.read_text(encoding="utf-8")
    assert "log-marker-homo" in homo.profile.paths.log_file.read_text(encoding="utf-8")
    assert "log-marker-prod" in prod.profile.paths.log_file.read_text(encoding="utf-8")
    assert not (prod.profile.paths.logs_dir / "facturador.log").samefile(
        homo.profile.paths.log_file
    )
    assert staging_homo.is_file()
    assert staging_prod.is_file()
    assert not (prod.profile.paths.backups_dir / staging_homo.name).exists()
    assert not (homo.profile.paths.backups_dir / staging_prod.name).exists()


def test_cambio_de_ambiente_no_reusa_db_ni_cliente_arca(isolated_pair):
    """Acceptance: switching never reuses the previous DB or ARCA client."""
    app_data, homo, prod = isolated_pair
    _crear_cliente(homo, "CLIENTE ANTES DEL SWITCH S.A.")
    factura = _autorizar_factura(homo)

    before_conn = id(homo.client.app.state.service.conn)
    before_wsfex = id(homo.client.app.state.service.wsfex)
    before_db = homo.client.app.state.service.config.paths.db.resolve()
    before_env = homo.client.app.state.profile.environment

    write_change_environment_request(homo.profile.paths, ArcaEnvironment.HOMO)
    assert read_change_environment_request(homo.profile.paths) is not None

    stopped: list[str] = []

    class _Supervisor:
        plan = type(
            "P",
            (),
            {
                "profile": homo.profile,
                "display_name": homo.profile.display_name,
            },
        )()
        open_browser = False
        base_url = "http://127.0.0.1:8399"

        def poll_change_environment_request(self):
            return read_change_environment_request(homo.profile.paths)

        def clear_change_environment_request(self):
            from facturador.launcher.switch import clear_change_environment_request

            clear_change_environment_request(homo.profile.paths)

        def stop(self):
            stopped.append("stop")

    switch = _handle_change_environment_request(
        _Supervisor(),  # type: ignore[arg-type]
        current=ArcaEnvironment.HOMO,
        choose=lambda: ArcaEnvironment.PROD,
    )
    assert isinstance(switch, SwitchTo)
    assert switch.environment is ArcaEnvironment.PROD
    assert stopped == ["stop"]
    assert read_change_environment_request(homo.profile.paths) is None

    # Tras el reinicio el launcher arranca el perfil destino (nuevo proceso).
    plan = plan_backend_launch(
        ArcaEnvironment.PROD, app_data_root=app_data, port=8411
    )
    assert plan.profile.paths.root == prod.profile.paths.root
    assert plan.env["ARCA_ENV"] == "prod"
    assert plan.env["FACTURADOR_APP_DATA"] == str(app_data)

    # El backend de prod es otra app: otra DB y otro cliente ARCA.
    assert id(prod.client.app.state.service.conn) != before_conn
    assert id(prod.client.app.state.service.wsfex) != before_wsfex
    assert prod.client.app.state.service.config.paths.db.resolve() != before_db
    assert prod.client.app.state.profile.environment is not before_env
    assert prod.client.app.state.profile.environment is ArcaEnvironment.PROD
    assert prod.client.get(f"/invoices/{factura['id']}").status_code == 404
    assert all(
        c["razon_social"] != "CLIENTE ANTES DEL SWITCH S.A."
        for c in prod.client.get("/clients").json()
    )

    # Pedido de cambio no hace hot-switch: homo sigue en homo.
    assert homo.client.app.state.profile.environment is ArcaEnvironment.HOMO
    assert id(homo.client.app.state.service.wsfex) == before_wsfex
    assert id(homo.client.app.state.service.conn) == before_conn


def test_respuestas_http_no_exponen_rutas_fisicas(isolated_pair):
    """Acceptance: normal HTTP responses do not expose physical profile paths."""
    _, homo, prod = isolated_pair
    _crear_cliente(homo, "CLIENTE HOMO HTTP S.A.")
    _crear_cliente(prod, "CLIENTE PROD HTTP S.A.")
    factura_homo = _autorizar_factura(homo)
    factura_prod = _autorizar_factura(prod)

    for world, factura in ((homo, factura_homo), (prod, factura_prod)):
        root = str(world.profile.paths.root)
        secret_paths = (
            root,
            str(world.profile.paths.db),
            str(world.profile.paths.cert),
            str(world.profile.paths.key),
            str(world.profile.paths.pdf_dir),
            "cert.crt",
            "cert.key",
        )
        endpoints = [
            *[(p, world.client.get(p)) for p in _PAGINAS],
            ("/health", world.client.get("/health")),
            ("/health/arca", world.client.get("/health/arca")),
            ("/clients", world.client.get("/clients")),
            ("/invoices", world.client.get("/invoices")),
            (
                f"/invoices/{factura['id']}",
                world.client.get(f"/invoices/{factura['id']}"),
            ),
            ("/params/moneda", world.client.get("/params/moneda")),
        ]
        for path, response in endpoints:
            assert response.status_code == 200, path
            body = response.text
            for secret in secret_paths:
                assert secret not in body, (path, secret)

        health = world.client.get("/health").json()
        assert health == {
            "status": "ok",
            "environment": world.environment.value,
        }
        assert "path" not in health
        assert "root" not in health


def test_raices_compartidas_fallan_fuerte(tmp_path):
    """Acceptance: pointing both environments at one root fails loudly."""
    shared = tmp_path / "shared-root"
    shared.mkdir()
    homo = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, shared)
    prod = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, shared)

    with pytest.raises(ProfileError, match="no pueden compartir"):
        ensure_profile_roots_differ(homo, prod)

    # Symlink/junction al mismo directorio físico también debe fallar.
    app_data = tmp_path / "appdata"
    real = app_data / "real"
    real.mkdir(parents=True)
    linked = app_data / "linked"
    linked.symlink_to(real, target_is_directory=True)
    homo_link = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, real)
    prod_link = EnvironmentProfile.for_testing(ArcaEnvironment.PROD, linked)
    with pytest.raises(ProfileError, match="no pueden compartir"):
        ensure_profile_roots_differ(homo_link, prod_link)

    # El camino feliz del launcher sigue validando aislamiento.
    ok_homo, ok_prod = resolve_isolated_profiles(app_data_root=tmp_path / "ok")
    ensure_profile_roots_differ(ok_homo, ok_prod)
    assert ok_homo.paths.root != ok_prod.paths.root


def test_launcher_seleccion_contra_perfiles_temporales(tmp_path, test_cert_and_key):
    """Launcher selection + restart switch against temporary isolated profiles."""
    app_data = tmp_path / "FacturadorE"
    picks = iter([ArcaEnvironment.HOMO])

    chosen = choose_environment(
        force_tty=True,
        prompt_tty=lambda _opts: next(picks),
        prompt_gui=lambda _opts: (_ for _ in ()).throw(
            AssertionError("GUI no debe abrirse")
        ),
    )
    assert chosen is ArcaEnvironment.HOMO
    assert {opt.environment for opt in ENVIRONMENT_OPTIONS} == {
        ArcaEnvironment.HOMO,
        ArcaEnvironment.PROD,
    }

    plan_homo = plan_backend_launch(
        chosen, app_data_root=app_data, port=8420, home=tmp_path / "home"
    )
    plan_prod = plan_backend_launch(
        ArcaEnvironment.PROD,
        app_data_root=app_data,
        port=8421,
        home=tmp_path / "home",
    )
    ensure_profile_roots_differ(plan_homo.profile, plan_prod.profile)
    assert plan_homo.profile.paths.root == app_data / "homo"
    assert plan_prod.profile.paths.root == app_data / "prod"
    assert plan_homo.env["ARCA_ENV"] == "homo"
    assert plan_prod.env["ARCA_ENV"] == "prod"
    assert plan_homo.env["FACTURADOR_APP_DATA"] == str(app_data)
    assert str(plan_homo.profile.paths.root) not in repr(plan_homo.profile)

    homo = _build_world(ArcaEnvironment.HOMO, app_data, test_cert_and_key)
    prod = _build_world(ArcaEnvironment.PROD, app_data, test_cert_and_key)
    _crear_cliente(homo, "DESDE LAUNCHER HOMO S.A.")
    assert all(
        c["razon_social"] != "DESDE LAUNCHER HOMO S.A."
        for c in prod.client.get("/clients").json()
    )
    assert homo.client.get("/health").json()["environment"] == "homo"
    assert prod.client.get("/health").json()["environment"] == "prod"

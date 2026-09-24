"""Política Host/Origin localhost (DNS rebinding / cross-origin)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db
from facturador.api import create_app
from facturador.api.localhost_policy import (
    allowed_hosts,
    allowed_origins,
    check_host_header,
    check_origin_headers,
    loopback_base_url,
    origin_from_referer,
    resolve_listen_port,
)
from facturador.arca.wsfex import WsfexClient
from facturador.constants import DEFAULT_PORT
from tests.arca_fake import FakeWsaa
from tests.conftest import seed_params, seed_settings

_CLIENTE = {
    "razon_social": "LOOPBACK OK S.A.",
    "domicilio": "Calle 1",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "RUT 1",
    "moneda_default": "DOL",
    "idioma_default": 1,
    "forma_pago_default": "WIRE",
}


@pytest.mark.parametrize(
    "host",
    [
        f"127.0.0.1:{DEFAULT_PORT}",
        f"localhost:{DEFAULT_PORT}",
        f"[::1]:{DEFAULT_PORT}",
        f"127.0.0.1:{DEFAULT_PORT}".upper(),  # case-insensitive
    ],
)
def test_hosts_de_loopback_esperados_pasan(host):
    assert check_host_header(host, DEFAULT_PORT) is None


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:{port}",
        "127.0.0.1:{other}",
        "localhost",  # sin puerto
        "0.0.0.0:{port}",
        "192.168.1.1:{port}",
        "[::ffff:127.0.0.1]:{port}",
        "",
    ],
)
def test_hosts_inesperados_rechazados(host):
    filled = host.format(port=DEFAULT_PORT, other=DEFAULT_PORT + 1)
    assert check_host_header(filled or None, DEFAULT_PORT) is not None


def test_host_ausente_rechazado():
    assert check_host_header(None, DEFAULT_PORT) == "Host header requerido"


@pytest.mark.parametrize(
    "origin",
    [
        f"http://127.0.0.1:{DEFAULT_PORT}",
        f"http://localhost:{DEFAULT_PORT}",
        f"http://[::1]:{DEFAULT_PORT}",
    ],
)
def test_origins_de_loopback_esperados_pasan(origin):
    assert check_origin_headers("POST", origin, None, DEFAULT_PORT) is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:{port}",  # sin TLS en este producto
        "http://evil.example:{port}",
        "http://127.0.0.1:{other}",
        "null",
        "*",
    ],
)
def test_origins_inesperados_rechazados_en_post(origin):
    filled = origin.format(port=DEFAULT_PORT, other=DEFAULT_PORT + 1)
    err = check_origin_headers("POST", filled, None, DEFAULT_PORT)
    assert err == "Origin no permitido"


def test_get_no_valida_origin():
    assert (
        check_origin_headers(
            "GET", "http://evil.example", None, DEFAULT_PORT
        )
        is None
    )


def test_post_sin_origin_ni_referer_permitido():
    # Clientes no-browser (API, health checks); los forms los cubre CSRF.
    assert check_origin_headers("POST", None, None, DEFAULT_PORT) is None


def test_post_con_referer_loopback_pasa():
    referer = f"http://127.0.0.1:{DEFAULT_PORT}/ui/facturas"
    assert check_origin_headers("POST", None, referer, DEFAULT_PORT) is None


def test_post_con_referer_externo_rechazado():
    err = check_origin_headers(
        "POST", None, "http://evil.example/attack", DEFAULT_PORT
    )
    assert err == "Referer no permitido"


def test_origin_from_referer_extrae_origen():
    assert (
        origin_from_referer(f"http://localhost:{DEFAULT_PORT}/path?q=1")
        == f"http://localhost:{DEFAULT_PORT}"
    )


def test_allowed_sets_no_incluyen_wildcard():
    assert "*" not in allowed_hosts(DEFAULT_PORT)
    assert "*" not in allowed_origins(DEFAULT_PORT)
    assert all(not o.endswith("*") for o in allowed_origins(DEFAULT_PORT))


def test_puerto_del_launcher_cambia_la_allowlist():
    port = 8410
    assert f"127.0.0.1:{port}" in allowed_hosts(port)
    assert f"http://127.0.0.1:{port}" in allowed_origins(port)
    assert check_host_header(f"127.0.0.1:{DEFAULT_PORT}", port) is not None
    assert check_host_header(f"127.0.0.1:{port}", port) is None


def test_resolve_listen_port_desde_env(monkeypatch):
    monkeypatch.setenv("FACTURADOR_PORT", "8500")
    assert resolve_listen_port() == 8500
    assert resolve_listen_port(8411) == 8411  # explícito gana


def test_docker_public_port_en_allowlist_junto_al_listen(monkeypatch):
    """Compose: bind 8399 adentro, browser en FACTURADOR_PUBLIC_PORT del host."""
    from facturador.api.localhost_policy import resolve_policy_ports

    monkeypatch.delenv("FACTURADOR_PUBLIC_PORT", raising=False)
    assert resolve_policy_ports(DEFAULT_PORT) == frozenset({DEFAULT_PORT})

    monkeypatch.setenv("FACTURADOR_PUBLIC_PORT", "8400")
    ports = resolve_policy_ports(DEFAULT_PORT)
    assert ports == frozenset({DEFAULT_PORT, 8400})
    assert check_host_header("localhost:8400", ports) is None
    assert check_host_header(f"127.0.0.1:{DEFAULT_PORT}", ports) is None
    assert check_origin_headers(
        "POST", "http://localhost:8400", None, ports
    ) is None
    assert check_host_header("localhost:8411", ports) is not None


def test_middleware_acepta_host_del_publish_docker(
    test_profile, test_config, arca
):
    """Browser en puerto host distinto del bind interno (Docker mapping)."""
    listen = DEFAULT_PORT
    public = 8400
    conn = db.connect(test_profile.paths.db)
    seed_params(conn)
    seed_settings(conn)
    wsfex = WsfexClient(
        test_config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(
        test_profile,
        config=test_config,
        conn=conn,
        wsfex=wsfex,
        port=listen,
        public_port=public,
    )
    # TestClient base_url usa el puerto público (lo que ve el browser).
    client = TestClient(app, base_url=loopback_base_url(public))

    assert client.get("/health").status_code == 200
    # Healthcheck interno del contenedor sigue válido en el listen port.
    assert (
        client.get("/health", headers={"Host": f"localhost:{listen}"}).status_code
        == 200
    )
    r = client.post(
        "/clients",
        json={**_CLIENTE, "razon_social": "DOCKER PUBLIC", "id_impositivo": "RUT 3"},
        headers={"Origin": f"http://127.0.0.1:{public}"},
    )
    assert r.status_code == 201, r.text
    bad = client.get("/health", headers={"Host": "localhost:8411"})
    assert bad.status_code == 400


def test_middleware_rechaza_host_inesperado(api):
    # Host hostil con el mismo puerto que escucha la app.
    r = api.get("/health", headers={"Host": f"evil.example:{DEFAULT_PORT}"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Host no permitido"


def test_middleware_acepta_localhost_y_ipv6(api):
    for host in (
        f"localhost:{DEFAULT_PORT}",
        f"[::1]:{DEFAULT_PORT}",
    ):
        r = api.get("/health", headers={"Host": host})
        assert r.status_code == 200, host


def test_middleware_rechaza_origin_inesperado_en_post(api):
    r = api.post(
        "/clients",
        json={**_CLIENTE, "razon_social": "X"},
        headers={"Origin": "http://evil.example:8399"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Origin no permitido"


def test_middleware_acepta_origin_loopback_en_post(api):
    r = api.post(
        "/clients",
        json=_CLIENTE,
        headers={"Origin": f"http://127.0.0.1:{DEFAULT_PORT}"},
    )
    assert r.status_code == 201, r.text


def test_middleware_respeta_puerto_custom(test_profile, test_config, arca):
    """Launcher en puerto distinto: solo ese Host/Origin pasan."""
    port = 8410
    conn = db.connect(test_profile.paths.db)
    seed_params(conn)
    seed_settings(conn)
    wsfex = WsfexClient(
        test_config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(
        test_profile, config=test_config, conn=conn, wsfex=wsfex, port=port
    )
    client = TestClient(app, base_url=loopback_base_url(port))

    assert client.get("/health").status_code == 200
    bad = client.get(
        "/health", headers={"Host": f"127.0.0.1:{DEFAULT_PORT}"}
    )
    assert bad.status_code == 400
    good_origin = client.post(
        "/clients",
        json={**_CLIENTE, "razon_social": "PUERTO CUSTOM", "id_impositivo": "RUT 2"},
        headers={"Origin": f"http://127.0.0.1:{port}"},
    )
    assert good_origin.status_code == 201, good_origin.text

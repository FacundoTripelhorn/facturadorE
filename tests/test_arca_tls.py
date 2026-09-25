"""Prod-only WSFEX TLS verify (SECLEVEL=1, never verify=False)."""

from __future__ import annotations

import ssl

import httpx

from facturador.arca import wsfex as wsfex_mod
from facturador.arca.tls import wsfex_verify
from facturador.arca.wsfex import WsfexClient
from facturador.constants import ArcaEnvironment


class _FakeWsaa:
    def get_ticket(self):
        raise AssertionError("no se pide TA en este test")


def test_wsfex_verify_homo_keeps_httpx_default() -> None:
    assert wsfex_verify(ArcaEnvironment.HOMO) is True


def test_wsfex_verify_prod_lowers_seclevel_keeps_cert_verify() -> None:
    ctx = wsfex_verify(ArcaEnvironment.PROD)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.security_level == 1


def test_wsfex_verify_prod_preserves_default_cipher_policy() -> None:
    """SECLEVEL drops to 1 without switching the suite list to DEFAULT."""
    default_names = {c["name"] for c in ssl.create_default_context().get_ciphers()}
    ctx = wsfex_verify(ArcaEnvironment.PROD)
    assert isinstance(ctx, ssl.SSLContext)
    assert {c["name"] for c in ctx.get_ciphers()} == default_names

    broader = ssl.create_default_context()
    broader.set_ciphers("ALL:@SECLEVEL=1")
    assert len(broader.get_ciphers()) > len(ctx.get_ciphers())


def test_wsfex_client_default_http_uses_helper(test_config, monkeypatch) -> None:
    """Default construction calls wsfex_verify(env); injected http skips it."""
    calls: list[ArcaEnvironment] = []

    def fake_verify(env: ArcaEnvironment) -> bool:
        calls.append(env)
        return True

    monkeypatch.setattr(wsfex_mod, "wsfex_verify", fake_verify)

    WsfexClient(test_config, wsaa=_FakeWsaa())
    assert calls == [test_config.env]

    calls.clear()
    injected = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    WsfexClient(test_config, wsaa=_FakeWsaa(), http=injected)
    assert calls == []

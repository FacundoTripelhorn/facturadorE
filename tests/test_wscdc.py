"""Cliente WSCDC y constatación bajo demanda (fake in-process)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from facturador.arca.wsaa import SERVICE_WSCDC
from facturador.arca.wscdc import (
    WSCDC_NS,
    ConstatacionRequest,
    WscdcClient,
    WscdcError,
)
from facturador.constants import CBTE_MODO_CAE, CBTE_TIPO_FACTURA_E, TIPO_DOC_CUIT
from tests.arca_fake import FakeWsaa
from tests.conftest import TEST_CUIT


@pytest.fixture
def wscdc(test_config, arca):
    return WscdcClient(
        test_config,
        wsaa=FakeWsaa(service=SERVICE_WSCDC),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )


def _req(**overrides) -> ConstatacionRequest:
    base = dict(
        cuit_emisor=int(TEST_CUIT),
        punto_venta=1,
        cbte_tipo=CBTE_TIPO_FACTURA_E,
        cbte_nro=1,
        fecha_cbte="20260703",
        imp_total=Decimal("1500.00"),
        cae="76100000000001",
        doc_tipo_receptor=str(TIPO_DOC_CUIT),
        doc_nro_receptor="55000002002",
        cbte_modo=CBTE_MODO_CAE,
    )
    base.update(overrides)
    return ConstatacionRequest(**base)


def test_constatar_happy_path(wscdc, arca):
    result = wscdc.constatar(_req())
    assert result.ok
    assert result.resultado == "A"
    assert result.fch_proceso == "20260809120000"
    assert arca.calls["ComprobanteConstatar"] == 1
    assert arca.last_constatar_req is not None
    assert arca.last_constatar_req["CbteModo"] == "CAE"
    assert arca.last_constatar_req["CbteTipo"] == "19"
    assert arca.last_constatar_req["DocTipoReceptor"] == "80"
    assert arca.last_constatar_req["DocNroReceptor"] == "55000002002"
    assert arca.last_constatar_req["ImpTotal"] == "1500.00"
    assert arca.last_constatar_req["CodAutorizacion"] == "76100000000001"


def test_constatar_mismatch(wscdc, arca):
    arca.constatar_mode = "mismatch"
    result = wscdc.constatar(_req())
    assert not result.ok
    assert result.resultado == "R"
    assert result.observations
    assert result.observations[0][0] == "107"


def test_constatar_errors(wscdc, arca):
    arca.constatar_mode = "error"
    result = wscdc.constatar(_req())
    assert not result.ok
    assert result.errors
    assert result.errors[0] == ("100", "CAE inexistente")


def test_soap_uses_wscdc_namespace(wscdc, arca):
    wscdc.constatar(_req())
    # El fake ya parseó el body; re-armar un request y mirar el envelope.
    auth = wscdc._auth_element()
    cmp_req = wscdc._cmp_req_element(_req())
    assert auth.tag == f"{{{WSCDC_NS}}}Auth"
    assert cmp_req.tag == f"{{{WSCDC_NS}}}CmpReq"


def test_request_from_invoice_row_rejects_draft(test_config, arca):
    class Row(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    inv = Row(
        status="draft",
        cae=None,
        cbte_nro=None,
        cbte_tipo=19,
        punto_venta=1,
        fecha_cbte="20260703",
        imp_total="10.00",
        cuit_pais_cliente=55000002002,
    )
    with pytest.raises(WscdcError, match="autorizada"):
        WscdcClient.request_from_invoice_row(inv, cuit_emisor=int(TEST_CUIT))


def test_non_xml_http_error_becomes_wscdc_error(test_config):
    """502/503 HTML outage pages must not leak ET.ParseError as a 500."""

    def html_502(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text="<html><body>Bad Gateway</body></html>",
            headers={"Content-Type": "text/html"},
        )

    client = WscdcClient(
        test_config,
        wsaa=FakeWsaa(service=SERVICE_WSCDC),
        http=httpx.Client(transport=httpx.MockTransport(html_502)),
    )
    with pytest.raises(WscdcError, match="HTTP 502") as exc_info:
        client.constatar(_req())
    assert exc_info.value.code == "502"


def test_non_xml_200_becomes_wscdc_error(test_config):
    def plain_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not xml at all")

    client = WscdcClient(
        test_config,
        wsaa=FakeWsaa(service=SERVICE_WSCDC),
        http=httpx.Client(transport=httpx.MockTransport(plain_ok)),
    )
    with pytest.raises(WscdcError, match="no es XML"):
        client.constatar(_req())

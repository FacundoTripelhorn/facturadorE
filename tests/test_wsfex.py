"""Fase 2 — WSFEX: FEXDummy, parámetros, errores FEXErr y eventos."""

import datetime as dt
import xml.etree.ElementTree as ET

import httpx
import pytest

from facturador import db, repo
from facturador.arca.wsaa import Ticket
from facturador.arca.wsfex import (
    FEX_NS,
    ParamRecord,
    WsfexClient,
    WsfexError,
    parse_param_items,
)


def _fake_ticket() -> Ticket:
    return Ticket(
        token="tok==",
        sign="sig==",
        generation=dt.datetime.now(dt.UTC),
        expiration=dt.datetime.now(dt.UTC) + dt.timedelta(hours=12),
        service="wsfex",
        environment="homo",
    )


class _FakeWsaa:
    def get_ticket(self):
        return _fake_ticket()


def _soap(method: str, inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{method}Response xmlns="{FEX_NS}">'
        f"<{method}Result>{inner}</{method}Result>"
        f"</{method}Response></soap:Body></soap:Envelope>"
    )


DUMMY_OK = _soap(
    "FEXDummy",
    "<AppServer>OK</AppServer><DbServer>OK</DbServer><AuthServer>OK</AuthServer>",
)

MON_OK = _soap(
    "FEXGetPARAM_MON",
    "<FEXResultGet>"
    "<ClsFEXResponse_Mon><Mon_Id>DOL</Mon_Id><Mon_Ds>Dolar Estadounidense</Mon_Ds>"
    "<Mon_vig_desde>20090403</Mon_vig_desde><Mon_vig_hasta>NULL</Mon_vig_hasta>"
    "</ClsFEXResponse_Mon>"
    "<ClsFEXResponse_Mon><Mon_Id>PES</Mon_Id><Mon_Ds>Pesos Argentinos</Mon_Ds>"
    "<Mon_vig_desde>20090403</Mon_vig_desde><Mon_vig_hasta>NULL</Mon_vig_hasta>"
    "</ClsFEXResponse_Mon>"
    "</FEXResultGet>",
)

ERR_600 = _soap(
    "FEXGetPARAM_MON",
    "<FEXErr><ErrCode>600</ErrCode>"
    "<ErrMsg>No se corresponden token con firma</ErrMsg></FEXErr>",
)

CON_EVENTOS = _soap(
    "FEXGetPARAM_MON",
    "<FEXResultGet>"
    "<ClsFEXResponse_Mon><Mon_Id>DOL</Mon_Id><Mon_Ds>Dolar</Mon_Ds></ClsFEXResponse_Mon>"
    "</FEXResultGet>"
    "<FEXEvents><EventCode>99</EventCode>"
    "<EventMsg>Mantenimiento programado</EventMsg></FEXEvents>"
    "<FEXErr><ErrCode>0</ErrCode><ErrMsg>OK</ErrMsg></FEXErr>",
)


def _client(test_config, handler) -> WsfexClient:
    return WsfexClient(
        test_config,
        wsaa=_FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_dummy_parsea_estado_de_servidores(test_config):
    def handler(request):
        assert request.headers["SOAPAction"] == f'"{FEX_NS}FEXDummy"'
        return httpx.Response(200, text=DUMMY_OK)

    client = _client(test_config, handler)
    estado = client.dummy()
    assert estado == {"appserver": "OK", "dbserver": "OK", "authserver": "OK"}


def test_get_param_incluye_auth_y_parsea_registros(test_config):
    capturado = {}

    def handler(request):
        capturado["body"] = request.content
        return httpx.Response(200, text=MON_OK)

    client = _client(test_config, handler)
    monedas = client.get_param("moneda")

    assert monedas == [
        ParamRecord("DOL", "Dolar Estadounidense", "20090403", "NULL"),
        ParamRecord("PES", "Pesos Argentinos", "20090403", "NULL"),
    ]
    # El request lleva Auth{Token,Sign,Cuit} armado por serializador.
    root = ET.fromstring(capturado["body"])
    auth = root.find(f".//{{{FEX_NS}}}Auth")
    assert auth.findtext(f"{{{FEX_NS}}}Token") == "tok=="
    assert auth.findtext(f"{{{FEX_NS}}}Cuit") == "20111111112"


def test_errcode_distinto_de_cero_levanta_error(test_config):
    client = _client(test_config, lambda r: httpx.Response(200, text=ERR_600))
    with pytest.raises(WsfexError) as exc:
        client.get_param("moneda")
    assert exc.value.code == "600"


def test_eventos_se_loguean_como_warning_y_no_cortan(test_config, caplog):
    client = _client(test_config, lambda r: httpx.Response(200, text=CON_EVENTOS))
    with caplog.at_level("WARNING"):
        monedas = client.get_param("moneda")
    assert len(monedas) == 1  # el evento no interrumpe
    assert client.last_events == [("99", "Mantenimiento programado")]
    assert "Mantenimiento programado" in caplog.text


def test_cuit_sale_del_certificado_si_no_hay_env(test_config):
    client = _client(test_config, lambda r: httpx.Response(200, text=DUMMY_OK))
    assert client.cuit == 20111111112


def test_parse_param_items_generico_para_paises():
    result = ET.fromstring(
        f'<FEXGetPARAM_DST_paisResult xmlns="{FEX_NS}"><FEXResultGet>'
        "<ClsFEXResponse_DST_pais><DST_Codigo>225</DST_Codigo><DST_Ds>URUGUAY</DST_Ds></ClsFEXResponse_DST_pais>"
        "</FEXResultGet></FEXGetPARAM_DST_paisResult>"
    )
    assert parse_param_items(result) == [ParamRecord("225", "URUGUAY", None, None)]


def test_cache_de_params_en_sqlite(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    n = repo.replace_params(
        conn, "moneda", [ParamRecord("DOL", "Dolar", None, None).as_dict()]
    )
    assert n == 1
    filas = repo.get_params(conn, "moneda")
    assert filas[0]["code"] == "DOL"
    assert filas[0]["fetched_at"]  # timestamp presente para el TTL de 24 h

    # refresh reemplaza, no duplica
    repo.replace_params(
        conn, "moneda", [ParamRecord("EUR", "Euro", None, None).as_dict()]
    )
    filas = repo.get_params(conn, "moneda")
    assert [f["code"] for f in filas] == ["EUR"]


@pytest.mark.parametrize(
    ("status", "cuerpo", "error"),
    [
        # HTML real de un proxy: no es XML bien formado.
        (502, "<!DOCTYPE html><html><body>Bad Gateway<br></body></html>",
         httpx.HTTPStatusError),
        # HTML que sí parsea como XML, pero sin SOAP Fault.
        (502, "<html><body>Bad Gateway</body></html>", httpx.HTTPStatusError),
        (503, "", httpx.HTTPStatusError),
        (200, "<!DOCTYPE html><p>mantenimiento", httpx.DecodingError),
        (200, "", httpx.DecodingError),
    ],
)
def test_respuesta_que_no_es_xml_es_error_de_transporte(
    test_config, status, cuerpo, error
):
    """Una página HTML de un proxy o un cuerpo vacío no dicen nada sobre lo
    que hizo ARCA: salen como error de transporte (como un timeout), nunca
    como ParseError ni como rechazo de ARCA."""
    client = _client(test_config, lambda request: httpx.Response(status, text=cuerpo))
    with pytest.raises(error):
        client.dummy()

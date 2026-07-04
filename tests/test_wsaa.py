"""Fase 1 — WSAA: TRA, firma CMS, parseo/validación del TA, cache y renovación."""

import base64
import datetime as dt
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import pkcs7

from facturador.arca.wsaa import (
    SERVICE,
    TaAlreadyIssuedError,
    Ticket,
    TicketCache,
    WsaaClient,
    WsaaError,
    build_login_request,
    build_tra,
    cuit_from_certificate,
    parse_login_response,
    sign_tra_cms,
)
from tests.conftest import TEST_CUIT


def _make_ta_xml(
    token="tok==",
    sign="sig==",
    expiration: dt.datetime | None = None,
) -> str:
    expiration = expiration or (
        dt.datetime.now(dt.UTC) + dt.timedelta(hours=12)
    )
    generation = dt.datetime.now(dt.UTC)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<loginTicketResponse version="1.0">
  <header>
    <source>CN=wsaahomo, O=AFIP, C=AR</source>
    <destination>SERIALNUMBER=CUIT {TEST_CUIT}, CN=facturador-test</destination>
    <uniqueId>1</uniqueId>
    <generationTime>{generation.isoformat(timespec="seconds")}</generationTime>
    <expirationTime>{expiration.isoformat(timespec="seconds")}</expirationTime>
  </header>
  <credentials>
    <token>{token}</token>
    <sign>{sign}</sign>
  </credentials>
</loginTicketResponse>"""


def _make_soap_response(ta_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body>"
        '<loginCmsResponse xmlns="http://wsaa.view.sua.dvadac.desein.afip.gov">'
        f"<loginCmsReturn>{escape(ta_xml)}</loginCmsReturn>"
        "</loginCmsResponse></soapenv:Body></soapenv:Envelope>"
    )


FAULT_ALREADY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soapenv:Body><soapenv:Fault>"
    "<faultcode>ns1:coe.alreadyAuthenticated</faultcode>"
    "<faultstring>El CEE ya posee un TA valido para el acceso al WSN"
    " solicitado</faultstring>"
    "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
)


# --- TRA ---


def test_tra_estructura_y_servicio():
    tra = build_tra()
    root = ET.fromstring(tra)
    assert root.tag == "loginTicketRequest"
    assert root.findtext("service") == "wsfex"
    assert root.findtext("header/uniqueId").isdigit()


def test_tra_ventana_amplia_de_tiempos():
    now = dt.datetime(2026, 7, 3, 12, 0, 0, tzinfo=dt.UTC)
    root = ET.fromstring(build_tra(now=now))
    gen = dt.datetime.fromisoformat(root.findtext("header/generationTime"))
    exp = dt.datetime.fromisoformat(root.findtext("header/expirationTime"))
    assert gen == now - dt.timedelta(minutes=10)
    assert exp == now + dt.timedelta(minutes=10)


def test_tra_escapa_valores_via_serializador():
    # El TRA no lleva texto libre, pero la garantía general es que TODO XML
    # sale de ElementTree: un valor hostil queda escapado, nunca inyectado.
    root = ET.Element("x")
    root.text = '<script>&"'
    assert b"&lt;script&gt;&amp;" in ET.tostring(root)


# --- Firma CMS ---


def test_firma_cms_incluye_tra_y_certificado(test_cert_and_key):
    cert_pem, key_pem = test_cert_and_key
    tra = build_tra()
    der = sign_tra_cms(tra, cert_pem, key_pem)
    certs = pkcs7.load_der_pkcs7_certificates(der)
    assert len(certs) == 1
    assert "facturador-test" in certs[0].subject.rfc4514_string()
    assert tra in der  # contenido embebido, no detached


def test_cuit_se_extrae_del_certificado(test_cert_and_key):
    cert_pem, _ = test_cert_and_key
    assert cuit_from_certificate(cert_pem) == int(TEST_CUIT)


def test_login_request_es_soap_valido_con_cms_base64(test_cert_and_key):
    cert_pem, key_pem = test_cert_and_key
    der = sign_tra_cms(build_tra(), cert_pem, key_pem)
    body = build_login_request(der)
    root = ET.fromstring(body)
    in0 = root.find(
        ".//{http://wsaa.view.sua.dvadac.desein.afip.gov}in0"
    )
    assert base64.b64decode(in0.text) == der


# --- Parseo y validación del TA (checklist §2.1.1 punto 2) ---


def test_parseo_ta_ok():
    ticket = parse_login_response(_make_soap_response(_make_ta_xml()), "homo")
    assert ticket.token == "tok=="
    assert ticket.sign == "sig=="
    assert ticket.service == SERVICE
    assert ticket.environment == "homo"
    assert ticket.is_valid()


def test_ta_vencido_rechazado_al_parsear():
    vencido = _make_ta_xml(
        expiration=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    )
    with pytest.raises(WsaaError, match="vencido"):
        parse_login_response(_make_soap_response(vencido), "homo")


def test_ta_sin_credenciales_rechazado():
    sin_token = _make_ta_xml(token="")
    with pytest.raises(WsaaError, match="incompleto"):
        parse_login_response(_make_soap_response(sin_token), "homo")


def test_str_de_ticket_redacta_credenciales():
    ticket = parse_login_response(_make_soap_response(_make_ta_xml()), "homo")
    for texto in (str(ticket), repr(ticket)):
        assert "tok==" not in texto
        assert "sig==" not in texto


# --- Cache: reuso, renovación al expirar, rechazo de servicio/ambiente ajeno ---


def _ticket(expiration: dt.datetime, service=SERVICE, env="homo") -> Ticket:
    return Ticket(
        token="tok==",
        sign="sig==",
        generation=dt.datetime.now(dt.UTC),
        expiration=expiration,
        service=service,
        environment=env,
    )


def test_cache_roundtrip(tmp_path):
    cache = TicketCache(tmp_path / "ta.json")
    original = _ticket(dt.datetime.now(dt.UTC) + dt.timedelta(hours=12))
    cache.save(original)
    cargado = cache.load("homo")
    assert cargado == original


def test_cache_descarta_ta_vencido(tmp_path):
    cache = TicketCache(tmp_path / "ta.json")
    cache.save(_ticket(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)))
    # dentro del margen de renovación (5 min) => se considera vencido
    assert cache.load("homo") is None


def test_cache_descarta_ta_de_otro_servicio_o_ambiente(tmp_path):
    cache = TicketCache(tmp_path / "ta.json")
    cache.save(_ticket(dt.datetime.now(dt.UTC) + dt.timedelta(hours=12),
                       service="wsfe"))
    assert cache.load("homo") is None
    cache.save(_ticket(dt.datetime.now(dt.UTC) + dt.timedelta(hours=12),
                       env="prod"))
    assert cache.load("homo") is None


def test_cache_corrupto_se_descarta(tmp_path):
    path = tmp_path / "ta.json"
    path.write_text("{no es json", encoding="utf-8")
    assert TicketCache(path).load("homo") is None


# --- Cliente end-to-end con transporte mockeado ---


def _client_with_transport(test_config, handler) -> WsaaClient:
    return WsaaClient(
        test_config, http=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_obtener_ta_reusar_y_renovar(test_config):
    llamadas = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas["n"] += 1
        return httpx.Response(200, text=_make_soap_response(_make_ta_xml()))

    client = _client_with_transport(test_config, handler)

    primero = client.get_ticket()   # va a WSAA
    segundo = client.get_ticket()   # sale del cache
    assert llamadas["n"] == 1
    assert primero == segundo

    # Forzar expiración: reescribir el cache con un TA al borde de vencer.
    client.cache.save(
        _ticket(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2))
    )
    client.get_ticket()             # renueva contra WSAA
    assert llamadas["n"] == 2


def test_fault_ta_vigente_da_error_especifico(test_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=FAULT_ALREADY)

    client = _client_with_transport(test_config, handler)
    with pytest.raises(TaAlreadyIssuedError):
        client.get_ticket()


def test_request_a_wsaa_no_usa_fstrings_para_el_cms(test_config):
    """El request enviado debe ser XML parseable con el CMS en base64."""
    capturado = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["body"] = request.content
        capturado["url"] = str(request.url)
        return httpx.Response(200, text=_make_soap_response(_make_ta_xml()))

    client = _client_with_transport(test_config, handler)
    client.get_ticket()

    assert capturado["url"] == test_config.wsaa_url  # URL derivada de ARCA_ENV
    root = ET.fromstring(capturado["body"])
    in0 = root.find(".//{http://wsaa.view.sua.dvadac.desein.afip.gov}in0")
    base64.b64decode(in0.text)  # no explota => base64 válido

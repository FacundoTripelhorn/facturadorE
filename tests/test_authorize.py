"""Fase 3 — FEXAuthorize: armado del Cmp, escaping hostil, reproceso seguro,
numeración y verificación post-emisión."""

import datetime as dt
import xml.etree.ElementTree as ET
from decimal import Decimal

import httpx
import pytest

from facturador.arca.wsaa import Ticket
from facturador.arca.wsfex import (
    FEX_NS,
    AuthResult,
    Invoice,
    InvoiceItem,
    WsfexClient,
    WsfexError,
    build_cmp_element,
)

# Orden de secuencia del ClsFEXRequest según el WSDL real (2026-07).
WSDL_ORDER = [
    "Id", "Fecha_cbte", "Cbte_Tipo", "Punto_vta", "Cbte_nro", "Tipo_expo",
    "Permiso_existente", "Permisos", "Dst_cmp", "Cliente", "Cuit_pais_cliente",
    "Domicilio_cliente", "Id_impositivo", "Moneda_Id", "Moneda_ctz",
    "CanMisMonExt", "Obs_comerciales", "Imp_total", "Obs", "Cmps_asoc",
    "Forma_pago", "Incoterms", "Incoterms_Ds", "Idioma_cbte", "Items",
    "Opcionales", "Fecha_pago", "Actividades",
]


def _invoice(**overrides) -> Invoice:
    base = dict(
        arca_id=1,
        fecha_cbte="20260703",
        punto_vta=1,
        cbte_nro=1,
        dst_cmp=225,
        cliente="CLIENTE PRUEBA S.A.",
        cuit_pais_cliente=55000002002,
        domicilio_cliente="Montevideo",
        id_impositivo="RUT 219999830019",
        moneda_ctz=Decimal("1000.50"),
        imp_total=Decimal("100.00"),
        fecha_pago="20260704",
        forma_pago="WIRE TRANSFER",
        items=[InvoiceItem(pro_ds="Servicios", pro_precio_uni=Decimal("100.00"))],
    )
    base.update(overrides)
    return Invoice(**base)


class _FakeWsaa:
    def get_ticket(self):
        return Ticket(
            token="tok==",
            sign="sig==",
            generation=dt.datetime.now(dt.UTC),
            expiration=dt.datetime.now(dt.UTC) + dt.timedelta(hours=12),
            service="wsfex",
            environment="homo",
        )


def _client(test_config, handler) -> WsfexClient:
    return WsfexClient(
        test_config,
        wsaa=_FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _soap(method: str, inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{method}Response xmlns="{FEX_NS}">'
        f"<{method}Result>{inner}</{method}Result>"
        f"</{method}Response></soap:Body></soap:Envelope>"
    )


def _auth_ok(reproceso="N", cae="76123456789012", cbte_nro=1, arca_id=1):
    return _soap(
        "FEXAuthorize",
        "<FEXResultAuth>"
        f"<Id>{arca_id}</Id><Cbte_nro>{cbte_nro}</Cbte_nro>"
        f"<Cae>{cae}</Cae><Fch_venc_Cae>20260713</Fch_venc_Cae>"
        "<Fecha_cbte>20260703</Fecha_cbte><Cbte_tipo>19</Cbte_tipo>"
        f"<Punto_vta>1</Punto_vta><Reproceso>{reproceso}</Reproceso>"
        "</FEXResultAuth>"
        "<FEXErr><ErrCode>0</ErrCode><ErrMsg>OK</ErrMsg></FEXErr>",
    )


# --- armado del Cmp ---


def test_cmp_respeta_el_orden_del_wsdl():
    cmp_el = build_cmp_element(_invoice())
    tags = [t.tag.rsplit("}", 1)[-1] for t in cmp_el]
    posiciones = [WSDL_ORDER.index(t) for t in tags]
    assert posiciones == sorted(posiciones), f"Orden inválido: {tags}"


def test_cmp_valores_correctos():
    cmp_el = build_cmp_element(_invoice())

    def get(tag):
        return cmp_el.findtext(f"{{{FEX_NS}}}{tag}")

    assert get("Id") == "1"
    assert get("Tipo_expo") == "2"
    assert get("Permiso_existente") == ""
    assert get("Incoterms") == ""  # vacío en servicios
    assert get("Moneda_ctz") == "1000.50"
    assert get("Imp_total") == "100.00"
    assert get("Fecha_pago") == "20260704"
    item = cmp_el.find(f"{{{FEX_NS}}}Items/{{{FEX_NS}}}Item")
    assert item.findtext(f"{{{FEX_NS}}}Pro_umed") == "7"
    assert item.findtext(f"{{{FEX_NS}}}Pro_bonificacion") == "0"
    assert item.findtext(f"{{{FEX_NS}}}Pro_total_item") == "100.00"


def test_datos_hostiles_quedan_escapados():
    """Checklist §2.1.1 punto 4: XML injection en campos de texto libre."""
    hostil = '</Cliente><Imp_total>999999</Imp_total><x a="&\'"> <![CDATA[y]]>'
    invoice = _invoice(cliente=hostil, domicilio_cliente="<script>&amp;")
    xml_bytes = ET.tostring(build_cmp_element(invoice))
    # El XML resultante sigue siendo parseable y el valor vuelve intacto.
    parsed = ET.fromstring(xml_bytes)
    assert parsed.findtext(f"{{{FEX_NS}}}Cliente") == hostil
    assert parsed.findtext(f"{{{FEX_NS}}}Imp_total") == "100.00"
    assert b"<Imp_total>999999" not in xml_bytes


def test_imp_total_debe_igualar_suma_de_items():
    with pytest.raises(ValueError, match="Imp_total"):
        build_cmp_element(_invoice(imp_total=Decimal("999.99")))


# --- numeración y cotización ---


def test_last_id_y_last_cmp(test_config):
    def handler(request):
        body = request.content.decode()
        if "FEXGetLast_ID" in body:
            return httpx.Response(
                200,
                text=_soap("FEXGetLast_ID", "<FEXResultGet><Id>41</Id></FEXResultGet>"),
            )
        # El Auth de Last_CMP lleva Pto_venta y Cbte_Tipo adentro.
        assert "<Pto_venta" in body.replace("ns0:", "") or "Pto_venta" in body
        return httpx.Response(
            200,
            text=_soap(
                "FEXGetLast_CMP",
                "<FEXResult_LastCMP><Cbte_nro>7</Cbte_nro></FEXResult_LastCMP>",
            ),
        )

    client = _client(test_config, handler)
    assert client.get_last_id() == 41
    assert client.get_last_cmp(1, 19) == 7


def test_get_ctz(test_config):
    respuesta = _soap(
        "FEXGetPARAM_Ctz",
        "<FEXResultGet><Mon_ctz>1000.50</Mon_ctz><Mon_fecha>20260703</Mon_fecha></FEXResultGet>",
    )
    client = _client(test_config, lambda r: httpx.Response(200, text=respuesta))
    ctz, fecha = client.get_ctz("DOL")
    assert ctz == Decimal("1000.50")
    assert fecha == "20260703"


# --- authorize: aceptación y reproceso seguro (checklist punto 3) ---


def test_authorize_ok_devuelve_cae(test_config):
    client = _client(test_config, lambda r: httpx.Response(200, text=_auth_ok()))
    resultado = client.authorize(_invoice())
    assert resultado.cae == "76123456789012"
    assert resultado.cae_fch_vto == "20260713"
    assert resultado.reproceso == "N"


def test_authorize_rechaza_respuesta_que_no_corresponde_al_request(test_config):
    # ARCA devuelve un CAE de OTRO comprobante (número distinto): no aceptarlo.
    client = _client(
        test_config, lambda r: httpx.Response(200, text=_auth_ok(cbte_nro=99))
    )
    with pytest.raises(WsfexError, match="no corresponde al request"):
        client.authorize(_invoice())


def test_reproceso_con_datos_coincidentes_se_acepta(test_config):
    client = _client(
        test_config, lambda r: httpx.Response(200, text=_auth_ok(reproceso="S"))
    )
    resultado = client.authorize(_invoice())
    assert resultado.reproceso == "S"
    assert resultado.cae == "76123456789012"


# --- verificación post-emisión (checklist punto 6) ---


CMP_REGISTRADO = _soap(
    "FEXGetCMP",
    "<FEXResultGet>"
    "<Cbte_tipo>19</Cbte_tipo><Punto_vta>1</Punto_vta><Cbte_nro>1</Cbte_nro>"
    "<Cae>76123456789012</Cae><Imp_total>100.00</Imp_total>"
    "</FEXResultGet>",
)


def test_verify_issued_ok(test_config):
    client = _client(test_config, lambda r: httpx.Response(200, text=CMP_REGISTRADO))
    resultado = AuthResult(
        cae="76123456789012", cae_fch_vto="20260713", arca_id=1, cbte_nro=1,
        cbte_tipo=19, punto_vta=1, fecha_cbte="20260703", reproceso="N",
        motivos_obs=None,
    )
    assert client.verify_issued(_invoice(), resultado) == []


def test_verify_issued_detecta_discrepancias(test_config):
    client = _client(test_config, lambda r: httpx.Response(200, text=CMP_REGISTRADO))
    resultado = AuthResult(
        cae="OTRO_CAE", cae_fch_vto="20260713", arca_id=1, cbte_nro=1,
        cbte_tipo=19, punto_vta=1, fecha_cbte="20260703", reproceso="N",
        motivos_obs=None,
    )
    problemas = client.verify_issued(_invoice(imp_total=Decimal("100.00")), resultado)
    assert any("CAE" in p for p in problemas)

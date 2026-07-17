"""Fase 5 — PDF + QR RG 4892.

Cubre: contenido del payload del QR contra la spec RG 4892, contrato del
endpoint (200/409/404), copia persistida en data/pdfs y escaping de datos
hostiles en el HTML del comprobante.
"""

import base64
import datetime as dt
import json
from urllib.parse import parse_qs, urlparse

import pytest

from facturador import repo
from facturador.pdf import render_invoice_html
from facturador.pdf.qr import QR_BASE_URL, build_qr_payload, qr_url
from facturador.settings import Emisor, load_settings
from tests.conftest import TEST_CUIT

CLIENTE = {
    "razon_social": "CLIENTE URUGUAY S.A.",
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "RUT 219999830019",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": True,
}


def _factura_autorizada(api, **cliente_overrides):
    alta = api.post("/clients", json={**CLIENTE, **cliente_overrides})
    assert alta.status_code == 201, alta.text
    draft = api.post("/invoices", json={"imp_total": "1500.00"}).json()
    r = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert r.status_code == 200, r.text
    return r.json()


# --- QR RG 4892 ---


def test_qr_payload_cumple_rg4892(api, arca):
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])

    payload = build_qr_payload(inv, int(TEST_CUIT))

    hoy = dt.date.today()
    assert payload == {
        "ver": 1,
        "fecha": hoy.isoformat(),
        "cuit": int(TEST_CUIT),
        "ptoVta": 1,
        "tipoCmp": 19,
        "nroCmp": 1,
        "importe": 1500.0,
        "moneda": "DOL",
        "ctz": 1145.569,
        "tipoDocRec": 80,
        "nroDocRec": 55000002002,
        "tipoCodAut": "E",
        "codAut": 76100000000001,
    }


def test_qr_url_lleva_el_payload_en_base64(api, arca):
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    payload = build_qr_payload(inv, int(TEST_CUIT))

    url = qr_url(payload)

    assert url.startswith(f"{QR_BASE_URL}?p=")
    p = parse_qs(urlparse(url).query)["p"][0]
    assert json.loads(base64.b64decode(p)) == payload


def test_qr_exige_factura_con_cae(api, arca):
    api.post("/clients", json=CLIENTE)
    draft = api.post("/invoices", json={"imp_total": "10.00"}).json()
    inv = repo.get_invoice(api.conn, draft["id"])
    try:
        build_qr_payload(inv, int(TEST_CUIT))
        raise AssertionError("debió rechazar una factura sin CAE")
    except ValueError:
        pass


# --- endpoint /invoices/:id/pdf ---


def _weasyprint_disponible() -> bool:
    # weasyprint levanta OSError (no ImportError) si faltan las libs nativas
    # de Pango, así que pytest.importorskip no alcanza.
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


@pytest.mark.skipif(
    not _weasyprint_disponible(),
    reason="weasyprint sin libs nativas (Pango/GTK); el runtime real es Docker",
)
def test_pdf_de_factura_autorizada(api, arca, test_config):
    factura = _factura_autorizada(api)

    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")
    filename = "factura-E-00001-00000001-homo.pdf"
    assert filename in r.headers["content-disposition"]
    # Copia persistida en el pdfs/ del perfil (FAC-25), idéntica a la respuesta.
    assert (test_config.paths.pdf_dir / filename).read_bytes() == r.content


def test_pdf_de_draft_es_conflicto(api, arca):
    api.post("/clients", json=CLIENTE)
    draft = api.post("/invoices", json={"imp_total": "10.00"}).json()
    r = api.get(f"/invoices/{draft['id']}/pdf")
    assert r.status_code == 409
    assert "draft" in r.json()["detail"]


def test_pdf_inexistente_es_404(api):
    assert api.get("/invoices/inexistente/pdf").status_code == 404


def test_el_pdf_usa_el_snapshot_del_emisor_no_la_fila_viva(
    api, arca, monkeypatch
):
    """FAC-10: editar el mismo emisor tras autorizar no muda el PDF."""
    factura = _factura_autorizada(api)
    emisor_id = repo.get_invoice(api.conn, factura["id"])["emisor_id"]
    repo.update_emisor(
        api.conn,
        emisor_id,
        {
            "razon_social": "RAZON SOCIAL EDITADA S.A.",
            "domicilio": "Calle Nueva 999",
            "iibb": "Convenio Multilateral",
            "inicio_actividades": "15/03/2021",
            "condicion_iva": "IVA Responsable Inscripto",
            "puntos_venta": "[1]",
        },
    )
    assert load_settings(api.conn).emisor.razon_social == "RAZON SOCIAL EDITADA S.A."

    capturado = {}

    def fake_render(inv, items, emisor, cuit_emisor, **kwargs):
        capturado["emisor"] = emisor
        capturado["inv"] = inv
        return b"%PDF-fake"

    monkeypatch.setattr(
        "facturador.api.invoices.render_invoice_pdf", fake_render
    )
    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert capturado["emisor"].id == emisor_id
    assert capturado["emisor"].razon_social == "MI EMPRESA S.R.L."
    assert capturado["emisor"].domicilio == "Calle Falsa 123, CABA"
    assert capturado["emisor"].iibb == "Exento"
    assert capturado["emisor"].inicio_actividades == "01/08/2020"
    assert capturado["inv"]["emisor_razon_social"] == "MI EMPRESA S.R.L."
    assert "RAZON SOCIAL EDITADA" not in capturado["emisor"].razon_social


def test_el_pdf_conserva_el_emisor_activo_al_momento_de_emitir(api, arca, monkeypatch):
    """Cambiar el emisor activo no altera el encabezado de facturas ya creadas."""
    factura = _factura_autorizada(api)
    emisor_original = repo.get_invoice(api.conn, factura["id"])["emisor_id"]

    segundo = repo.create_emisor(
        api.conn,
        {
            "razon_social": "OTRO EMISOR S.A.",
            "domicilio": "Otra calle 1",
            "iibb": "Exento",
            "inicio_actividades": "01/01/2019",
            "condicion_iva": "IVA Responsable Inscripto",
            "ambiente": "homo",
            "puntos_venta": "[5]",
        },
    )
    from facturador.settings import set_active_emisor

    set_active_emisor(api.conn, segundo["id"])
    assert load_settings(api.conn).emisor.razon_social == "OTRO EMISOR S.A."

    capturado = {}

    def fake_render(inv, items, emisor, cuit_emisor, **kwargs):
        capturado["emisor"] = emisor
        return b"%PDF-fake"

    monkeypatch.setattr(
        "facturador.api.invoices.render_invoice_pdf", fake_render
    )
    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert capturado["emisor"].id == emisor_original
    assert capturado["emisor"].razon_social == "MI EMPRESA S.R.L."


def test_editar_emisor_tras_draft_no_cambia_el_snapshot(api, arca):
    """FAC-10: el borrador revisado es el que se autorizará."""
    api.post("/clients", json=CLIENTE)
    draft = api.post("/invoices", json={"imp_total": "10.00"}).json()
    assert draft["emisor_razon_social"] == "MI EMPRESA S.R.L."
    assert draft["emisor_domicilio"] == "Calle Falsa 123, CABA"
    assert draft["emisor_iibb"] == "Exento"
    assert draft["emisor_inicio_actividades"] == "01/08/2020"
    assert draft["emisor_condicion_iva"] == "IVA Responsable Inscripto"

    emisor_id = draft["emisor_id"]
    repo.update_emisor(
        api.conn,
        emisor_id,
        {
            "razon_social": "CAMBIO POST DRAFT S.A.",
            "domicilio": "Otro domicilio 1",
            "iibb": "Local",
            "inicio_actividades": "02/02/2022",
            "condicion_iva": "IVA Responsable Inscripto",
            "puntos_venta": "[1]",
        },
    )

    inv = repo.get_invoice(api.conn, draft["id"])
    assert inv is not None
    assert inv["emisor_razon_social"] == "MI EMPRESA S.R.L."
    assert inv["emisor_domicilio"] == "Calle Falsa 123, CABA"
    assert inv["emisor_iibb"] == "Exento"
    assert inv["emisor_inicio_actividades"] == "01/08/2020"

    auth = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert auth.status_code == 200, auth.text
    body = auth.json()
    assert body["emisor_razon_social"] == "MI EMPRESA S.R.L."
    assert body["emisor_domicilio"] == "Calle Falsa 123, CABA"


# --- contenido y escaping del HTML ---


def test_html_contiene_los_datos_del_comprobante(api, arca):
    # Los datos del emisor salen del snapshot de la factura (FAC-10), no
    # de la fila viva de emisores ni del entorno.
    from facturador.settings import emisor_from_invoice_snapshot

    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])
    emisor = emisor_from_invoice_snapshot(inv)

    html = render_invoice_html(
        inv,
        items,
        emisor,
        int(TEST_CUIT),
        pais_ds="URUGUAY",
        cuit_pais_ds="URUGUAY - Persona Juridica",
        moneda_ds="Dolar Estadounidense",
    )

    assert "MI EMPRESA S.R.L." in html
    # IIBB sale literal de la config ("Exento" en el comprobante real),
    # nunca el CUIT como reemplazo.
    assert "Exento" in html
    assert "01/08/2020" in html
    assert "IVA Responsable Inscripto" in html
    assert "CLIENTE URUGUAY S.A." in html
    # Paridad con el comprobante real de Comprobantes en Línea:
    assert "Destino del Comprobante:</b> URUGUAY" in html
    assert "(URUGUAY - Persona Juridica)" in html         # CUIT País con descripción
    assert "USD - Dolar Estadounidense" in html           # divisa con descripción
    assert "Compr. Nro:</b> 00001-00000001" in html       # rótulo real, con la "r"
    # Descripción con el código adelante, como imprime Comprobantes en Línea.
    assert "0001 - Servicios de desarrollo de software" in html
    # U. Medida no es columna propia: va como sub-línea bajo la cantidad.
    assert "U. Medida: unidades" in html
    assert "<th>U. Medida</th>" not in html
    assert "1500,00" in html                              # importes con coma, 2 dec
    assert "1,000000" in html                             # cantidad con 6 decimales
    assert "1500,000000" in html                          # precio unit. con 6 dec
    assert "1145.569000" in html                          # cotización: punto, 6 dec
    assert "IVA EXENTO OPERACIÓN DE EXPORTACIÓN" in html
    assert "Comprobante Autorizado" in html
    assert "76100000000001" in html                       # CAE
    assert "data:image/png;base64," in html               # QR incrustado
    assert "SIN VALOR FISCAL" in html                     # marca de homologación


def test_datos_hostiles_quedan_escapados_en_el_html(api, arca):
    factura = _factura_autorizada(
        api, razon_social='PYME <script>alert(1)</script> & "CO"'
    )
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])

    html = render_invoice_html(
        inv, items, load_settings(api.conn).emisor, int(TEST_CUIT)
    )

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_datos_hostiles_del_emisor_quedan_escapados_en_el_html(api, arca):
    """Los datos del emisor ahora también son texto libre (vienen de la DB
    vía la UI): mismo tratamiento que los del cliente (checklist §2.1.1
    punto 4 aplicado al HTML)."""
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])
    emisor = Emisor(
        razon_social='EMISORA <img src=x onerror=alert(1)> & "SA"',
        domicilio="Av. <b>Negrita</b> 1",
    )

    html = render_invoice_html(inv, items, emisor, int(TEST_CUIT))

    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    assert "<b>Negrita</b>" not in html

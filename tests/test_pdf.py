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
from facturador.settings import Emisor, Settings, load_settings, save_settings
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
    # Copia persistida en data/pdfs (layout §2.5), idéntica a la respuesta.
    assert (test_config.pdf_dir / filename).read_bytes() == r.content


def test_pdf_de_draft_es_conflicto(api, arca):
    api.post("/clients", json=CLIENTE)
    draft = api.post("/invoices", json={"imp_total": "10.00"}).json()
    r = api.get(f"/invoices/{draft['id']}/pdf")
    assert r.status_code == 409
    assert "draft" in r.json()["detail"]


def test_pdf_inexistente_es_404(api):
    assert api.get("/invoices/inexistente/pdf").status_code == 404


def test_el_pdf_usa_el_emisor_del_ambiente_del_comprobante(api, arca, monkeypatch):
    """El comprobante es un snapshot: su PDF usa el emisor del ambiente en
    que se emitió (inv["environment"]), no el del ambiente activo — mismo
    criterio que es_homo. Acá la factura es de homo y aparece un emisor de
    prod que no debe filtrarse al PDF."""
    factura = _factura_autorizada(api)
    save_settings(
        api.conn,
        Settings(emisor=Emisor(razon_social="OTRA S.R.L.", ambiente="prod")),
    )
    capturado = {}

    def fake_render(inv, items, emisor, cuit_emisor, pais_ds=""):
        capturado["emisor"] = emisor
        return b"%PDF-fake"

    monkeypatch.setattr(
        "facturador.api.invoices.render_invoice_pdf", fake_render
    )
    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert capturado["emisor"].ambiente == "homo"
    assert capturado["emisor"].razon_social == "MI EMPRESA S.R.L."


# --- contenido y escaping del HTML ---


def test_html_contiene_los_datos_del_comprobante(api, arca):
    # Los datos del emisor salen de la DB (sembrados por seed_settings en
    # el fixture), no del entorno.
    emisor = load_settings(api.conn, "homo").emisor
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])

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
        inv, items, load_settings(api.conn, "homo").emisor, int(TEST_CUIT)
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

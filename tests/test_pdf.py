"""Fase 5 — PDF + QR RG 4892.

Cubre: contenido del payload del QR contra la spec RG 4892, contrato del
endpoint (200/409/404), cache local descartable en data/pdfs (FAC-53),
datos hostiles impresos literales, snapshot inmutable, despacho por
``pdf_render_version`` y el motor fpdf2 de una sola página. El contenido
se verifica extrayendo el texto del PDF real.
"""

import base64
import datetime as dt
import io
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pypdf import PdfReader

from facturador import repo
from facturador.constants import PDF_RENDER_VERSION
from facturador.pdf import (
    PdfLayoutError,
    get_or_render_invoice_pdf,
    invoice_pdf_cache_path,
    known_pdf_render_versions,
    render_invoice_pdf,
)
from facturador.pdf.qr import QR_BASE_URL, build_qr_payload, qr_url
from facturador.pdf.registry import PDF_RENDERERS
from facturador.settings import load_settings
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


def _texto(pdf: bytes) -> str:
    """Texto del PDF con los espacios normalizados (una sola página)."""
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    return " ".join(reader.pages[0].extract_text().split())


def _pdf_de(api, factura) -> bytes:
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])
    return render_invoice_pdf(inv, items)


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


def test_pdf_de_factura_autorizada(api, arca, test_config):
    factura = _factura_autorizada(api)

    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")
    filename = "factura-E-19-00001-00000001-homo.pdf"
    assert filename in r.headers["content-disposition"]
    # Cache local en pdfs/ del perfil (FAC-25/53), idéntica a la respuesta.
    assert (test_config.paths.pdf_dir / filename).read_bytes() == r.content


def test_pdf_de_draft_es_conflicto(api, arca):
    api.post("/clients", json=CLIENTE)
    draft = api.post("/invoices", json={"imp_total": "10.00"}).json()
    r = api.get(f"/invoices/{draft['id']}/pdf")
    assert r.status_code == 409
    assert "draft" in r.json()["detail"]


def test_pdf_que_no_entra_en_una_pagina_es_409_accionable(api, arca):
    """Sin multipágina: un comprobante demasiado largo falla en claro."""
    factura = _factura_autorizada(api)
    with api.conn:
        api.conn.execute(
            "UPDATE invoice_items SET pro_ds = ? WHERE invoice_id = ?",
            ("Servicio muy detallado " * 400, factura["id"]),
        )

    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 409
    assert "no entra en una página A4" in r.json()["detail"]


def test_layout_error_es_value_error():
    """La ruta mapea ValueError → 409; PdfLayoutError tiene que caer ahí."""
    assert issubclass(PdfLayoutError, ValueError)


def test_observaciones_largas_tambien_hacen_overflow(api, arca):
    factura = _factura_autorizada(api)
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET obs = ? WHERE id = ?",
            ("Observación extensa. " * 600, factura["id"]),
        )
    with pytest.raises(PdfLayoutError, match="no entra en una página A4"):
        _pdf_de(api, factura)


def test_observaciones_se_imprimen(api, arca):
    factura = _factura_autorizada(api)
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET obs = ? WHERE id = ?",
            ("Pago en dos cuotas", factura["id"]),
        )
    assert "Observaciones: Pago en dos cuotas" in _texto(_pdf_de(api, factura))


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

    def fake_render(inv, items):
        capturado["inv"] = inv
        capturado["items"] = items
        return b"%PDF-fake"

    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf", fake_render
    )
    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert capturado["inv"]["emisor_razon_social"] == "MI EMPRESA S.R.L."
    assert capturado["inv"]["emisor_domicilio"] == "Calle Falsa 123, CABA"
    assert capturado["inv"]["emisor_iibb"] == "Exento"
    assert capturado["inv"]["emisor_inicio_actividades"] == "01/08/2020"
    assert "RAZON SOCIAL EDITADA" not in capturado["inv"]["emisor_razon_social"]


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

    def fake_render(inv, items):
        capturado["inv"] = inv
        return b"%PDF-fake"

    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf", fake_render
    )
    r = api.get(f"/invoices/{factura['id']}/pdf")

    assert r.status_code == 200, r.text
    assert capturado["inv"]["emisor_id"] == emisor_original
    assert capturado["inv"]["emisor_razon_social"] == "MI EMPRESA S.R.L."


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


def test_nueva_factura_registra_snapshot_de_render_completo(api, arca):
    """FAC-52: descripciones de params + pdf_render_version al crear."""
    api.post("/clients", json=CLIENTE)
    draft = api.post("/invoices", json={"imp_total": "10.00"}).json()

    assert draft["pdf_render_version"] == PDF_RENDER_VERSION
    assert draft["dst_cmp_ds"] == "URUGUAY"
    assert draft["cuit_pais_cliente_ds"] == "URUGUAY - Persona Juridica"
    assert draft["moneda_ds"] == "Dolar Estadounidense"
    assert draft["items"][0]["pro_umed_ds"] == "unidades"

    inv = repo.get_invoice(api.conn, draft["id"])
    assert inv is not None
    assert inv["pdf_render_version"] == PDF_RENDER_VERSION
    items = repo.get_invoice_items(api.conn, draft["id"])
    assert items[0]["pro_umed_ds"] == "unidades"


def test_editar_cliente_params_y_settings_no_cambia_inputs_del_render(
    api, arca, monkeypatch
):
    """FAC-52: regenerar el PDF no depende de filas vivas ni del cache."""
    factura = _factura_autorizada(api)
    client_id = factura["client_id"]

    # Mutar cliente vivo.
    edited = api.put(
        f"/clients/{client_id}",
        json={
            **CLIENTE,
            "razon_social": "CLIENTE EDITADO S.A.",
            "domicilio": "Otra calle 99",
            "id_impositivo": "RUT EDITADO",
        },
    )
    assert edited.status_code == 200, edited.text
    # Mutar descripciones de arca_params (fuente dinámica del label).
    repo.replace_params(
        api.conn,
        "pais",
        [{"code": "225", "description": "PAIS MUTADO"}],
    )
    repo.replace_params(
        api.conn,
        "cuit_pais",
        [{"code": "55000002002", "description": "CUIT PAIS MUTADO"}],
    )
    repo.replace_params(
        api.conn,
        "moneda",
        [
            {"code": "DOL", "description": "MONEDA MUTADA"},
            {"code": "PES", "description": "Peso Argentino"},
        ],
    )
    repo.replace_params(
        api.conn,
        "umed",
        [{"code": "7", "description": "umed-mutada"}],
    )
    # Mutar emisor / settings activos.
    emisor_id = repo.get_invoice(api.conn, factura["id"])["emisor_id"]
    repo.update_emisor(
        api.conn,
        emisor_id,
        {
            "razon_social": "EMISOR MUTADO S.A.",
            "domicilio": "Domicilio mutado",
            "iibb": "Local mutado",
            "inicio_actividades": "99/99/9999",
            "condicion_iva": "IVA mutado",
            "puntos_venta": "[1]",
        },
    )

    capturado = {}

    def fake_render(inv, items):
        capturado["inv"] = dict(inv)
        capturado["items"] = [dict(i) for i in items]
        return b"%PDF-fake"

    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf", fake_render
    )
    r = api.get(f"/invoices/{factura['id']}/pdf")
    assert r.status_code == 200, r.text

    inv = capturado["inv"]
    item = capturado["items"][0]
    assert inv["cliente"] == "CLIENTE URUGUAY S.A."
    assert inv["domicilio_cliente"] == "Av. Siempreviva 123, Montevideo"
    assert inv["id_impositivo"] == "RUT 219999830019"
    assert inv["dst_cmp_ds"] == "URUGUAY"
    assert inv["cuit_pais_cliente_ds"] == "URUGUAY - Persona Juridica"
    assert inv["moneda_ds"] == "Dolar Estadounidense"
    assert inv["emisor_razon_social"] == "MI EMPRESA S.R.L."
    assert inv["pdf_render_version"] == PDF_RENDER_VERSION
    assert item["pro_umed_ds"] == "unidades"
    assert "PAIS MUTADO" not in inv["dst_cmp_ds"]
    assert "MONEDA MUTADA" not in inv["moneda_ds"]
    assert "umed-mutada" not in item["pro_umed_ds"]


def test_pdf_usa_solo_snapshot_sin_lookups_externos(api, arca):
    """render_invoice_pdf(inv, items) sin kwargs de params/emisor."""
    factura = _factura_autorizada(api)
    # Envenenar el cache: si el render lo leyera, el HTML saldría mutado.
    repo.replace_params(
        api.conn, "pais", [{"code": "225", "description": "XX-MUTADO"}]
    )
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])

    texto = _texto(render_invoice_pdf(inv, items))

    assert "URUGUAY" in texto
    assert "XX-MUTADO" not in texto
    assert "URUGUAY - Persona Juridica" in texto
    assert "Dolar Estadounidense" in texto
    assert "U. Medida: unidades" in texto


# --- contenido del PDF y datos hostiles ---


def test_pdf_contiene_los_datos_del_comprobante(api, arca):
    # Los datos del emisor y las descripciones salen del snapshot (FAC-10/52).
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])

    pdf = render_invoice_pdf(inv, items)
    texto = _texto(pdf)

    assert "MI EMPRESA S.R.L." in texto
    # IIBB sale literal de la config ("Exento" en el comprobante real),
    # nunca el CUIT como reemplazo.
    assert "Ingresos Brutos: Exento" in texto
    assert "Fecha de Inicio de Actividades: 01/08/2020" in texto
    assert "Condición frente al IVA: IVA Responsable Inscripto" in texto
    assert f"CUIT: {TEST_CUIT}" in texto
    assert "Señor(es): CLIENTE URUGUAY S.A." in texto
    assert "Domicilio: Av. Siempreviva 123, Montevideo" in texto
    assert "ID Impositivo: RUT 219999830019" in texto
    # Paridad con el comprobante real de Comprobantes en Línea:
    assert "Destino del Comprobante: URUGUAY" in texto
    assert "(URUGUAY - Persona Juridica)" in texto        # CUIT País con descripción
    assert "Divisa: USD - Dolar Estadounidense" in texto  # divisa con descripción
    assert "Compr. Nro: 00001-00000001" in texto          # rótulo real, con la "r"
    assert "Forma de Pago: WIRE TRANSFER" in texto
    assert "Incoterms:" in texto
    # Descripción con el código adelante, como imprime Comprobantes en Línea.
    assert "0001 - Servicios de desarrollo de software" in texto
    # U. Medida no es columna propia: va como sub-línea bajo la cantidad.
    assert "U. Medida: unidades" in texto
    assert "1500,00" in texto                             # importes con coma, 2 dec
    assert "1,000000" in texto                            # cantidad con 6 decimales
    assert "1500,000000" in texto                         # precio unit. con 6 dec
    assert "Tipo de Cambio: 1145.569000" in texto         # cotización: punto, 6 dec
    assert "Importe Total: USD 1500,00" in texto
    assert "IVA EXENTO OPERACIÓN DE EXPORTACIÓN" in texto
    assert "FACTURA DE EXPORTACIÓN" in texto
    assert "COD. 19" in texto
    assert "Comprobante Autorizado" in texto
    assert "CAE N°: 76100000000001" in texto
    assert "Fecha de Vto. de CAE:" in texto
    assert "SIN VALOR FISCAL" in texto                    # marca de homologación
    # QR RG 4892 incrustado como imagen.
    assert len(PdfReader(io.BytesIO(pdf)).pages[0].images) == 1


def test_pdf_de_produccion_no_lleva_marca_de_homologacion(api, arca):
    factura = _factura_autorizada(api)
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET environment = 'prod' WHERE id = ?",
            (factura["id"],),
        )
    assert "SIN VALOR FISCAL" not in _texto(_pdf_de(api, factura))


def test_datos_hostiles_se_imprimen_literales(api, arca):
    """fpdf2 no interpreta markup: el texto hostil sale tal cual, sin
    ejecutarse ni formatearse (checklist §2.1.1 punto 4)."""
    factura = _factura_autorizada(
        api, razon_social='PYME <script>alert(1)</script> & "CO" **negrita**'
    )

    texto = _texto(_pdf_de(api, factura))

    assert 'PYME <script>alert(1)</script> & "CO" **negrita**' in texto


def test_caracteres_fuera_de_latin1_se_imprimen(api, arca):
    """La fuente embebida (Liberation Sans) cubre UTF-8 más allá de Latin-1."""
    factura = _factura_autorizada(
        api, razon_social="Łódź Spółka — Žilina s.r.o. €"
    )

    texto = _texto(_pdf_de(api, factura))

    assert "Łódź Spółka — Žilina s.r.o. €" in texto


def test_datos_hostiles_del_emisor_se_imprimen_literales(api, arca):
    """Los datos del emisor también son texto libre (vienen de la DB vía la
    UI): mismo tratamiento que los del cliente."""
    factura = _factura_autorizada(api)
    # Inyectar texto hostil en el snapshot de la fila (fuente real del PDF).
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET emisor_razon_social = ?, emisor_domicilio = ?"
            " WHERE id = ?",
            (
                'EMISORA <img src=x onerror=alert(1)> & "SA"',
                "Av. <b>Negrita</b> 1",
                factura["id"],
            ),
        )

    texto = _texto(_pdf_de(api, factura))

    assert '<img src=x onerror=alert(1)> & "SA"' in texto
    assert "Av. <b>Negrita</b> 1" in texto


def test_baseline_incluye_snapshot_de_render(tmp_path):
    """FAC-52: columnas de render en el baseline — sin migración de upgrade."""
    from facturador import db
    from facturador.migrations import latest_version

    conn = db.connect(tmp_path / "fresh.db")
    try:
        inv_cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        item_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(invoice_items)")
        }
        for name in (
            "dst_cmp_ds",
            "cuit_pais_cliente_ds",
            "moneda_ds",
            "pdf_render_version",
        ):
            assert name in inv_cols
        assert "pro_umed_ds" in item_cols
        # Guardrail: snapshot de render sigue en el baseline (una migración).
        assert latest_version() == 1
    finally:
        conn.close()


# --- FAC-53: despacho versionado + cache descartable ---


def test_renderer_v1_esta_registrado():
    """FAC-53: el layout actual es la versión 1 del registry."""
    assert PDF_RENDER_VERSION == 1
    assert known_pdf_render_versions() == [1]
    assert 1 in PDF_RENDERERS


def test_version_desconocida_falla_en_claro(api, arca):
    """FAC-53: no se cae al template más nuevo en silencio."""
    factura = _factura_autorizada(api)
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET pdf_render_version = 999 WHERE id = ?",
            (factura["id"],),
        )
    inv = repo.get_invoice(api.conn, factura["id"])
    items = repo.get_invoice_items(api.conn, factura["id"])

    with pytest.raises(ValueError, match="pdf_render_version=999") as exc:
        render_invoice_pdf(inv, items)
    assert "versiones conocidas: 1" in str(exc.value)

    r = api.get(f"/invoices/{factura['id']}/pdf")
    assert r.status_code == 409
    assert "pdf_render_version=999" in r.json()["detail"]


def test_cache_ausente_regenera_desde_db(api, arca, test_config, monkeypatch):
    """FAC-53: sin archivo en pdfs/, se regenera desde el snapshot."""
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    assert inv is not None
    cache = invoice_pdf_cache_path(test_config.paths.pdf_dir, inv)
    assert not cache.exists()

    calls = {"n": 0}

    def fake_render(row, items):
        calls["n"] += 1
        return b"%PDF-regenerado"

    monkeypatch.setattr("facturador.pdf.render.render_invoice_pdf", fake_render)

    pdf = get_or_render_invoice_pdf(inv, [], test_config.paths.pdf_dir)
    assert pdf == b"%PDF-regenerado"
    assert calls["n"] == 1
    assert cache.read_bytes() == b"%PDF-regenerado"


def test_cache_hit_no_vuelve_a_renderizar(api, arca, test_config, monkeypatch):
    """FAC-53: si el cache existe, se sirve sin re-render."""
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    assert inv is not None
    cache = invoice_pdf_cache_path(test_config.paths.pdf_dir, inv)
    test_config.paths.pdf_dir.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"%PDF-desde-cache")

    def boom(_inv, _items):
        raise AssertionError("no debió regenerar con cache presente")

    monkeypatch.setattr("facturador.pdf.render.render_invoice_pdf", boom)
    r = api.get(f"/invoices/{factura['id']}/pdf")
    assert r.status_code == 200, r.text
    assert r.content == b"%PDF-desde-cache"


def test_borrar_cache_no_pierde_registro_fiscal(
    api, arca, test_config, monkeypatch
):
    """FAC-53: borrar pdfs/ no toca CAE ni fila en SQLite; se regenera."""
    factura = _factura_autorizada(api)
    invoice_id = factura["id"]
    cae = factura["cae"]
    assert cae

    # Primera descarga: crea cache.
    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf",
        lambda inv, items: b"%PDF-primera",
    )
    r1 = api.get(f"/invoices/{invoice_id}/pdf")
    assert r1.status_code == 200, r1.text
    inv = repo.get_invoice(api.conn, invoice_id)
    assert inv is not None
    cache = invoice_pdf_cache_path(test_config.paths.pdf_dir, inv)
    assert cache.is_file()
    cache.unlink()
    assert not cache.exists()

    # El registro fiscal sigue intacto.
    row = repo.get_invoice(api.conn, invoice_id)
    assert row is not None
    assert row["cae"] == cae
    assert row["status"] == "authorized"
    assert row["pdf_render_version"] == PDF_RENDER_VERSION

    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf",
        lambda inv, items: b"%PDF-regenerado",
    )
    r2 = api.get(f"/invoices/{invoice_id}/pdf")
    assert r2.status_code == 200, r2.text
    assert r2.content == b"%PDF-regenerado"
    assert cache.read_bytes() == b"%PDF-regenerado"


def test_cache_write_es_atomico(api, arca, test_config, monkeypatch):
    """FAC-53 review: el cache se publica con os.replace, no write in-place."""
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    assert inv is not None
    cache = invoice_pdf_cache_path(test_config.paths.pdf_dir, inv)
    assert not cache.exists()

    replaces: list[tuple[str, str]] = []
    real_replace = os.replace

    def tracking_replace(src, dst, *args, **kwargs):
        replaces.append((str(src), str(dst)))
        # Mientras no haya replace, el destino final no debe existir
        # (evita hits concurrentes sobre un PDF a medias).
        assert not cache.exists()
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("facturador.pdf.render.os.replace", tracking_replace)
    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf",
        lambda _inv, _items: b"%PDF-atomico",
    )

    pdf = get_or_render_invoice_pdf(inv, [], test_config.paths.pdf_dir)

    assert pdf == b"%PDF-atomico"
    assert cache.read_bytes() == b"%PDF-atomico"
    assert len(replaces) == 1
    src, dst = replaces[0]
    assert dst == str(cache)
    assert src.endswith(".tmp")
    assert Path(src).name.startswith(f".{cache.name}.")
    # El temp no queda huérfano tras el replace exitoso.
    assert not Path(src).exists()
    assert list(test_config.paths.pdf_dir.glob(".*.tmp")) == []


def test_cache_write_fallido_no_deja_pdf_parcial(
    api, arca, test_config, monkeypatch
):
    """Si el replace falla, el path final no existe (nada que servir a medias)."""
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    assert inv is not None
    cache = invoice_pdf_cache_path(test_config.paths.pdf_dir, inv)

    def boom_replace(src, dst, *args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr("facturador.pdf.render.os.replace", boom_replace)
    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf",
        lambda _inv, _items: b"%PDF-parcial",
    )

    with pytest.raises(OSError, match="disco lleno"):
        get_or_render_invoice_pdf(inv, [], test_config.paths.pdf_dir)

    assert not cache.exists()
    assert list(test_config.paths.pdf_dir.glob(".*.tmp")) == []


def test_renders_concurrentes_del_mismo_comprobante_renderizan_una_vez(
    api, arca, test_config, monkeypatch
):
    """Dos pedidos en frío a la vez del mismo comprobante → 1 render."""
    import threading

    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    assert inv is not None

    calls = {"n": 0}
    rendering = threading.Event()
    release = threading.Event()

    def slow_render(_inv, _items):
        calls["n"] += 1
        rendering.set()
        assert release.wait(timeout=5)
        return b"%PDF-una-vez"

    monkeypatch.setattr("facturador.pdf.render.render_invoice_pdf", slow_render)

    results: list[bytes] = []

    def pedir() -> None:
        results.append(
            get_or_render_invoice_pdf(inv, [], test_config.paths.pdf_dir)
        )

    primero = threading.Thread(target=pedir)
    primero.start()
    assert rendering.wait(timeout=5)
    segundo = threading.Thread(target=pedir)
    segundo.start()
    release.set()
    primero.join(timeout=5)
    segundo.join(timeout=5)

    assert calls["n"] == 1
    assert results == [b"%PDF-una-vez", b"%PDF-una-vez"]


def test_render_fallido_libera_el_lock_y_reintenta(
    api, arca, test_config, monkeypatch
):
    """Si el render falla, el próximo pedido vuelve a intentar."""
    factura = _factura_autorizada(api)
    inv = repo.get_invoice(api.conn, factura["id"])
    assert inv is not None

    def boom(_inv, _items):
        raise RuntimeError("el render falló")

    monkeypatch.setattr("facturador.pdf.render.render_invoice_pdf", boom)
    with pytest.raises(RuntimeError, match="el render falló"):
        get_or_render_invoice_pdf(inv, [], test_config.paths.pdf_dir)

    monkeypatch.setattr(
        "facturador.pdf.render.render_invoice_pdf",
        lambda _inv, _items: b"%PDF-reintento",
    )
    pdf = get_or_render_invoice_pdf(inv, [], test_config.paths.pdf_dir)
    assert pdf == b"%PDF-reintento"


def test_cache_key_incluye_cbte_tipo(tmp_path, monkeypatch):
    """FAC-53 review: numeración ARCA es por (PV, cbte_tipo); no colisionar."""
    import sqlite3
    from typing import cast

    from facturador.pdf import invoice_pdf_filename

    factura = cast(
        sqlite3.Row,
        {
            "cbte_tipo": 19,
            "punto_venta": 1,
            "cbte_nro": 1,
            "environment": "homo",
        },
    )
    nota_credito = cast(
        sqlite3.Row,
        {
            "cbte_tipo": 21,
            "punto_venta": 1,
            "cbte_nro": 1,
            "environment": "homo",
        },
    )
    name_fe = invoice_pdf_filename(factura)
    name_nc = invoice_pdf_filename(nota_credito)
    assert name_fe == "factura-E-19-00001-00000001-homo.pdf"
    assert name_nc == "factura-E-21-00001-00000001-homo.pdf"
    assert name_fe != name_nc

    def render(inv, _items):
        return f"%PDF-tipo-{inv['cbte_tipo']}".encode()

    monkeypatch.setattr("facturador.pdf.render.render_invoice_pdf", render)
    pdf_dir = tmp_path / "pdfs"

    out_fe = get_or_render_invoice_pdf(factura, [], pdf_dir)
    out_nc = get_or_render_invoice_pdf(nota_credito, [], pdf_dir)
    assert out_fe == b"%PDF-tipo-19"
    assert out_nc == b"%PDF-tipo-21"
    assert (pdf_dir / name_fe).read_bytes() == out_fe
    assert (pdf_dir / name_nc).read_bytes() == out_nc

    def boom(*_a, **_k):
        raise AssertionError("no debió regenerar")

    monkeypatch.setattr("facturador.pdf.render.render_invoice_pdf", boom)
    assert get_or_render_invoice_pdf(nota_credito, [], pdf_dir) == b"%PDF-tipo-21"

"""Avisos de ARCA (FEXEvents) y observaciones de autorización en la UI."""

from __future__ import annotations

from facturador import repo
from facturador.arca.events import load_events, record_events
from tests.test_web import _autorizar, _crear_cliente_por_form, _generar_borrador


def _emitir(api) -> str:
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    _autorizar(api, invoice_id)
    return invoice_id


def _llamada_wsfex(api) -> None:
    api.app.state.service.wsfex.call("FEXDummy")


# --- cache del último aviso ---


def test_sin_eventos_no_hay_aviso(api, arca, test_config):
    _llamada_wsfex(api)

    assert load_events(test_config.paths.arca_events).events == []
    assert "Aviso de ARCA" not in api.get("/comprobantes").text


def test_evento_codigo_cero_es_sin_novedades(api, arca, test_config):
    arca.events = [("0", "OK")]
    _llamada_wsfex(api)

    assert load_events(test_config.paths.arca_events).events == []
    assert "Aviso de ARCA" not in api.get("/comprobantes").text


def test_evento_de_arca_se_muestra_en_toda_la_ui(api, arca):
    arca.events = [("39", "A partir del 01/12 se exigira un campo nuevo")]
    invoice_id = _emitir(api)

    for url in ("/comprobantes", f"/facturas/{invoice_id}"):
        html = api.get(url).text
        assert "Aviso de ARCA (código 39):" in html
        assert "A partir del 01/12 se exigira un campo nuevo" in html


def test_el_aviso_desaparece_cuando_arca_deja_de_mandarlo(api, arca):
    arca.events = [("39", "Aviso temporal")]
    _llamada_wsfex(api)
    assert "Aviso temporal" in api.get("/comprobantes").text

    arca.events = []
    _llamada_wsfex(api)
    assert "Aviso temporal" not in api.get("/comprobantes").text


def test_texto_del_aviso_se_escapa(api, arca):
    arca.events = [("7", "&lt;script&gt;alert(1)&lt;/script&gt;")]
    _llamada_wsfex(api)

    html = api.get("/comprobantes").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_cache_corrupto_o_ausente_no_rompe(tmp_path):
    path = tmp_path / "arca-events.json"
    assert load_events(path).events == []
    path.write_text("{no es json", encoding="utf-8")
    assert load_events(path).events == []


def test_error_al_guardar_no_propaga(tmp_path):
    bloqueo = tmp_path / "data"
    bloqueo.write_text("un archivo donde iría el directorio", encoding="utf-8")

    record_events(bloqueo / "arca-events.json", [("39", "x")])  # no levanta


# --- observaciones al autorizar ---


def test_autorizada_sin_observaciones_es_verde(api, arca):
    invoice_id = _emitir(api)

    html = api.get(f"/facturas/{invoice_id}").text
    assert 'class="panel ok"' in html
    assert "Observaciones de ARCA" not in html


def test_autorizada_con_observaciones_es_amarilla(api, arca):
    arca.authorize_obs = "El campo Incoterms se informa vacio"
    invoice_id = _emitir(api)

    html = api.get(f"/facturas/{invoice_id}").text
    assert 'class="panel aviso"' in html
    assert "autorizada con observaciones" in html
    assert (
        "Observaciones de ARCA:</strong> El campo Incoterms se informa vacio"
        in html
    )
    assert 'id="ver-pdf"' in html  # el PDF sigue disponible


def test_rechazo_sin_cae_muestra_el_motivo(api, arca):
    arca.authorize_mode = "reject_obs"
    arca.authorize_obs = "Id_impositivo invalido para el pais destino"
    invoice_id = _emitir(api)

    inv = repo.get_invoice(api.conn, invoice_id)
    assert inv["status"] == "rejected"
    assert "Id_impositivo invalido para el pais destino" in inv["last_error"]
    html = api.get(f"/facturas/{invoice_id}").text
    assert 'class="panel error"' in html
    assert "Id_impositivo invalido para el pais destino" in html

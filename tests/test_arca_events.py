"""Avisos de ARCA (FEXEvents) y observaciones de autorización en la UI."""

from __future__ import annotations

import json
import re

from facturador import repo
from facturador.arca.events import load_events, mark_read, record_events
from tests.conftest import with_csrf
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


def test_marcar_leido_que_no_se_puede_guardar_devuelve_false(tmp_path, monkeypatch):
    path = tmp_path / "arca-events.json"
    record_events(path, [("39", "x")])
    aviso_id = load_events(path).events[0].id

    def falla(*args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr("facturador.arca.events.tempfile.mkstemp", falla)
    assert not mark_read(path, aviso_id)
    assert not load_events(path).events[0].read


# --- leídos: banner, campana y desplegable ---


def _banner(html: str) -> list[str]:
    return re.findall(r'<div class="panel neutro aviso-arca"', html)


def _marcar_leido(api, aviso_id: str, referer: str = "/comprobantes"):
    return api.post(
        f"/ui/avisos-arca/{aviso_id}/leido",
        data=with_csrf(api),
        headers={"referer": f"{api.base_url}{referer}"},
        follow_redirects=False,
    )


def _id(test_config, code: str) -> str:
    avisos = load_events(test_config.paths.arca_events).events
    return next(a.id for a in avisos if a.code == code)


def test_aviso_nuevo_banner_y_contador(api, arca):
    arca.events = [("39", "Mantenimiento el sabado"), ("40", "Otro aviso")]
    _llamada_wsfex(api)

    html = api.get("/comprobantes").text
    assert len(_banner(html)) == 2
    assert '<span class="contador">2</span>' in html
    assert 'aria-label="Avisos de ARCA: 2 sin leer"' in html


def test_marcar_leido_saca_el_banner_y_queda_en_el_desplegable(
    api, arca, test_config
):
    arca.events = [("39", "Mantenimiento el sabado"), ("40", "Otro aviso")]
    _llamada_wsfex(api)

    r = _marcar_leido(api, _id(test_config, "39"), referer="/clientes?x=1")
    assert r.status_code == 303
    assert r.headers["location"] == "/clientes?x=1"

    html = api.get("/comprobantes").text
    assert len(_banner(html)) == 1
    assert "Aviso de ARCA (código 39)" not in html  # sin banner
    assert "Mantenimiento el sabado" in html  # sigue en el desplegable
    assert '<span class="contador">1</span>' in html


def test_leido_sobrevive_a_nuevas_respuestas_y_a_reiniciar(api, arca, test_config):
    arca.events = [("39", "Mantenimiento el sabado")]
    _llamada_wsfex(api)
    _marcar_leido(api, _id(test_config, "39"))

    _llamada_wsfex(api)  # ARCA lo sigue mandando

    # Lo que se lee de disco es lo que vería la app al reiniciar.
    (aviso,) = load_events(test_config.paths.arca_events).events
    assert aviso.read
    html = api.get("/comprobantes").text
    assert _banner(html) == []
    assert 'class="contador"' not in html
    assert "Leído" in html


def test_mismo_codigo_con_otro_texto_vuelve_a_no_leido(api, arca, test_config):
    arca.events = [("39", "Mantenimiento el sabado")]
    _llamada_wsfex(api)
    _marcar_leido(api, _id(test_config, "39"))

    arca.events = [("39", "Mantenimiento pasado al domingo")]
    _llamada_wsfex(api)

    html = api.get("/comprobantes").text
    assert "Aviso de ARCA (código 39):</strong> Mantenimiento pasado al domingo" in html
    assert "Mantenimiento el sabado" not in html


def test_aviso_que_arca_deja_de_mandar_vuelve_como_nuevo(api, arca, test_config):
    arca.events = [("39", "Mantenimiento el sabado")]
    _llamada_wsfex(api)
    _marcar_leido(api, _id(test_config, "39"))

    arca.events = []
    _llamada_wsfex(api)
    html = api.get("/comprobantes").text
    assert "Mantenimiento el sabado" not in html
    assert "Sin avisos de ARCA." in html

    arca.events = [("39", "Mantenimiento el sabado")]
    _llamada_wsfex(api)
    html = api.get("/comprobantes").text
    assert len(_banner(html)) == 1
    assert '<span class="contador">1</span>' in html


def test_primera_vista_se_conserva(tmp_path):
    path = tmp_path / "arca-events.json"
    record_events(path, [("39", "x")])
    primera = json.loads(path.read_text(encoding="utf-8"))["events"][0]["first_seen"]

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["events"][0]["first_seen"] = "2026-01-02T03:04:05+00:00"
    path.write_text(json.dumps(raw), encoding="utf-8")
    record_events(path, [("39", "x")])

    (aviso,) = load_events(path).events
    assert primera
    assert aviso.first_seen is not None
    assert aviso.first_seen.isoformat() == "2026-01-02T03:04:05+00:00"


def test_marcar_leido_de_aviso_que_ya_no_esta(api, arca, test_config):
    arca.events = [("39", "Mantenimiento el sabado")]
    _llamada_wsfex(api)
    aviso_id = _id(test_config, "39")
    arca.events = []
    _llamada_wsfex(api)

    r = _marcar_leido(api, aviso_id)
    assert r.status_code == 303
    assert load_events(test_config.paths.arca_events).events == []


def test_marcar_leido_sin_referer_vuelve_al_inicio(api, arca, test_config):
    arca.events = [("39", "x")]
    _llamada_wsfex(api)

    r = api.post(
        f"/ui/avisos-arca/{_id(test_config, '39')}/leido",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_marcar_leido_desde_otro_origen_se_rechaza(api, arca, test_config):
    arca.events = [("39", "x")]
    _llamada_wsfex(api)

    r = api.post(
        f"/ui/avisos-arca/{_id(test_config, '39')}/leido",
        data=with_csrf(api),
        headers={"referer": "http://evil.example/phish"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert not load_events(test_config.paths.arca_events).events[0].read


def test_marcar_leido_no_vuelve_a_otro_post(api, arca, test_config):
    arca.events = [("39", "x")]
    _llamada_wsfex(api)

    r = _marcar_leido(api, _id(test_config, "39"), referer="/ui/registry/catch-up")
    assert r.headers["location"] == "/"


def test_marcar_leido_exige_csrf(api, arca, test_config):
    arca.events = [("39", "x")]
    _llamada_wsfex(api)

    r = api.post(
        f"/ui/avisos-arca/{_id(test_config, '39')}/leido",
        data={"csrf_token": "falso"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert not load_events(test_config.paths.arca_events).events[0].read


def test_leidos_por_perfil(tmp_path):
    homo = tmp_path / "homo" / "arca-events.json"
    prod = tmp_path / "prod" / "arca-events.json"
    record_events(homo, [("39", "x")])
    record_events(prod, [("39", "x")])

    assert mark_read(homo, load_events(homo).events[0].id)

    assert load_events(homo).events[0].read
    assert not load_events(prod).events[0].read


def test_archivo_viejo_o_con_entradas_raras(tmp_path):
    path = tmp_path / "arca-events.json"
    # Formato anterior (sin first_seen ni read) y basura mezclada.
    path.write_text(
        json.dumps({
            "events": [
                {"code": "39", "msg": "x"},
                "basura",
                {"read": "si"},
                {"code": "0", "msg": "OK"},
            ],
            "seen_at": "2026-01-02T03:04:05+00:00",
        }),
        encoding="utf-8",
    )
    avisos = load_events(path).events
    assert [(a.code, a.read) for a in avisos] == [("39", False)]
    assert avisos[0].first_seen is not None  # cae en seen_at

    path.write_text("{no es json", encoding="utf-8")
    assert not mark_read(path, "0123456789abcdef")
    record_events(path, [("39", "x")])  # reescribe sobre el corrupto
    assert [a.read for a in load_events(path).events] == [False]


def test_fecha_fuera_de_rango_no_rompe_la_pagina(api, arca, test_config):
    arca.events = [("39", "x")]
    _llamada_wsfex(api)
    path = test_config.paths.arca_events
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["events"][0]["first_seen"] = "0001-01-01T00:00:00+00:00"
    raw["seen_at"] = "9999-12-31T23:59:59+00:00"
    path.write_text(json.dumps(raw), encoding="utf-8")

    r = api.get("/comprobantes")
    assert r.status_code == 200
    assert "Aviso de ARCA (código 39):" in r.text


def test_sin_avisos_la_campana_no_tiene_contador(api, arca):
    html = api.get("/comprobantes").text
    assert "Sin avisos de ARCA." in html
    assert 'class="contador"' not in html
    assert 'aria-label="Avisos de ARCA"' in html


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

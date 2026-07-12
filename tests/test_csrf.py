"""FAC-42 — CSRF en formularios del browser (double-submit cookie)."""

from __future__ import annotations

from facturador.api.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    generate_csrf_token,
    tokens_match,
)
from tests.conftest import ensure_csrf_cookie, with_csrf
from tests.test_web import CLIENTE_FORM, FACTURA_FORM, _crear_cliente_por_form


def test_tokens_match_rechaza_ausentes_y_distintos():
    a = generate_csrf_token()
    b = generate_csrf_token()
    assert tokens_match(a, a)
    assert not tokens_match(a, b)
    assert not tokens_match(a, None)
    assert not tokens_match(None, a)
    assert not tokens_match("", a)
    assert not tokens_match(a, a + "x")


def test_get_emite_cookie_csrf_sin_exponer_token_en_body_de_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    assert CSRF_COOKIE_NAME in r.cookies
    token = r.cookies[CSRF_COOKIE_NAME]
    assert len(token) >= 32
    # JSON de health no incluye el token (solo Set-Cookie).
    assert token not in r.text


def test_formulario_html_incluye_campo_csrf_alineado_a_cookie(api):
    _crear_cliente_por_form(api)
    r = api.get("/")
    assert r.status_code == 200
    token = r.cookies.get(CSRF_COOKIE_NAME) or api.cookies.get(CSRF_COOKIE_NAME)
    assert token
    assert f'name="{CSRF_FORM_FIELD}"' in r.text
    assert f'value="{token}"' in r.text


def test_post_ui_sin_token_es_403(api):
    ensure_csrf_cookie(api)
    r = api.post("/ui/clientes", data=CLIENTE_FORM, follow_redirects=False)
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]
    # Mensaje genérico: no filtra el valor del cookie.
    assert api.cookies.get(CSRF_COOKIE_NAME) not in r.text


def test_post_ui_con_token_invalido_es_403(api):
    ensure_csrf_cookie(api)
    bad = {**CLIENTE_FORM, CSRF_FORM_FIELD: "token-falso-no-coincide"}
    r = api.post("/ui/clientes", data=bad, follow_redirects=False)
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_post_ui_sin_cookie_es_403(api):
    token = generate_csrf_token()
    # Sin cookie: solo el campo del form no alcanza.
    api.cookies.clear()
    r = api.post(
        "/ui/clientes",
        data={**CLIENTE_FORM, CSRF_FORM_FIELD: token},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_post_ui_valido_con_form_field(api):
    r = api.post(
        "/ui/clientes",
        data=with_csrf(api, CLIENTE_FORM),
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_post_ui_valido_con_header_csrf(api):
    token = ensure_csrf_cookie(api)
    r = api.post(
        "/ui/clientes",
        data=CLIENTE_FORM,
        headers={CSRF_HEADER_NAME: token},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_get_y_api_json_no_exigen_csrf(api):
    api.cookies.clear()
    assert api.get("/").status_code == 200
    assert api.get("/health").status_code == 200
    # API JSON: sin form CSRF (FAC-41 ya cubre Origin; scripts locales OK).
    r = api.post(
        "/clients",
        json={
            "razon_social": "API SIN CSRF S.A.",
            "domicilio": "Calle 1",
            "pais_dst": 225,
            "cuit_pais": 55000002002,
            "id_impositivo": "RUT 99",
            "moneda_default": "DOL",
            "idioma_default": 1,
            "forma_pago_default": "WIRE",
        },
    )
    assert r.status_code == 201, r.text


def test_authorize_ui_exige_csrf(api, arca):
    _crear_cliente_por_form(api)
    draft = api.post(
        "/ui/facturas",
        data=with_csrf(api, FACTURA_FORM),
        follow_redirects=False,
    )
    invoice_id = draft.headers["location"].split("/")[2]

    missing = api.post(
        f"/ui/facturas/{invoice_id}/authorize",
        follow_redirects=False,
    )
    assert missing.status_code == 403

    ok = api.post(
        f"/ui/facturas/{invoice_id}/authorize",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert arca.calls["FEXAuthorize"] == 1


def test_respuesta_csrf_no_filtra_material_del_token(api):
    token = ensure_csrf_cookie(api)
    submitted = "valor-falso-que-no-debe-ecoarse"
    r = api.post(
        "/ui/clientes",
        data={**CLIENTE_FORM, CSRF_FORM_FIELD: submitted},
        follow_redirects=False,
    )
    assert r.status_code == 403
    body = r.text
    assert token not in body
    assert submitted not in body
    assert r.json()["detail"] == "CSRF token ausente o inválido"

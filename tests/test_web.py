"""Fase 6a — Frontend Jinja2 + HTMX (spike.md §2.4).

Cubre: caso feliz semanal en un click (CAE + link al PDF en el partial),
errores de dominio legibles en HTML, flujo force_desync, escaping de datos
hostiles y alta/edición de clientes vía form.
"""

CLIENTE_FORM = {
    "razon_social": "CLIENTE URUGUAY S.A.",
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": "225",
    "cuit_pais": "55000002002",
    "id_impositivo": "RUT 219999830019",
    "moneda_default": "DOL",
    "idioma_default": "1",
    "forma_pago_default": "WIRE TRANSFER",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": "true",
}


def _crear_cliente_por_form(api, **overrides):
    r = api.post(
        "/ui/clientes", data={**CLIENTE_FORM, **overrides}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    return r


FACTURA_FORM = {
    "imp_total": "1500.00",
    "fecha_pago": "2026-07-05",
    "descripcion": "Servicios de desarrollo de software",
    "obs": "",
    "client_id": "",
}


# --- home ---


def test_home_sin_clientes_invita_a_crear_uno(api):
    r = api.get("/")
    assert r.status_code == 200
    assert "No hay clientes cargados" in r.text


def test_home_precarga_cliente_default_y_cotizacion(api, arca):
    _crear_cliente_por_form(api)
    r = api.get("/")
    assert r.status_code == 200
    assert "CLIENTE URUGUAY S.A." in r.text
    assert 'value="Servicios de desarrollo de software"' in r.text
    assert arca.ctz in r.text                 # cotización ARCA del día, informativa
    assert "htmx.min.js" in r.text


def test_static_htmx_se_sirve_local(api):
    r = api.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:200]


# --- caso feliz: un click ---


def test_emitir_y_autorizar_en_un_click(api, arca):
    _crear_cliente_por_form(api)
    r = api.post("/ui/facturas", data=FACTURA_FORM)
    assert r.status_code == 200, r.text
    assert "autorizada" in r.text
    assert "76100000000001" in r.text         # CAE en el partial
    assert "/pdf" in r.text                   # link de descarga
    assert r.headers["hx-trigger"] == "facturas-changed"
    # fecha_pago del <input type=date> convertida a AAAAMMDD
    assert arca.issued[(19, 1, 1)]["arca_id"] == 1

    listado = api.get("/ui/listado")
    assert "00001-00000001" in listado.text
    assert "Autorizada" in listado.text


def test_error_de_dominio_se_muestra_legible(api, arca):
    # Sin clientes: create_invoice es ConflictError => panel de error, no JSON.
    r = api.post("/ui/facturas", data=FACTURA_FORM)
    assert r.status_code == 200
    assert "No se pudo autorizar" in r.text
    assert "cliente default" in r.text


def test_monto_invalido_es_error_de_validacion_legible(api, arca):
    _crear_cliente_por_form(api)
    r = api.post("/ui/facturas", data={**FACTURA_FORM, "imp_total": "-5"})
    assert "Datos inválidos" in r.text


def test_rechazo_de_arca_se_muestra_con_el_motivo(api, arca):
    _crear_cliente_por_form(api)
    arca.authorize_mode = "reject"
    r = api.post("/ui/facturas", data=FACTURA_FORM)
    assert "Rechazada" in r.text
    assert "1068" in r.text


# --- DB desactualizada: el 409 forzable (§2.5) ---


def test_db_desactualizada_ofrece_boton_de_force(api, arca):
    _crear_cliente_por_form(api)
    arca.last_cmp[(1, 19)] = 5
    r = api.post("/ui/facturas", data=FACTURA_FORM)
    assert "desactualizado" in r.text
    assert "force=true" in r.text             # botón para forzar a conciencia

    # El botón reintenta el authorize del draft ya creado, con force.
    import re

    match = re.search(r"/ui/facturas/([0-9a-f]+)/authorize\?force=true", r.text)
    assert match
    retry = api.post(f"/ui/facturas/{match.group(1)}/authorize?force=true")
    assert "autorizada" in retry.text


# --- reintento desde el listado ---


def test_unknown_muestra_reintento_en_listado(api, arca):
    _crear_cliente_por_form(api)
    arca.authorize_mode = "timeout"
    r = api.post("/ui/facturas", data=FACTURA_FORM)
    assert "A reconciliar" in r.text

    listado = api.get("/ui/listado")
    assert "Autorizar" in listado.text        # botón de reintento


# --- escaping (checklist §2.1.1 punto 4, aplicado al HTML) ---


def test_datos_hostiles_quedan_escapados_en_el_frontend(api, arca):
    _crear_cliente_por_form(
        api, razon_social='PYME <script>alert(1)</script> & "CO"'
    )
    home = api.get("/")
    assert "<script>alert(1)</script>" not in home.text
    assert "&lt;script&gt;" in home.text

    api.post("/ui/facturas", data=FACTURA_FORM)
    listado = api.get("/ui/listado")
    assert "<script>alert(1)</script>" not in listado.text


# --- clientes ---


def test_alta_y_edicion_de_cliente_por_form(api, arca):
    _crear_cliente_por_form(api)
    pagina = api.get("/clientes")
    assert "CLIENTE URUGUAY S.A." in pagina.text
    assert "★" in pagina.text                 # marcado como default
    assert "URUGUAY" in pagina.text           # select poblado del cache params

    cliente_id = api.get("/clients").json()[0]["id"]
    edit = api.get(f"/clientes?edit={cliente_id}")
    assert 'value="CLIENTE URUGUAY S.A."' in edit.text

    editado = {**CLIENTE_FORM, "client_id": cliente_id}
    editado["razon_social"] = "RENOMBRADO S.A."
    r = api.post("/ui/clientes", data=editado, follow_redirects=False)
    assert r.status_code == 303
    assert api.get("/clients").json()[0]["razon_social"] == "RENOMBRADO S.A."


def test_alta_de_cliente_invalida_no_pierde_la_pagina(api, arca):
    r = api.post("/ui/clientes", data={**CLIENTE_FORM, "pais_dst": "999"})
    assert r.status_code == 422
    assert "pais_dst" in r.text               # error legible en la misma página

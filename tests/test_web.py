"""Frontend Jinja2 + HTMX (spike.md §2.4), flujo en dos fases.

Generar crea SOLO el borrador y muestra la revisión; nada viaja a ARCA
hasta el Confirmar explícito. Un borrador equivocado se descarta sin
consecuencia impositiva. Cubre además: errores de dominio legibles,
force_desync, escaping de datos hostiles y alta/edición de clientes.
"""

import re

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

FACTURA_FORM = {
    "imp_total": "1500.00",
    "fecha_pago": "2026-07-05",
    "descripcion": "Servicios de desarrollo de software",
    "obs": "",
    "client_id": "",
}


def _crear_cliente_por_form(api, **overrides):
    r = api.post(
        "/ui/clientes", data={**CLIENTE_FORM, **overrides}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    return r


def _generar_borrador(api, **overrides):
    """POST del form → partial de revisión. Devuelve (respuesta, invoice_id)."""
    r = api.post("/ui/facturas", data={**FACTURA_FORM, **overrides})
    assert r.status_code == 200, r.text
    match = re.search(r"/ui/facturas/([0-9a-f]+)/authorize", r.text)
    return r, (match.group(1) if match else None)


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
    assert "Monto (USD)" in r.text            # DOL se muestra como USD
    assert "htmx.min.js" in r.text


def test_static_htmx_se_sirve_local(api):
    r = api.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:200]


# --- flujo en dos fases: generar → revisar → confirmar ---


def test_generar_crea_solo_el_borrador_y_muestra_revision(api, arca):
    _crear_cliente_por_form(api)
    r, invoice_id = _generar_borrador(api)

    assert invoice_id is not None
    assert "Revisar antes de enviar" in r.text
    assert "CLIENTE URUGUAY S.A." in r.text
    assert "USD" in r.text                    # moneda legible en la revisión
    assert "1500.00" in r.text
    assert "Confirmar y autorizar" in r.text
    assert "Descartar borrador" in r.text
    assert arca.calls["FEXAuthorize"] == 0    # NADA viajó a ARCA todavía

    factura = api.get(f"/invoices/{invoice_id}").json()
    assert factura["status"] == "draft"


def test_confirmar_autoriza_y_devuelve_cae(api, arca):
    _crear_cliente_por_form(api)
    _, invoice_id = _generar_borrador(api)

    r = api.post(f"/ui/facturas/{invoice_id}/authorize")
    assert "autorizada" in r.text
    assert "76100000000001" in r.text         # CAE en el partial
    assert "/pdf" in r.text
    assert r.headers["hx-trigger"] == "facturas-changed"
    assert arca.calls["FEXAuthorize"] == 1

    listado = api.get("/ui/listado")
    assert "00001-00000001" in listado.text
    assert "Autorizada" in listado.text


def test_borrador_se_reabre_para_revisar_desde_el_listado(api, arca):
    _crear_cliente_por_form(api)
    _, invoice_id = _generar_borrador(api)

    listado = api.get("/ui/listado")
    assert "Revisar" in listado.text          # botón del borrador

    r = api.get(f"/ui/facturas/{invoice_id}/confirmar")
    assert "Revisar antes de enviar" in r.text
    assert "Confirmar y autorizar" in r.text


def test_descartar_borrador_no_deja_rastro_ni_toca_arca(api, arca):
    _crear_cliente_por_form(api)
    _, invoice_id = _generar_borrador(api)

    r = api.post(f"/ui/facturas/{invoice_id}/descartar")
    assert "descartado" in r.text
    assert api.get(f"/invoices/{invoice_id}").status_code == 404
    assert api.get("/invoices").json() == []
    assert arca.calls["FEXAuthorize"] == 0


def test_factura_enviada_no_se_puede_descartar(api, arca):
    _crear_cliente_por_form(api)
    _, invoice_id = _generar_borrador(api)
    api.post(f"/ui/facturas/{invoice_id}/authorize")

    r = api.post(f"/ui/facturas/{invoice_id}/descartar")
    assert "No se pudo" in r.text or "Solo se pueden descartar" in r.text
    assert api.get(f"/invoices/{invoice_id}").status_code == 200  # sigue ahí


# --- errores de dominio legibles ---


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
    _, invoice_id = _generar_borrador(api)
    arca.authorize_mode = "reject"
    r = api.post(f"/ui/facturas/{invoice_id}/authorize")
    assert "Rechazada" in r.text
    assert "1068" in r.text


# --- DB desactualizada: el 409 forzable (§2.5) ---


def test_db_desactualizada_ofrece_boton_de_force(api, arca):
    _crear_cliente_por_form(api)
    _, invoice_id = _generar_borrador(api)
    arca.last_cmp[(1, 19)] = 5
    r = api.post(f"/ui/facturas/{invoice_id}/authorize")
    assert "desactualizado" in r.text
    assert "force=true" in r.text             # botón para forzar a conciencia

    retry = api.post(f"/ui/facturas/{invoice_id}/authorize?force=true")
    assert "autorizada" in retry.text


# --- reintento desde el listado ---


def test_unknown_muestra_reintento_en_listado(api, arca):
    _crear_cliente_por_form(api)
    _, invoice_id = _generar_borrador(api)
    arca.authorize_mode = "timeout"
    r = api.post(f"/ui/facturas/{invoice_id}/authorize")
    assert "A reconciliar" in r.text

    listado = api.get("/ui/listado")
    assert "Reintentar" in listado.text       # reproceso idempotente, sin revisión


# --- página de comprobantes: tabs por estado ---


def test_comprobantes_filtra_por_tab(api, arca):
    _crear_cliente_por_form(api)
    _, borrador_id = _generar_borrador(api)
    _, autorizada_id = _generar_borrador(api, imp_total="900.00")
    api.post(f"/ui/facturas/{autorizada_id}/authorize")

    todas = api.get("/comprobantes")
    assert todas.status_code == 200
    assert "Borradores" in todas.text and "Autorizadas" in todas.text
    assert "Borrador" in todas.text and "Autorizada" in todas.text

    borradores = api.get("/comprobantes?estado=borradores")
    assert "Revisar" in borradores.text
    assert "76100000000001" not in borradores.text   # la autorizada no aparece

    autorizadas = api.get("/comprobantes?estado=autorizadas")
    assert "76100000000001" in autorizadas.text
    assert "Revisar" not in autorizadas.text

    assert api.get("/comprobantes?estado=inventado").status_code == 200  # cae en todas


def test_home_es_solo_emision_y_linkea_al_registro(api, arca):
    _crear_cliente_por_form(api)
    r = api.get("/")
    assert "Generar borrador" in r.text
    assert 'href="/comprobantes"' in r.text
    assert "<table" not in r.text            # el listado ya no vive acá


# --- escaping (checklist §2.1.1 punto 4, aplicado al HTML) ---


def test_datos_hostiles_quedan_escapados_en_el_frontend(api, arca):
    _crear_cliente_por_form(
        api, razon_social='PYME <script>alert(1)</script> & "CO"'
    )
    home = api.get("/")
    assert "<script>alert(1)</script>" not in home.text
    assert "&lt;script&gt;" in home.text

    r, _ = _generar_borrador(api)
    assert "<script>alert(1)</script>" not in r.text   # revisión escapada

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

"""Frontend en páginas separadas (patrón Post/Redirect/Get).

Emitir (único lugar con inputs) → POST crea el borrador → página de
revisión read-only (QUÉ se va a enviar) → Confirmar → página de detalle
read-only (qué se envió, estado, CAE, PDF). Nada viaja a ARCA sin pasar
por la revisión; descartar un borrador no tiene efecto impositivo.
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
    """POST del form → 303 a la página de revisión. Devuelve invoice_id."""
    r = api.post(
        "/ui/facturas", data={**FACTURA_FORM, **overrides}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.endswith("/revisar")
    return location.split("/")[2]


def _autorizar(api, invoice_id, query=""):
    r = api.post(
        f"/ui/facturas/{invoice_id}/authorize{query}", follow_redirects=False
    )
    return r


# --- home (Emitir) ---


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


def test_home_es_solo_emision_sin_listado(api, arca):
    _crear_cliente_por_form(api)
    r = api.get("/")
    assert "Generar borrador" in r.text
    assert 'href="/comprobantes"' in r.text
    assert "<table" not in r.text             # el registro no vive acá


def test_static_htmx_se_sirve_local(api):
    r = api.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:200]


# --- generar → revisar (página read-only) ---


def test_generar_redirige_a_revision_sin_tocar_arca(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    assert arca.calls["FEXAuthorize"] == 0    # NADA viajó a ARCA todavía

    r = api.get(f"/facturas/{invoice_id}/revisar")
    assert r.status_code == 200
    assert "Revisar antes de enviar" in r.text
    assert "CLIENTE URUGUAY S.A." in r.text
    assert "USD" in r.text
    assert "1500.00" in r.text
    assert "Confirmar y autorizar" in r.text
    assert "Descartar borrador" in r.text
    # Página read-only: ningún campo editable (solo botones de acción).
    assert 'name="imp_total"' not in r.text
    assert "<input" not in r.text


def test_revisar_de_factura_no_borrador_redirige_al_detalle(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    _autorizar(api, invoice_id)
    r = api.get(f"/facturas/{invoice_id}/revisar", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/facturas/{invoice_id}"


# --- confirmar → detalle (página read-only) ---


def test_confirmar_autoriza_y_muestra_detalle(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)

    r = _autorizar(api, invoice_id)
    assert r.status_code == 303
    assert r.headers["location"] == f"/facturas/{invoice_id}"
    assert arca.calls["FEXAuthorize"] == 1

    detalle = api.get(f"/facturas/{invoice_id}")
    assert "autorizada" in detalle.text
    assert "76100000000001" in detalle.text   # CAE
    assert "/pdf" in detalle.text
    assert "Lo enviado a ARCA" in detalle.text
    assert "<input" not in detalle.text       # read-only: sin inputs
    assert "Generar" not in detalle.text      # sin form de nueva factura


def test_detalle_de_borrador_redirige_a_revision(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    r = api.get(f"/facturas/{invoice_id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/revisar")


# --- descartar ---


def test_descartar_borrador_vuelve_al_form_sin_tocar_arca(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)

    r = api.post(f"/ui/facturas/{invoice_id}/descartar", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?aviso=descartado"
    assert "descartado" in api.get("/?aviso=descartado").text
    assert api.get(f"/invoices/{invoice_id}").status_code == 404
    assert arca.calls["FEXAuthorize"] == 0


def test_factura_enviada_no_se_puede_descartar(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    _autorizar(api, invoice_id)

    r = api.post(f"/ui/facturas/{invoice_id}/descartar", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/facturas/{invoice_id}"  # al detalle
    assert api.get(f"/invoices/{invoice_id}").status_code == 200  # sigue ahí


# --- errores de dominio legibles ---


def test_error_de_dominio_se_muestra_en_el_form(api, arca):
    # Sin clientes: create_invoice es ConflictError => error en la misma página.
    r = api.post("/ui/facturas", data=FACTURA_FORM)
    assert r.status_code == 422
    assert "cliente default" in r.text


def test_monto_invalido_es_error_de_validacion_legible(api, arca):
    _crear_cliente_por_form(api)
    r = api.post("/ui/facturas", data={**FACTURA_FORM, "imp_total": "-5"})
    assert r.status_code == 422
    assert "Datos inválidos" in r.text


def test_rechazo_de_arca_se_ve_en_el_detalle(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    arca.authorize_mode = "reject"
    _autorizar(api, invoice_id)
    detalle = api.get(f"/facturas/{invoice_id}")
    assert "Rechazada" in detalle.text
    assert "1068" in detalle.text


# --- DB desactualizada: el 409 forzable (§2.5) ---


def test_db_desactualizada_reabre_revision_con_force(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    arca.last_cmp[(1, 19)] = 5
    r = api.post(f"/ui/facturas/{invoice_id}/authorize")
    assert r.status_code == 409               # re-render de la revisión
    assert "desactualizado" in r.text
    assert "force=true" in r.text             # botón para forzar a conciencia

    retry = _autorizar(api, invoice_id, query="?force=true")
    assert retry.status_code == 303
    assert "autorizada" in api.get(f"/facturas/{invoice_id}").text


# --- unknown: reintento desde el detalle ---


def test_unknown_ofrece_reintento_en_el_detalle(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    arca.authorize_mode = "timeout"
    _autorizar(api, invoice_id)

    detalle = api.get(f"/facturas/{invoice_id}")
    assert "A reconciliar" in detalle.text
    assert "Reintentar" in detalle.text       # reproceso idempotente

    listado = api.get("/comprobantes?estado=atencion")
    assert "Ver detalle" in listado.text

    arca.authorize_mode = "ok"
    _autorizar(api, invoice_id)
    assert "autorizada" in api.get(f"/facturas/{invoice_id}").text


# --- página de comprobantes: tabs por estado ---


def test_comprobantes_filtra_por_tab(api, arca):
    _crear_cliente_por_form(api)
    _generar_borrador(api)
    autorizada_id = _generar_borrador(api, imp_total="900.00")
    _autorizar(api, autorizada_id)

    todas = api.get("/comprobantes")
    assert todas.status_code == 200
    assert "Borradores" in todas.text and "Autorizadas" in todas.text
    assert "Borrador" in todas.text and "Autorizada" in todas.text

    borradores = api.get("/comprobantes?estado=borradores")
    assert "Revisar" in borradores.text
    assert "76100000000001" not in borradores.text   # la autorizada no aparece

    autorizadas = api.get("/comprobantes?estado=autorizadas")
    assert "76100000000001" in autorizadas.text
    assert f"/facturas/{autorizada_id}" in autorizadas.text  # número → detalle
    assert "Revisar" not in autorizadas.text

    assert api.get("/comprobantes?estado=inventado").status_code == 200  # cae en todas


# --- escaping (checklist §2.1.1 punto 4, aplicado al HTML) ---


def test_datos_hostiles_quedan_escapados_en_el_frontend(api, arca):
    _crear_cliente_por_form(
        api, razon_social='PYME <script>alert(1)</script> & "CO"'
    )
    home = api.get("/")
    assert "<script>alert(1)</script>" not in home.text
    assert "&lt;script&gt;" in home.text

    invoice_id = _generar_borrador(api)
    revision = api.get(f"/facturas/{invoice_id}/revisar")
    assert "<script>alert(1)</script>" not in revision.text

    listado = api.get("/comprobantes")
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

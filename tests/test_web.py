"""Frontend en páginas separadas (patrón Post/Redirect/Get).

Emitir (único lugar con inputs) → POST crea el borrador → página de
revisión read-only (QUÉ se va a enviar) → Confirmar → página de detalle
read-only (qué se envió, estado, CAE, PDF). Nada viaja a ARCA sin pasar
por la revisión; descartar un borrador no tiene efecto impositivo.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from facturador import db
from facturador.api import create_app
from facturador.api.localhost_policy import loopback_base_url
from facturador.arca.wsfex import WsfexClient
from facturador.config import Config
from facturador.constants import ArcaEnvironment
from facturador.profile import EnvironmentProfile
from facturador.settings import get_active_emisor_id
from tests.arca_fake import FakeWsaa
from tests.conftest import install_test_cert_pair, seed_params, seed_settings, with_csrf

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
        "/ui/clientes",
        data=with_csrf(api, {**CLIENTE_FORM, **overrides}),
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r


def _generar_borrador(api, **overrides):
    """POST del form → 303 a la página de revisión. Devuelve invoice_id."""
    r = api.post(
        "/ui/facturas",
        data=with_csrf(api, {**FACTURA_FORM, **overrides}),
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    assert location.endswith("/revisar")
    return location.split("/")[2]


def _autorizar(api, invoice_id, query=""):
    r = api.post(
        f"/ui/facturas/{invoice_id}/authorize{query}",
        data=with_csrf(api),
        follow_redirects=False,
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
    # Página read-only: ningún campo editable de factura; solo CSRF (FAC-42).
    assert 'name="imp_total"' not in r.text
    assert 'name="csrf_token"' in r.text
    assert r.text.count("<input") == r.text.count('name="csrf_token"')


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

    r = api.post(
        f"/ui/facturas/{invoice_id}/descartar",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?aviso=descartado"
    assert "descartado" in api.get("/?aviso=descartado").text
    assert api.get(f"/invoices/{invoice_id}").status_code == 404
    assert arca.calls["FEXAuthorize"] == 0


def test_factura_enviada_no_se_puede_descartar(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    _autorizar(api, invoice_id)

    r = api.post(
        f"/ui/facturas/{invoice_id}/descartar",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/facturas/{invoice_id}"  # al detalle
    assert api.get(f"/invoices/{invoice_id}").status_code == 200  # sigue ahí


# --- errores de dominio legibles ---


def test_error_de_dominio_se_muestra_en_el_form(api, arca):
    # Sin clientes: create_invoice es ConflictError => error en la misma página.
    r = api.post("/ui/facturas", data=with_csrf(api, FACTURA_FORM))
    assert r.status_code == 422
    assert "cliente default" in r.text


def test_monto_invalido_es_error_de_validacion_legible(api, arca):
    _crear_cliente_por_form(api)
    r = api.post(
        "/ui/facturas", data=with_csrf(api, {**FACTURA_FORM, "imp_total": "-5"})
    )
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
    r = api.post(
        f"/ui/facturas/{invoice_id}/authorize",
        data=with_csrf(api),
    )
    assert r.status_code == 409               # re-render de la revisión
    assert "desactualizado" in r.text
    assert "force=true" in r.text             # botón para forzar a conciencia

    retry = _autorizar(api, invoice_id, query="?force=true")
    assert retry.status_code == 303
    assert "autorizada" in api.get(f"/facturas/{invoice_id}").text


def test_ui_catch_up_sin_invoice_id_muestra_error_en_listado(api, monkeypatch):
    """Ítem 6 review: POST catch-up sin invoice_id → error visible en /comprobantes."""
    from facturador.service import ConflictError, InvoiceService

    def boom(self):
        raise ConflictError("fallo de prueba catch-up")

    monkeypatch.setattr(InvoiceService, "catch_up_from_arca", boom)

    r = api.post(
        "/ui/registry/catch-up",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/comprobantes?error=")
    page = api.get(location)
    assert page.status_code == 200
    assert "No se pudo sincronizar" in page.text
    assert "fallo de prueba catch-up" in page.text
    assert "panel error" in page.text


def test_ui_catch_up_sin_invoice_id_muestra_aviso_en_listado(api, arca):
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=701)
    arca.last_cmp[(1, 19)] = 1

    r = api.post(
        "/ui/registry/catch-up",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/comprobantes?aviso=catchup")
    page = api.get(location)
    assert page.status_code == 200
    assert "Registro sincronizado desde ARCA: 1 comprobantes." in page.text
    assert "panel ok" in page.text


def test_ui_catch_up_con_invoice_id_muestra_aviso_en_revisar(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    arca.seed_issued(tipo=19, pv=1, nro=1, arca_id=702)
    arca.last_cmp[(1, 19)] = 1

    r = api.post(
        "/ui/registry/catch-up",
        data=with_csrf(api, {"invoice_id": invoice_id}),
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith(f"/facturas/{invoice_id}/revisar?aviso=catchup")
    page = api.get(location)
    assert page.status_code == 200
    assert "Registro sincronizado desde ARCA: 1 comprobantes." in page.text
    assert "panel ok" in page.text


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
    r = api.post(
        "/ui/clientes", data=with_csrf(api, editado), follow_redirects=False
    )
    assert r.status_code == 303
    assert api.get("/clients").json()[0]["razon_social"] == "RENOMBRADO S.A."


def test_alta_de_cliente_invalida_no_pierde_la_pagina(api, arca):
    r = api.post(
        "/ui/clientes",
        data=with_csrf(api, {**CLIENTE_FORM, "pais_dst": "999"}),
    )
    assert r.status_code == 422
    assert "pais_dst" in r.text               # error legible en la misma página


# --- configuración (settings de dominio: viven en la DB) ---

EMISOR_FORM = {
    "razon_social": "MI EMPRESA S.R.L.",
    "domicilio": "Calle Falsa 123, CABA",
    "iibb": "901-123456-7",
    "inicio_actividades": "01/2020",
    "condicion_iva": "IVA Responsable Inscripto",
    "puntos_venta": "1",
}


def _guardar_emisor(api, **overrides):
    return api.post(
        "/ui/emisores",
        data=with_csrf(api, {**EMISOR_FORM, **overrides}),
        follow_redirects=False,
    )


def _editar_emisor_activo(api, **overrides):
    emisor_id = api.conn.execute("SELECT id FROM emisores LIMIT 1").fetchone()["id"]
    data = dict(EMISOR_FORM)
    data["emisor_id"] = emisor_id
    return api.post(
        "/ui/emisores",
        data=with_csrf(api, {**data, **overrides}),
        follow_redirects=False,
    )


def _sin_settings(api):
    """Estado de primer arranque: sin emisores y la tabla settings vacía."""
    with api.conn:
        api.conn.execute("DELETE FROM emisores")
        api.conn.execute("DELETE FROM settings")


def test_primer_arranque_bloquea_emision_hasta_ready(api, arca):
    """FAC-35: sin emisor el perfil no está ready; factura/ARCA → 503.
    Configuración sigue disponible para completar el setup (FAC-38)."""
    _sin_settings(api)

    home = api.get("/")
    assert home.status_code == 503
    assert home.json()["setup_state"] == "emisor_required"

    setup = api.get("/setup/status")
    assert setup.status_code == 200
    assert setup.json()["ready"] is False

    r = api.post("/ui/facturas", data=with_csrf(api, FACTURA_FORM))
    assert r.status_code == 503
    assert r.json()["setup_state"] == "emisor_required"

    assert api.get("/configuracion").status_code == 200


def test_pagina_de_configuracion_carga_y_guarda(api, arca):
    _sin_settings(api)
    pagina = api.get("/configuracion")
    assert pagina.status_code == 200
    assert "Dar de alta al menos un emisor" in pagina.text

    r = _guardar_emisor(api)
    assert r.status_code == 303

    guardada = api.get("/configuracion?aviso=guardado")
    assert "Emisor guardado" in guardada.text
    assert "MI EMPRESA S.R.L." in guardada.text

    # Con el emisor completo, el form de emisión vuelve a estar disponible.
    _crear_cliente_por_form(api)
    assert "Generar borrador" in api.get("/").text


def test_configuracion_invalida_no_pierde_la_pagina(api, arca):
    r = _guardar_emisor(api, razon_social="")
    assert r.status_code == 422
    assert "Datos inválidos" in r.text


def test_configuracion_con_solo_espacios_es_invalida(api, arca):
    # El strip corre antes de validar: "   " no debe guardarse como vacío
    # con un "guardado" que deja al emisor incompleto.
    r = _guardar_emisor(api, razon_social="   ")
    assert r.status_code == 422
    assert "Datos inválidos" in r.text
    # El emisor sembrado sigue intacto: el intento fallido no pisó nada.
    assert "MI EMPRESA S.R.L." in api.get("/configuracion").text


def test_punto_venta_configurado_se_usa_al_emitir(api, arca):
    # Con un solo PV habilitado no hace falta elegirlo en el form.
    r = _editar_emisor_activo(api, puntos_venta="7")
    assert r.status_code == 303
    _crear_cliente_por_form(api)
    invoice_id = _generar_borrador(api)
    factura = api.get(f"/invoices/{invoice_id}").json()
    assert factura["punto_venta"] == 7


def test_puntos_venta_no_numericos_rechazados(api, arca):
    r = _editar_emisor_activo(api, puntos_venta="1, dos")
    assert r.status_code == 422
    assert "Datos inválidos" in r.text


def test_nav_incluye_configuracion(api):
    assert 'href="/configuracion"' in api.get("/").text


EMISOR_FORM_SEGUNDO = {
    "razon_social": "OTRO EMISOR S.A.",
    "domicilio": "Av. Corrientes 1000",
    "iibb": "Exento",
    "inicio_actividades": "01/01/2019",
    "condicion_iva": "IVA Responsable Inscripto",
    "puntos_venta": "5, 9",
}


def test_alta_de_segundo_emisor_y_activacion(api, arca):
    _crear_cliente_por_form(api)
    r = api.post(
        "/ui/emisores",
        data=with_csrf(api, EMISOR_FORM_SEGUNDO),
        follow_redirects=False,
    )
    assert r.status_code == 303
    pagina = api.get("/configuracion")
    assert "OTRO EMISOR S.A." in pagina.text
    assert "5, 9" in pagina.text

    emisores = api.conn.execute(
        "SELECT id, razon_social FROM emisores WHERE razon_social = ?",
        ("OTRO EMISOR S.A.",),
    ).fetchone()
    nuevo_id = emisores["id"]

    r = api.post(
        f"/ui/emisores/{nuevo_id}/activar",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "★" in api.get("/configuracion").text

    invoice_id = _generar_borrador(api, punto_venta="5")
    factura = api.get(f"/invoices/{invoice_id}").json()
    assert factura["punto_venta"] == 5  # PV elegido al emitir


def test_alta_de_emisor_rechaza_ambiente_obsoleto(api, arca):
    r = api.post(
        "/ui/emisores",
        data=with_csrf(api, {**EMISOR_FORM_SEGUNDO, "ambiente": "prod"}),
        follow_redirects=False,
    )
    assert r.status_code == 422
    assert "ambiente sale del perfil activo" in r.text
    assert (
        api.conn.execute(
            "SELECT COUNT(*) FROM emisores WHERE razon_social = ?",
            ("OTRO EMISOR S.A.",),
        ).fetchone()[0]
        == 0
    )


def test_edicion_de_emisor_rechaza_ambiente_obsoleto(api, arca):
    r = _editar_emisor_activo(api, ambiente="prod")
    assert r.status_code == 422
    assert "ambiente sale del perfil activo" in r.text


def test_no_se_puede_activar_emisor_inactivo_de_otro_perfil(api, arca):
    """FAC-27: un emisor legacy de otro ambiente puede seguir listado, pero
    no debe poder activarse ni mostrar el botón en la UI."""
    api.post(
        "/ui/emisores",
        data=with_csrf(api, EMISOR_FORM_SEGUNDO),
        follow_redirects=False,
    )
    otro = api.conn.execute(
        "SELECT id FROM emisores WHERE razon_social = ?",
        ("OTRO EMISOR S.A.",),
    ).fetchone()
    with api.conn:
        api.conn.execute(
            "UPDATE emisores SET ambiente = 'prod' WHERE id = ?",
            (otro["id"],),
        )

    pagina = api.get("/configuracion")
    assert pagina.status_code == 200
    assert f"/ui/emisores/{otro['id']}/activar" not in pagina.text

    activo_antes = api.conn.execute(
        "SELECT value FROM settings WHERE key = 'active_emisor_id'"
    ).fetchone()[0]
    r = api.post(
        f"/ui/emisores/{otro['id']}/activar",
        data=with_csrf(api),
        follow_redirects=False,
    )
    assert r.status_code == 303
    activo_despues = api.conn.execute(
        "SELECT value FROM settings WHERE key = 'active_emisor_id'"
    ).fetchone()[0]
    assert activo_despues == activo_antes


def test_emisor_activo_de_otro_perfil_bloquea_los_settings(api, arca):
    """FAC-26: con el emisor activo sellado con OTRO ambiente (DB ajena
    restaurada en el perfil equivocado), las páginas que cargan settings
    quedan bloqueadas con el diagnóstico de restore, igual que las
    facturas ajenas."""
    with api.conn:
        api.conn.execute("UPDATE emisores SET ambiente = 'prod'")
    r = api.get("/configuracion")
    assert r.status_code == 409
    assert "perfil" in r.json()["detail"]


def test_seleccion_punto_venta_al_emitir(api, arca):
    _editar_emisor_activo(api, puntos_venta="7, 3")
    _crear_cliente_por_form(api)
    home = api.get("/")
    assert 'name="punto_venta"' in home.text
    assert 'value="7"' in home.text
    assert 'value="3"' in home.text

    invoice_id = _generar_borrador(api, punto_venta="3")
    factura = api.get(f"/invoices/{invoice_id}").json()
    assert factura["punto_venta"] == 3


# --- identidad de ambiente persistente (FAC-31 / ADR 0001) ---

_PAGINAS_NORMALES = ("/", "/comprobantes", "/clientes", "/configuracion")


def _api_para_ambiente(environment, tmp_path, test_cert_and_key, arca):
    """App de un solo ambiente con perfil temporal (mismo wiring que conftest)."""
    profile = EnvironmentProfile.for_testing(
        environment, tmp_path / f"profile-{environment.value}"
    )
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)
    config = Config(env=environment, paths=profile.paths)
    conn = db.connect(profile.paths.db)
    seed_params(conn)
    seed_settings(conn, ambiente=environment.value)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(profile, config=config, conn=conn, wsfex=wsfex)
    return TestClient(app, base_url=loopback_base_url()), profile


@pytest.mark.parametrize(
    ("environment", "label", "badge_extra"),
    [
        (ArcaEnvironment.HOMO, "Homologación", "sin valor fiscal"),
        (ArcaEnvironment.PROD, "Producción", "validez fiscal"),
    ],
)
def test_paginas_muestran_identidad_de_ambiente(
    environment, label, badge_extra, tmp_path, test_cert_and_key, arca
):
    """Badge + título en lenguaje de negocio; sin paths ni secretos."""
    client, profile = _api_para_ambiente(
        environment, tmp_path, test_cert_and_key, arca
    )
    titulo = f"facturador — {label}"
    for path in _PAGINAS_NORMALES:
        r = client.get(path)
        assert r.status_code == 200, path
        assert f"<title>{titulo}</title>" in r.text
        assert f'data-env="{environment.value}"' in r.text
        assert f'class="badge-env {environment.value}"' in r.text
        assert label in r.text
        assert badge_extra in r.text
        if environment is ArcaEnvironment.PROD:
            assert "sin valor fiscal" not in r.text
        else:
            assert "validez fiscal" not in r.text
        # Diagnóstico seguro: nada de raíces de perfil ni certificados.
        assert str(profile.paths.root) not in r.text
        assert "cert.crt" not in r.text
        assert "cert.key" not in r.text


def test_produccion_muestra_avisos_de_seguridad_fiscal(
    tmp_path, test_cert_and_key, arca
):
    """FAC-40: copy de validez fiscal en setup/emisión; homo no lo muestra."""
    prod, _ = _api_para_ambiente(
        ArcaEnvironment.PROD, tmp_path / "prod", test_cert_and_key, arca
    )
    for path in ("/", "/configuracion"):
        r = prod.get(path)
        assert r.status_code == 200, path
        assert "validez fiscal" in r.text.lower()

    _crear_cliente_por_form(prod)
    invoice_id = _generar_borrador(prod)
    revisar = prod.get(f"/facturas/{invoice_id}/revisar")
    assert revisar.status_code == 200
    assert "validez fiscal real" in revisar.text.lower()
    assert "Autorizar en ARCA (validez fiscal)" in revisar.text

    arca.last_cmp[(1, 19)] = 5
    force_page = prod.post(
        f"/ui/facturas/{invoice_id}/authorize",
        data=with_csrf(prod),
    )
    assert force_page.status_code == 409
    assert "force=true" in force_page.text
    assert "Forzar autorización" in force_page.text
    assert "¿Forzar autorización en ARCA?" in force_page.text

    homo, _ = _api_para_ambiente(
        ArcaEnvironment.HOMO, tmp_path / "homo", test_cert_and_key, arca
    )
    home = homo.get("/")
    assert home.status_code == 200
    assert "Ambiente de Producción" not in home.text
    assert "validez fiscal real" not in home.text.lower()
    cfg = homo.get("/configuracion")
    assert cfg.status_code == 200
    assert "Configuración de Producción" not in cfg.text


@pytest.mark.parametrize("environment", list(ArcaEnvironment))
def test_health_identifica_ambiente_sin_filtrar_secretos(
    environment, tmp_path, test_cert_and_key, arca
):
    client, profile = _api_para_ambiente(
        environment, tmp_path, test_cert_and_key, arca
    )
    r = client.get("/health")
    assert r.status_code == 200
    payload = r.json()
    assert payload == {"status": "ok", "environment": environment.value}
    dumped = r.text
    assert str(profile.paths.root) not in dumped
    assert "cert.crt" not in dumped
    assert "cert.key" not in dumped


# --- setup certificado (FAC-37) ---


def _client_sin_certificados(
    tmp_path, arca, *, seed: bool = True
) -> tuple[TestClient, EnvironmentProfile]:
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "setup")
    profile.paths.ensure_layout()
    conn = db.connect(profile.paths.db)
    if seed:
        seed_params(conn)
        seed_settings(conn, ambiente="homo")
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    app = create_app(profile, config=config, conn=conn, wsfex=wsfex)
    client = TestClient(app, base_url=loopback_base_url())
    client.conn = conn
    return client, profile


def test_setup_pagina_identifica_ambiente_y_formulario(tmp_path, arca):
    client, _ = _client_sin_certificados(tmp_path, arca, seed=False)
    r = client.get("/setup")
    assert r.status_code == 200
    assert "Homologación" in r.text
    assert "read-only" in r.text.lower() or "read-only" in r.text
    assert 'name="certificado"' in r.text
    assert 'name="clave"' in r.text
    assert str(tmp_path) not in r.text
    assert "BEGIN PRIVATE KEY" not in r.text


def test_setup_instala_certificado_y_muestra_metadata(
    tmp_path, test_cert_and_key, arca
):
    client, profile = _client_sin_certificados(tmp_path, arca, seed=False)
    cert_pem, key_pem = test_cert_and_key
    r = client.post(
        "/ui/setup/certificado",
        data=with_csrf(client, {}),
        files={
            "certificado": ("cert.crt", cert_pem, "application/x-pem-file"),
            "clave": ("cert.key", key_pem, "application/x-pem-file"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?aviso=instalado"

    ok = client.get("/setup?aviso=instalado")
    assert ok.status_code == 200
    assert "20111111112" in ok.text
    assert "Certificado instalado" in ok.text
    assert 'action="/ui/setup/emisor"' in ok.text
    assert 'name="razon_social"' in ok.text
    assert profile.paths.cert.is_file()
    assert profile.paths.key.is_file()

    status = client.get("/setup/status").json()
    assert status["state"] == "emisor_required"
    assert status["ready"] is False


def test_setup_emisor_onboarding_llega_a_ready(
    tmp_path, test_cert_and_key, arca
):
    """FAC-38: crear emisor en /setup activa y completa el perfil."""
    client, profile = _client_sin_certificados(tmp_path, arca, seed=False)
    cert_pem, key_pem = test_cert_and_key
    client.post(
        "/ui/setup/certificado",
        data=with_csrf(client, {}),
        files={
            "certificado": ("cert.crt", cert_pem, "application/x-pem-file"),
            "clave": ("cert.key", key_pem, "application/x-pem-file"),
        },
        follow_redirects=False,
    )

    r = client.post(
        "/ui/setup/emisor",
        data=with_csrf(client, EMISOR_FORM),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?aviso=listo"

    ok = client.get("/setup?aviso=listo")
    assert ok.status_code == 200
    assert "está listo" in ok.text.lower()
    assert 'name="ambiente"' not in ok.text

    status = client.get("/setup/status").json()
    assert status["state"] == "ready"
    assert status["ready"] is True
    assert client.conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0] == 1
    assert get_active_emisor_id(client.conn) is not None


def test_setup_emisor_invalido_no_crea_duplicados(tmp_path, test_cert_and_key, arca):
    client, _ = _client_sin_certificados(tmp_path, arca, seed=False)
    cert_pem, key_pem = test_cert_and_key
    client.post(
        "/ui/setup/certificado",
        data=with_csrf(client, {}),
        files={
            "certificado": ("cert.crt", cert_pem, "application/x-pem-file"),
            "clave": ("cert.key", key_pem, "application/x-pem-file"),
        },
    )
    r = client.post(
        "/ui/setup/emisor",
        data=with_csrf(client, {**EMISOR_FORM, "razon_social": ""}),
    )
    assert r.status_code == 422
    assert client.conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0] == 0

    r2 = client.post(
        "/ui/setup/emisor",
        data=with_csrf(client, EMISOR_FORM),
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert client.conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0] == 1


def test_setup_retoma_emisor_sin_duplicar_tras_reinicio(
    tmp_path, test_cert_and_key, arca
):
    """FAC-38: reinicio con emisor incompleto retoma el mismo registro."""
    from facturador import repo
    from facturador.settings import Emisor, Settings, save_settings

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "setup")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)

    conn = db.connect(profile.paths.db)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social="PARCIAL S.A.",
                domicilio="",
                iibb="",
                inicio_actividades="",
                ambiente="homo",
            )
        ),
    )
    emisor_id = repo.list_emisores(conn)[0]["id"]

    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    client = TestClient(
        create_app(profile, config=config, conn=conn, wsfex=wsfex),
        base_url=loopback_base_url(),
    )
    client.conn = conn

    pagina = client.get("/setup")
    assert pagina.status_code == 200
    assert "PARCIAL S.A." in pagina.text
    assert 'name="emisor_id"' in pagina.text

    r = client.post(
        "/ui/setup/emisor",
        data=with_csrf(
            client,
            {
                **EMISOR_FORM,
                "emisor_id": emisor_id,
                "razon_social": "PARCIAL S.A.",
            },
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0] == 1
    assert client.get("/setup/status").json()["ready"] is True


def test_setup_punto_venta_onboarding(tmp_path, test_cert_and_key, arca):
    """FAC-38: emisor completo sin PV exige paso de punto de venta."""
    from facturador import repo
    from facturador.settings import Emisor, Settings, save_settings, set_active_emisor

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "setup")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)

    conn = db.connect(profile.paths.db)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social=EMISOR_FORM["razon_social"],
                domicilio=EMISOR_FORM["domicilio"],
                iibb=EMISOR_FORM["iibb"],
                inicio_actividades=EMISOR_FORM["inicio_actividades"],
                condicion_iva=EMISOR_FORM["condicion_iva"],
                ambiente="homo",
                puntos_venta=(),
            ),
        ),
    )
    set_active_emisor(conn, repo.list_emisores(conn)[0]["id"])

    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    client = TestClient(
        create_app(profile, config=config, conn=conn, wsfex=wsfex),
        base_url=loopback_base_url(),
    )

    pagina = client.get("/setup")
    assert pagina.status_code == 200
    assert 'action="/ui/setup/punto-venta"' in pagina.text

    r = client.post(
        "/ui/setup/punto-venta",
        data=with_csrf(client, {"puntos_venta": "3, 7"}),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?aviso=listo"
    assert client.get("/setup/status").json()["ready"] is True


def test_setup_emisor_rechaza_editar_emisor_de_otro_ambiente(
    tmp_path, test_cert_and_key, arca
):
    """Codex: no mutar emisor legacy de otro perfil durante onboarding."""
    from facturador import repo
    from facturador.settings import Emisor, Settings, save_settings, set_active_emisor

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "setup")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)

    conn = db.connect(profile.paths.db)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social="LEGACY PROD S.A.",
                domicilio=EMISOR_FORM["domicilio"],
                iibb=EMISOR_FORM["iibb"],
                inicio_actividades=EMISOR_FORM["inicio_actividades"],
                condicion_iva=EMISOR_FORM["condicion_iva"],
                ambiente="prod",
            )
        ),
    )
    otro_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, otro_id)

    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    client = TestClient(
        create_app(profile, config=config, conn=conn, wsfex=wsfex),
        base_url=loopback_base_url(),
    )
    client.conn = conn

    r = client.post(
        "/ui/setup/emisor",
        data=with_csrf(client, {**EMISOR_FORM, "emisor_id": otro_id}),
    )
    assert r.status_code == 422
    assert "perfil homo" in r.text
    row = repo.get_emisor(conn, otro_id)
    assert row is not None
    assert row["razon_social"] == "LEGACY PROD S.A."


def test_setup_emisor_rechaza_id_ajeno_al_onboarding(
    tmp_path, test_cert_and_key, arca
):
    """Codex: no actualizar otro emisor del mismo perfil vía emisor_id."""
    from facturador import repo
    from facturador.settings import Emisor, Settings, save_settings

    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "setup")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)

    conn = db.connect(profile.paths.db)
    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social="PRIMERO S.A.",
                domicilio=EMISOR_FORM["domicilio"],
                iibb=EMISOR_FORM["iibb"],
                inicio_actividades=EMISOR_FORM["inicio_actividades"],
                condicion_iva=EMISOR_FORM["condicion_iva"],
                ambiente="homo",
            )
        ),
    )
    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social="SEGUNDO S.A.",
                domicilio=EMISOR_FORM["domicilio"],
                iibb=EMISOR_FORM["iibb"],
                inicio_actividades=EMISOR_FORM["inicio_actividades"],
                condicion_iva=EMISOR_FORM["condicion_iva"],
                ambiente="homo",
            )
        ),
    )
    primero_id, segundo_id = (
        row["id"] for row in repo.list_emisores(conn)[:2]
    )

    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    client = TestClient(
        create_app(profile, config=config, conn=conn, wsfex=wsfex),
        base_url=loopback_base_url(),
    )
    client.conn = conn

    r = client.post(
        "/ui/setup/emisor",
        data=with_csrf(
            client,
            {
                **EMISOR_FORM,
                "emisor_id": segundo_id,
                "razon_social": "PIRATADO S.A.",
            },
        ),
    )
    assert r.status_code == 422
    assert "paso de setup en curso" in r.text
    assert repo.get_emisor(conn, segundo_id)["razon_social"] == "SEGUNDO S.A."
    assert repo.get_emisor(conn, primero_id)["razon_social"] == "PRIMERO S.A."


def test_setup_rechaza_par_invalido_sin_eco_de_clave(tmp_path, test_cert_and_key, arca):
    client, _ = _client_sin_certificados(tmp_path, arca, seed=False)
    cert_pem, key_pem = test_cert_and_key
    r = client.post(
        "/ui/setup/certificado",
        data=with_csrf(client, {}),
        files={
            "certificado": ("cert.crt", cert_pem, "application/x-pem-file"),
            "clave": ("cert.key", b"NOT-A-VALID-KEY", "application/x-pem-file"),
        },
    )
    assert r.status_code == 422
    assert 'class="panel error"' in r.text
    assert "BEGIN PRIVATE KEY" not in r.text
    assert key_pem.decode() not in r.text


def test_setup_retoma_paso_tras_reinicio(tmp_path, test_cert_and_key, arca):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "setup")
    profile.paths.ensure_layout()
    cert_pem, key_pem = test_cert_and_key
    install_test_cert_pair(profile.paths, cert_pem, key_pem)

    conn = db.connect(profile.paths.db)
    config = Config(env=ArcaEnvironment.HOMO, paths=profile.paths)
    wsfex = WsfexClient(
        config,
        wsaa=FakeWsaa(),
        http=httpx.Client(transport=httpx.MockTransport(arca.handler)),
    )
    client2 = TestClient(
        create_app(profile, config=config, conn=conn, wsfex=wsfex),
        base_url=loopback_base_url(),
    )
    assert client2.get("/setup/status").json()["state"] == "emisor_required"
    assert "Certificado instalado" in client2.get("/setup").text


# --- cambio de ambiente por reinicio (FAC-32 / ADR 0001) ---


def test_cambiar_ambiente_solo_con_launcher(monkeypatch, api):
    """Sin FACTURADOR_LAUNCHER no se ofrece el botón ni se acepta el POST."""
    from facturador.launcher.switch import LAUNCHER_SUPERVISED_ENV

    monkeypatch.delenv(LAUNCHER_SUPERVISED_ENV, raising=False)
    home = api.get("/")
    assert home.status_code == 200
    assert "Cambiar ambiente" not in home.text
    assert 'action="/ui/cambiar-ambiente"' not in home.text

    r = api.post("/ui/cambiar-ambiente", data=with_csrf(api))
    assert r.status_code == 422
    assert "launcher" in r.text.lower()


def test_cambiar_ambiente_con_launcher_escribe_pedido_sin_hot_switch(
    monkeypatch, tmp_path, test_cert_and_key, arca
):
    """Con launcher: botón visible; POST pide reinicio sin mutar el ambiente."""
    from facturador.launcher.switch import (
        LAUNCHER_SUPERVISED_ENV,
        LAUNCHER_SUPERVISED_VALUE,
        read_change_environment_request,
    )

    monkeypatch.setenv(LAUNCHER_SUPERVISED_ENV, LAUNCHER_SUPERVISED_VALUE)
    client, profile = _api_para_ambiente(
        ArcaEnvironment.HOMO, tmp_path, test_cert_and_key, arca
    )

    home = client.get("/")
    assert home.status_code == 200
    assert "Cambiar ambiente" in home.text
    assert 'action="/ui/cambiar-ambiente"' in home.text

    before = client.app.state.profile.environment
    before_cfg = client.app.state.service.config.env
    wsfex_id = id(client.app.state.service.wsfex)

    r = client.post("/ui/cambiar-ambiente", data=with_csrf(client))
    assert r.status_code == 200
    assert "launcher" in r.text.lower() or "Homologación" in r.text
    assert "Cancelar" in r.text or "cancelás" in r.text

    assert client.app.state.profile.environment is before is ArcaEnvironment.HOMO
    assert client.app.state.service.config.env is before_cfg is ArcaEnvironment.HOMO
    assert id(client.app.state.service.wsfex) == wsfex_id
    req = read_change_environment_request(profile.paths)
    assert req is not None
    assert req.from_environment is ArcaEnvironment.HOMO
    assert str(profile.paths.root) not in r.text

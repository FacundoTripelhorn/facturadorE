"""Fase 4 — API + máquina de estados.

Cubre los tests exigidos por design.md §4 fase 4: idempotencia de authorize,
reproceso verificado, escaping XML con datos hostiles vía API, rechazo con
errores legibles; más lock anti doble-submit, reconciliación de 'unknown' y
chequeo de DB desactualizada.
"""

import datetime as dt
import threading

import pytest
from pydantic import ValidationError

from facturador import repo
from facturador.schemas import EmisorCreateIn, EmisorUpdateIn
from facturador.service import StaleRegistryError

CLIENTE = {
    "razon_social": "CLIENTE URUGUAY S.A.",
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "RUT 219999830019",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": True,
}


def _crear_cliente(api, **overrides):
    r = api.post("/clients", json={**CLIENTE, **overrides})
    assert r.status_code == 201, r.text
    return r.json()


def _crear_draft(api, **overrides):
    r = api.post("/invoices", json={"imp_total": "1500.00", **overrides})
    assert r.status_code == 201, r.text
    return r.json()


# --- emisores ---

EMISOR_API = {
    "razon_social": "MI EMPRESA S.R.L.",
    "domicilio": "Calle Falsa 123, CABA",
    "iibb": "901-123456-7",
    "inicio_actividades": "01/2020",
    "condicion_iva": "IVA Responsable Inscripto",
    "puntos_venta": [1],
}


def test_api_rechaza_ambiente_obsoleto_al_crear_emisor():
    with pytest.raises(ValidationError, match="ambiente"):
        EmisorCreateIn(**EMISOR_API, ambiente="prod")


def test_api_rechaza_ambiente_obsoleto_al_editar_emisor():
    with pytest.raises(ValidationError, match="ambiente"):
        EmisorUpdateIn(**EMISOR_API, ambiente="prod")


# --- clientes ---


def test_alta_de_cliente_valida_codigos_contra_arca_params(api):
    r = api.post("/clients", json={**CLIENTE, "pais_dst": 999})
    assert r.status_code == 422
    assert "pais_dst" in r.json()["detail"]


def test_cliente_default_es_unico(api):
    primero = _crear_cliente(api)
    _crear_cliente(api, razon_social="OTRO S.A.", is_default=True)
    clientes = api.get("/clients").json()
    defaults = [c for c in clientes if c["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["razon_social"] == "OTRO S.A."
    assert primero["id"] != defaults[0]["id"]


def test_editar_cliente_no_toca_facturas_autorizadas(api, arca):
    cliente = _crear_cliente(api)
    draft = _crear_draft(api)
    api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    api.put(
        f"/clients/{cliente['id']}",
        json={**CLIENTE, "razon_social": "RENOMBRADO S.A."},
    )
    factura = api.get(f"/invoices/{draft['id']}").json()
    assert factura["cliente"] == "CLIENTE URUGUAY S.A."  # snapshot intacto


# --- creación de drafts ---


def test_caso_feliz_semanal_solo_monto(api, arca):
    """§0.1: con cliente default alcanza el monto; el resto se precarga."""
    _crear_cliente(api)
    draft = _crear_draft(api)
    assert draft["status"] == "draft"
    assert draft["cliente"] == CLIENTE["razon_social"]
    assert draft["dst_cmp"] == 225
    assert draft["moneda_id"] == "DOL"
    assert draft["moneda_ctz"] == arca.ctz  # cotización ARCA automática
    assert draft["fecha_cbte"] == dt.date.today().strftime("%Y%m%d")
    assert draft["fecha_pago"] == draft["fecha_cbte"]
    assert draft["incoterms"] == ""  # servicios
    assert len(draft["items"]) == 1
    item = draft["items"][0]
    assert item["pro_ds"] == CLIENTE["descripcion_default"]
    assert item["pro_qty"] == "1"
    assert item["pro_umed"] == 7
    assert item["pro_precio_uni"] == "1500.00"


def test_sin_cliente_default_ni_client_id_es_conflicto(api):
    r = api.post("/invoices", json={"imp_total": "100.00"})
    assert r.status_code == 409


def test_sin_datos_de_emisor_no_se_emite(api):
    """Sin emisor completo el perfil no está ready (FAC-35): la guardia
    bloquea la emisión antes del chequeo de dominio del service."""
    with api.conn:
        api.conn.execute("DELETE FROM emisores")
    r = api.post("/invoices", json={"imp_total": "100.00"})
    assert r.status_code == 503
    body = r.json()
    assert body["setup_state"] == "emisor_required"
    assert "setup" in body["detail"]


def test_imp_total_distinto_de_items_es_error_de_dominio(api):
    _crear_cliente(api)
    r = api.post(
        "/invoices",
        json={
            "imp_total": "100.00",
            "items": [{"pro_ds": "Servicio", "pro_precio_uni": "99.00"}],
        },
    )
    assert r.status_code == 422
    assert "imp_total" in r.json()["detail"]


def test_monto_invalido_rechazado_por_pydantic(api):
    _crear_cliente(api)
    r = api.post("/invoices", json={"imp_total": "-5"})
    assert r.status_code == 422


# --- authorize: caso feliz e idempotencia ---


def test_authorize_asigna_numeracion_de_arca_y_guarda_cae(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    r = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert r.status_code == 200, r.text
    factura = r.json()
    assert factura["status"] == "authorized"
    assert factura["arca_id"] == 1          # FEXGetLast_ID + 1
    assert factura["cbte_nro"] == 1         # FEXGetLast_CMP + 1
    assert factura["cae"] == "76100000000001"
    assert factura["cae_fch_vto"] == "20260713"
    assert arca.calls["FEXAuthorize"] == 1
    assert arca.calls["FEXGetCMP"] == 1     # verificación post-emisión


def test_authorize_es_idempotente(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    primera = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true").json()
    segunda = api.post(f"/invoices/{draft['id']}/authorize").json()
    assert segunda["cae"] == primera["cae"]
    assert arca.calls["FEXAuthorize"] == 1  # la segunda no tocó ARCA


# --- rechazo con errores legibles ---


def test_rechazo_deja_error_legible_y_no_permite_reintentar(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    arca.authorize_mode = "reject"
    r = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert r.status_code == 200
    factura = r.json()
    assert factura["status"] == "rejected"
    assert factura["cae"] is None
    assert "1068" in factura["last_error"]
    assert "Id_impositivo" in factura["last_error"]

    arca.authorize_mode = "ok"
    retry = api.post(f"/invoices/{draft['id']}/authorize")
    assert retry.status_code == 409  # corregir => nueva factura


# --- unknown + reconciliación + reproceso ---


def test_timeout_post_envio_reconcilia_en_get(api, arca):
    """El caso 'unknown' clave: ARCA emitió pero la respuesta se perdió."""
    _crear_cliente(api)
    draft = _crear_draft(api)
    arca.authorize_mode = "timeout_but_issued"
    factura = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true").json()
    assert factura["status"] == "unknown"
    assert "Sin respuesta" in factura["last_error"]

    arca.authorize_mode = "ok"
    reconciliada = api.get(f"/invoices/{draft['id']}").json()
    assert reconciliada["status"] == "authorized"
    assert reconciliada["cae"] == "76100000000001"  # recuperado de FEXGetCMP
    assert arca.calls["FEXAuthorize"] == 1  # nunca se re-envió


def test_timeout_sin_emision_reintenta_con_mismo_id(api, arca):
    """Reproceso verificado (checklist §2.1.1 punto 3) de punta a punta."""
    _crear_cliente(api)
    draft = _crear_draft(api)
    arca.authorize_mode = "timeout"
    factura = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true").json()
    assert factura["status"] == "unknown"
    arca_id_original = factura["arca_id"]
    cbte_nro_original = factura["cbte_nro"]

    arca.authorize_mode = "ok"
    retry = api.post(f"/invoices/{draft['id']}/authorize").json()
    assert retry["status"] == "authorized"
    assert retry["arca_id"] == arca_id_original      # mismo Id idempotente
    assert retry["cbte_nro"] == cbte_nro_original    # mismos datos
    assert arca.issued[(19, 1, cbte_nro_original)]["arca_id"] == arca_id_original


def test_arca_id_nuevo_no_colisiona_con_reservas_locales(api, arca):
    """Una factura 'unknown' reservó su arca_id sin que ARCA lo conozca
    (timeout pre-envío): la siguiente factura nueva debe saltearlo, no
    reventar contra el UNIQUE de arca_id."""
    _crear_cliente(api)
    colgada = _crear_draft(api)
    arca.authorize_mode = "timeout"
    r = api.post(f"/invoices/{colgada['id']}/authorize?force_desync=true").json()
    assert r["status"] == "unknown"
    assert r["arca_id"] == 1  # reservado localmente, ARCA nunca lo vio

    arca.authorize_mode = "ok"
    nueva = _crear_draft(api)
    r = api.post(f"/invoices/{nueva['id']}/authorize?force_desync=true")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "authorized"
    assert r.json()["arca_id"] == 2  # saltea la reserva local

    # La colgada, al reintentar, encuentra su número tomado por la nueva:
    # jamás adopta un CAE ajeno; queda para revisión manual.
    retry = api.post(f"/invoices/{colgada['id']}/authorize").json()
    assert retry["status"] == "unknown"
    assert retry["cae"] is None


def test_reconciliacion_detecta_comprobante_ajeno(api, arca):
    """Si ARCA registra nuestro número con OTRO importe, jamás adoptar el CAE."""
    _crear_cliente(api)
    draft = _crear_draft(api)
    arca.authorize_mode = "timeout_but_issued"
    api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    # Alguien (otra máquina) emitió con nuestro número y otro importe:
    arca.issued[(19, 1, 1)]["imp_total"] = "999999.00"

    factura = api.get(f"/invoices/{draft['id']}").json()
    assert factura["status"] == "unknown"
    assert "Revisión manual" in factura["last_error"]
    assert factura["cae"] is None


# --- lock anti doble-submit ---


def test_submitting_reciente_bloquea_doble_submit(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    repo.update_invoice(api.conn, draft["id"], status="submitting")
    r = api.post(f"/invoices/{draft['id']}/authorize")
    assert r.status_code == 409
    assert "en curso" in r.json()["detail"]


# --- chequeo de DB desactualizada (§2.5 / FAC-48) ---


def _insert_authorized(
    api,
    *,
    cbte_nro: int,
    source: str = "wsfex",
    punto_venta: int = 1,
    cbte_tipo: int = 19,
    arca_id: int | None = None,
) -> str:
    """Inserta una factura authorized de prueba (wsfex o imported)."""
    invoice_id = f"seed-{source}-{cbte_nro}-{punto_venta}"
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    reserved_id = arca_id if arca_id is not None else (10_000 + cbte_nro)
    api.conn.execute(
        "INSERT INTO invoices ("
        "  id, arca_id, cbte_tipo, punto_venta, cbte_nro, status, source,"
        "  fecha_cbte, fecha_pago, dst_cmp, cliente, cuit_pais_cliente,"
        "  moneda_ctz, imp_total, cae, cae_fch_vto, environment,"
        "  created_at, updated_at"
        ") VALUES ("
        "  ?, ?, ?, ?, ?, 'authorized', ?,"
        "  '20260101', '20260101', 225, 'SEED', 55000002002,"
        "  '1', '100', '76123456789012', '20260713', 'homo',"
        "  ?, ?)",
        (
            invoice_id,
            reserved_id if source == "wsfex" else None,
            cbte_tipo,
            punto_venta,
            cbte_nro,
            source,
            now,
            now,
        ),
    )
    api.conn.commit()
    return invoice_id


def test_registro_alineado_permite_emision(api, arca):
    """FAC-48: last_cmp == max wsfex local → se puede emitir el siguiente."""
    _crear_cliente(api)
    _insert_authorized(api, cbte_nro=3, source="wsfex", arca_id=103)
    arca.last_cmp[(1, 19)] = 3
    arca.last_id = 103
    draft = _crear_draft(api)
    r = api.post(f"/invoices/{draft['id']}/authorize")
    assert r.status_code == 200, r.text
    assert r.json()["cbte_nro"] == 4
    assert r.json()["source"] == "wsfex"


def test_db_desactualizada_bloquea_emision(api, arca):
    """FAC-48: ARCA adelante del registro wsfex local → bloqueo pre-asignación."""
    _crear_cliente(api)
    draft = _crear_draft(api)
    arca.last_cmp[(1, 19)] = 5  # ARCA conoce 5 comprobantes; DB local, 0
    r = api.post(f"/invoices/{draft['id']}/authorize")
    assert r.status_code == 409
    assert "desactualizado" in r.json()["detail"]
    # La factura vuelve a draft (el lock no queda tomado) y no se asignó número.
    body = api.get(f"/invoices/{draft['id']}").json()
    assert body["status"] == "draft"
    assert body["cbte_nro"] is None
    row = repo.get_invoice(api.conn, draft["id"])
    assert row is not None
    assert row["raw_request"] is None

    forzada = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert forzada.status_code == 200
    assert forzada.json()["cbte_nro"] == 6


def test_local_adelante_de_arca_bloquea_sin_force(api, arca):
    """FAC-48: local wsfex > FEXGetLast_CMP → inconsistencia no forzable."""
    _crear_cliente(api)
    _insert_authorized(api, cbte_nro=7, source="wsfex", arca_id=107)
    arca.last_cmp[(1, 19)] = 3
    draft = _crear_draft(api)
    r = api.post(f"/invoices/{draft['id']}/authorize")
    assert r.status_code == 409
    assert "inconsistente" in r.json()["detail"]
    forced = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert forced.status_code == 409
    assert "inconsistente" in forced.json()["detail"]
    assert api.get(f"/invoices/{draft['id']}").json()["status"] == "draft"


def test_importados_no_satisfacen_ni_contaminan_el_chequeo(api, arca):
    """FAC-48: filas imported no alinean el registro ni provocan local-ahead."""
    _crear_cliente(api)
    _insert_authorized(api, cbte_nro=20, source="imported")
    # ARCA adelante del wsfex local (0): el importado no "satisface" el chequeo.
    arca.last_cmp[(1, 19)] = 5
    draft = _crear_draft(api)
    r = api.post(f"/invoices/{draft['id']}/authorize")
    assert r.status_code == 409
    assert "desactualizado" in r.json()["detail"]

    # Sin hueco ARCA: el importado tampoco contamina como local-ahead.
    arca.last_cmp[(1, 19)] = 0
    ok = api.post(f"/invoices/{draft['id']}/authorize")
    assert ok.status_code == 200, ok.text
    assert ok.json()["cbte_nro"] == 1


def test_db_desactualizada_es_un_tipo_propio_no_un_mensaje(api, arca):
    """El conflicto forzable tiene su propio tipo (StaleRegistryError) para
    que la UI no dependa del texto del mensaje."""
    _crear_cliente(api)
    draft = _crear_draft(api)
    arca.last_cmp[(1, 19)] = 5
    service = api.app.state.service
    with pytest.raises(StaleRegistryError):
        service.authorize(draft["id"])


def test_authorize_concurrente_no_duplica_numeracion(api, arca):
    """Dos authorize en paralelo sobre facturas distintas se serializan: cada
    uno ve la numeración de ARCA ya avanzada por el anterior. Sin el lock de
    numeración ambos tomarían el mismo arca_id/cbte_nro."""
    _crear_cliente(api)
    d1 = _crear_draft(api)
    d2 = _crear_draft(api)
    service = api.app.state.service

    barrera = threading.Barrier(2)
    resultados: dict[str, object] = {}

    def correr(invoice_id: str) -> None:
        barrera.wait()  # maximiza el solapamiento
        try:
            resultados[invoice_id] = dict(service.authorize(invoice_id))
        except Exception as exc:  # noqa: BLE001 - se re-chequea abajo
            resultados[invoice_id] = exc

    hilos = [threading.Thread(target=correr, args=(d["id"],)) for d in (d1, d2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    filas = [resultados[d1["id"]], resultados[d2["id"]]]
    for f in filas:
        assert not isinstance(f, Exception), f
        assert f["status"] == "authorized"
    assert {f["cbte_nro"] for f in filas} == {1, 2}
    assert {f["arca_id"] for f in filas} == {1, 2}
    assert arca.calls["FEXAuthorize"] == 2


def test_descartar_borrador_borra_tambien_los_items(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    assert len(repo.get_invoice_items(api.conn, draft["id"])) == 1

    assert repo.delete_draft(api.conn, draft["id"]) is True
    assert repo.get_invoice(api.conn, draft["id"]) is None
    assert repo.get_invoice_items(api.conn, draft["id"]) == []


# --- escaping XML con datos hostiles vía API (checklist punto 4) ---


def test_datos_hostiles_de_cliente_viajan_escapados(api, arca):
    _crear_cliente(
        api,
        razon_social='PYME </Cliente><Imp_total>1</Imp_total> & "CO"',
        domicilio="Ruta <8> km 1 & 1/2",
    )
    draft = _crear_draft(api)
    r = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    # El fake parsea cada request con ET.fromstring: si la inyección hubiera
    # roto el XML, esto habría fallado. El valor llega intacto y escapado.
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "authorized"
    esperado = 'PYME </Cliente><Imp_total>1</Imp_total> & "CO"'
    assert arca.last_authorize_cliente == esperado
    assert arca.issued[(19, 1, 1)]["imp_total"] == "1500.00"


# --- params, cotización y salud ---


def test_params_se_sirven_del_cache_sin_tocar_arca(api, arca):
    r = api.get("/params/moneda")
    assert r.status_code == 200
    assert {p["code"] for p in r.json()} == {"DOL", "PES"}
    assert arca.calls["FEXGetPARAM_MON"] == 0


def test_params_kind_invalido(api):
    assert api.get("/params/noexiste").status_code == 404


def test_cotizacion_del_dia(api, arca):
    r = api.get("/params/currency/DOL/rate")
    assert r.status_code == 200
    assert r.json() == {
        "moneda_id": "DOL",
        "fecha": "20260703",
        "cotizacion": "1145.5690",
    }


def test_health_arca(api):
    r = api.get("/health/arca")
    assert r.status_code == 200
    assert r.json() == {
        "environment": "homo",
        "appserver": "OK",
        "dbserver": "OK",
        "authserver": "OK",
    }


def test_listado_paginado(api, arca):
    _crear_cliente(api)
    for _ in range(3):
        _crear_draft(api)
    assert len(api.get("/invoices").json()) == 3
    assert len(api.get("/invoices?limit=2").json()) == 2
    assert len(api.get("/invoices?limit=2&offset=2").json()) == 1


# --- vínculo factura ↔ perfil (ADR 0001 / FAC-26) ---


def test_el_ambiente_de_la_factura_sale_del_perfil_no_del_request(api, arca):
    """El environment es auditoría inmutable sellada por el perfil: no es
    input del request, y mandarlo igual se ignora."""
    _crear_cliente(api)
    r = api.post(
        "/invoices", json={"imp_total": "1500.00", "environment": "prod"}
    )
    assert r.status_code == 201, r.text
    fila = repo.get_invoice(api.conn, r.json()["id"])
    assert fila["environment"] == "homo"  # el del perfil del api fixture


def _marcar_como_de_otro_perfil(api, invoice_id):
    """Simula una DB ajena restaurada en el perfil equivocado."""
    with api.conn:
        api.conn.execute(
            "UPDATE invoices SET environment = 'prod' WHERE id = ?",
            (invoice_id,),
        )


def test_acceso_a_factura_de_otro_perfil_rechazado(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    _marcar_como_de_otro_perfil(api, draft["id"])

    r = api.get(f"/invoices/{draft['id']}")
    assert r.status_code == 409
    assert "otro" in r.json()["detail"]

    r = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert r.status_code == 409
    assert "perfil" in r.json()["detail"]


def test_emisor_activo_de_otro_perfil_rechazado(api, arca):
    """FAC-26 + FAC-35: emisor sellado con otro ambiente no deja el setup
    en ``ready``; la guardia responde 503 antes de llegar al 409 de dominio."""
    _crear_cliente(api)
    with api.conn:
        api.conn.execute("UPDATE emisores SET ambiente = 'prod'")

    r = api.post("/invoices", json={"imp_total": "1500.00"})
    assert r.status_code == 503
    assert r.json()["setup_state"] == "emisor_required"
    assert api.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0


def test_pdf_de_factura_de_otro_perfil_rechazado(api, arca):
    _crear_cliente(api)
    draft = _crear_draft(api)
    r = api.post(f"/invoices/{draft['id']}/authorize?force_desync=true")
    assert r.status_code == 200, r.text
    _marcar_como_de_otro_perfil(api, draft["id"])

    assert api.get(f"/invoices/{draft['id']}/pdf").status_code == 409


# --- FAC-68: peek solo-lectura del registro ARCA ---


def test_peek_arca_lista_mas_recientes_primero(api, arca):
    """FEXGetLast_CMP + FEXGetCMP; paginación offset/limit (más reciente primero)."""
    for nro in (1, 2, 3, 4, 5):
        arca.seed_issued(
            tipo=19,
            pv=1,
            nro=nro,
            imp_total=f"{100 * nro}.00",
            cliente=f"Cliente {nro}",
        )

    before = api.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    r = api.get("/invoices/arca?limit=2&offset=0")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["punto_venta"] == 1
    assert body["cbte_tipo"] == 19
    assert body["last_cmp"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [inv["cbte_nro"] for inv in body["invoices"]] == [5, 4]
    assert body["invoices"][0]["cae"].startswith("761")
    assert body["invoices"][0]["imp_total"] == "500.00"
    assert body["invoices"][0]["cliente"] == "Cliente 5"
    assert body["invoices"][0]["items"][0]["pro_ds"] == "Servicio"
    assert body["gaps"] == []

    page2 = api.get("/invoices/arca?limit=2&offset=2").json()
    assert [inv["cbte_nro"] for inv in page2["invoices"]] == [3, 2]

    # Sin escrituras locales (no es catch-up / rebuild).
    after = api.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    assert after == before
    assert arca.calls["FEXGetLast_CMP"] >= 1
    assert arca.calls["FEXGetCMP"] >= 4


def test_peek_arca_reporta_gaps_1521(api, arca):
    """Huecos confirmados (ErrCode 1521) van en gaps; el resto en invoices."""
    arca.seed_issued(tipo=19, pv=1, nro=1, imp_total="10.00")
    arca.seed_issued(tipo=19, pv=1, nro=3, imp_total="30.00")
    # last_cmp=3 pero nro 2 no existe → gap.

    r = api.get("/invoices/arca?limit=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_cmp"] == 3
    assert [inv["cbte_nro"] for inv in body["invoices"]] == [3, 1]
    assert body["gaps"] == [
        {"punto_venta": 1, "cbte_tipo": 19, "cbte_nro": 2}
    ]


def test_peek_arca_cbte_nro_unico(api, arca):
    arca.seed_issued(
        tipo=19, pv=1, nro=7, imp_total="77.00", cliente="Solo Uno"
    )
    # last_cmp queda en 7; pedimos un número puntual.
    r = api.get("/invoices/arca?cbte_nro=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_cmp"] == 7
    assert len(body["invoices"]) == 1
    assert body["invoices"][0]["cbte_nro"] == 7
    assert body["invoices"][0]["cliente"] == "Solo Uno"
    assert body["gaps"] == []

    missing = api.get("/invoices/arca?cbte_nro=2")
    assert missing.status_code == 200, missing.text
    assert missing.json()["invoices"] == []
    assert missing.json()["gaps"] == [
        {"punto_venta": 1, "cbte_tipo": 19, "cbte_nro": 2}
    ]


def test_peek_arca_punto_venta_override(api, arca):
    arca.seed_issued(tipo=19, pv=2, nro=1, imp_total="99.00")
    r = api.get("/invoices/arca?punto_venta=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["punto_venta"] == 2
    assert body["last_cmp"] == 1
    assert body["invoices"][0]["punto_venta"] == 2
    assert body["invoices"][0]["imp_total"] == "99.00"


def test_peek_arca_ruta_estatica_no_capturada_como_invoice_id(api, arca):
    """GET /invoices/arca no debe caer en GET /invoices/{invoice_id}."""
    r = api.get("/invoices/arca")
    assert r.status_code == 200, r.text
    assert "last_cmp" in r.json()
    # Si "arca" se interpretara como UUID, sería 404 de factura.
    assert r.json()["invoices"] == []
    assert r.json()["gaps"] == []

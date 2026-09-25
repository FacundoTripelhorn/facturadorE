"""Diagnóstico del perfil: ok / aviso / bloquea, local siempre y ARCA a pedido."""

import dataclasses
import datetime as dt

import httpx

from facturador import diagnostics
from facturador.api.setup_guard import is_setup_exempt
from facturador.arca.events import record_events
from facturador.certs import load_certificate_metadata
from facturador.constants import ArcaEnvironment
from facturador.diagnostics import Nivel, diagnosticar_perfil
from facturador.profile import EnvironmentProfile
from facturador.seed_backup import recipients_path
from facturador.seed_backup_sync import (
    LAST_ERROR_KEY,
    LAST_SUCCESS_KEY,
    STATUS_KEY,
    _write_settings,
)
from facturador.settings import load_settings, save_settings
from tests.conftest import TEST_CUIT
from tests.test_setup import _client_for_profile

CLIENTE = {
    "razon_social": "CLIENTE URUGUAY S.A.",
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": 225,
    "cuit_pais": 55000002002,
    "id_impositivo": "RUT 219999830019",
    "descripcion_default": "Servicios de desarrollo de software",
    "is_default": True,
}


def _profile(api) -> EnvironmentProfile:
    return api.app.state.profile


def _chequeo(chequeos, titulo):
    return next(c for c in chequeos if c.titulo == titulo)


def _backup_al_dia(api) -> None:
    conn = api.conn
    save_settings(
        conn, dataclasses.replace(load_settings(conn), backup_s3_bucket="mi-bucket")
    )
    recipients_path(_profile(api).paths).write_text("age1xyz\n", encoding="utf-8")
    _write_settings(
        conn, {STATUS_KEY: "ok", LAST_SUCCESS_KEY: "2026-09-20T10:00:00+00:00"}
    )


# --- página ---


def test_perfil_listo_con_avisos_no_consulta_arca(api, arca):
    r = api.get("/diagnostico")
    assert r.status_code == 200
    assert "Se puede emitir," in r.text  # sin S3: el backup es un aviso
    assert f"CUIT {TEST_CUIT}" in r.text
    assert "Solo local" in r.text
    assert "ARCA todavía no se verificó" in r.text
    assert "Verificar ARCA" in r.text
    assert arca.calls["FEXDummy"] == 0
    assert arca.calls["FEXGetLast_CMP"] == 0


def test_perfil_listo_sin_avisos(api, arca):
    _backup_al_dia(api)
    r = api.get("/diagnostico?arca=1")
    assert r.status_code == 200
    assert "Listo para emitir." in r.text
    assert "Al día. Último backup: 2026-09-20." in r.text


def test_menu_tiene_diagnostico(api):
    assert 'href="/diagnostico"' in api.get("/configuracion").text


def test_funciona_sin_setup_y_explica_que_falta(tmp_path):
    profile = EnvironmentProfile.for_testing(ArcaEnvironment.HOMO, tmp_path / "p")
    client = _client_for_profile(profile, seed=False)

    r = client.get("/diagnostico", follow_redirects=False)
    assert r.status_code == 200  # no redirige a /setup
    assert "No se puede emitir." in r.text
    assert "No hay certificado instalado." in r.text
    assert "Incompleta: falta el certificado." in r.text
    assert "No hay emisor activo." in r.text
    assert 'href="/setup"' in r.text

    r = client.get("/diagnostico?arca=1")
    assert r.status_code == 200
    assert "No se puede verificar hasta completar" in r.text


def test_diagnostico_es_setup_exempt():
    assert is_setup_exempt("GET", "/diagnostico")


# --- certificado ---


def test_certificado_por_vencer_es_aviso_y_vencido_bloquea(api):
    profile = _profile(api)
    meta = load_certificate_metadata(profile)
    assert meta is not None

    pronto = meta.not_valid_after - dt.timedelta(days=10, hours=1)
    cert = _chequeo(diagnosticar_perfil(profile, api.conn, ahora=pronto), "Certificado")
    assert cert.nivel is Nivel.AVISO
    assert "Quedan 10 días." in cert.detalle

    vencido = meta.not_valid_after + dt.timedelta(days=1)
    cert = _chequeo(
        diagnosticar_perfil(profile, api.conn, ahora=vencido), "Certificado"
    )
    assert cert.nivel is Nivel.BLOQUEA
    assert "Venció el" in cert.detalle
    assert cert.accion is not None and "Generar uno nuevo" in cert.accion.texto


def test_certificado_vigente_es_ok(api):
    cert = _chequeo(diagnosticar_perfil(_profile(api), api.conn), "Certificado")
    assert cert.nivel is Nivel.OK
    assert cert.detalle.startswith(f"CUIT {TEST_CUIT}. Vence el ")


# --- base de datos, pendientes, backup, avisos ---


def test_esquema_distinto_bloquea(api, monkeypatch):
    monkeypatch.setattr(diagnostics, "latest_version", lambda: 99)
    db = _chequeo(diagnosticar_perfil(_profile(api), api.conn), "Base de datos")
    assert db.nivel is Nivel.BLOQUEA
    assert "espera v99" in db.detalle


def test_comprobante_a_reconciliar_es_aviso(api, arca):
    assert api.post("/clients", json=CLIENTE).status_code == 201
    invoice_id = api.post("/invoices", json={"imp_total": "100.00"}).json()["id"]
    arca.authorize_mode = "timeout"
    api.post(f"/invoices/{invoice_id}/authorize?force_desync=true")

    pend = _chequeo(
        diagnosticar_perfil(_profile(api), api.conn), "Comprobantes a reconciliar"
    )
    assert pend.nivel is Nivel.AVISO
    assert pend.detalle == "1 sin respuesta concluyente de ARCA."


def test_backup_fallido_es_aviso_sin_mostrar_el_error(api):
    _backup_al_dia(api)
    _write_settings(
        api.conn,
        {STATUS_KEY: "failed", LAST_ERROR_KEY: "/ruta/secreta/backups/seed.age"},
    )
    backup = _chequeo(diagnosticar_perfil(_profile(api), api.conn), "Backup")
    assert backup.nivel is Nivel.AVISO
    assert "Falló el último intento" in backup.detalle
    assert "/ruta/secreta" not in api.get("/diagnostico").text


def test_backup_sin_recipients_es_aviso(api):
    _backup_al_dia(api)
    recipients_path(_profile(api).paths).unlink()
    backup = _chequeo(diagnosticar_perfil(_profile(api), api.conn), "Backup")
    assert backup.nivel is Nivel.AVISO
    assert "recipients.txt" in backup.detalle


def test_avisos_de_arca_vigentes_son_aviso(api):
    record_events(
        _profile(api).paths.arca_events, [("12", "Mantenimiento el domingo")]
    )
    avisos = _chequeo(diagnosticar_perfil(_profile(api), api.conn), "Avisos de ARCA")
    assert avisos.nivel is Nivel.AVISO
    assert avisos.detalle == "12: Mantenimiento el domingo"


# --- ARCA ---


def test_arca_ok_y_numeracion_alineada(api, arca):
    r = api.get("/diagnostico?arca=1")
    assert r.status_code == 200
    assert "appserver: OK" in r.text
    assert "Numeración PV 1" in r.text
    assert "Último comprobante 0, igual en ARCA." in r.text
    assert "Volver a verificar" in r.text
    assert arca.calls["FEXDummy"] == 1


def test_arca_adelantado_bloquea_y_ofrece_sincronizar(api, arca):
    arca.last_cmp[(1, 19)] = 5
    r = api.get("/diagnostico?arca=1")
    assert "No se puede emitir." in r.text
    assert "ARCA va por el 5 y el registro local por el 0" in r.text
    assert 'action="/ui/registry/catch-up"' in r.text
    assert 'name="csrf_token" value=""' not in r.text  # el form lleva token


def test_arca_caido_bloquea(api, arca, monkeypatch):
    def caido(body):
        raise httpx.ConnectTimeout("sin conexión")

    monkeypatch.setattr(arca, "_fexdummy", caido)
    r = api.get("/diagnostico?arca=1")
    assert "No se puede emitir." in r.text
    assert "ARCA no responde" in r.text
    assert arca.calls["FEXGetLast_CMP"] == 0


# --- seguridad ---


def test_no_expone_rutas_ni_secretos(api, arca, test_cert_and_key):
    _, key_pem = test_cert_and_key
    raiz = str(_profile(api).paths.root)
    for url in ("/diagnostico", "/diagnostico?arca=1"):
        texto = api.get(url).text
        assert raiz not in texto
        assert "PRIVATE KEY" not in texto
        assert key_pem.decode().splitlines()[1] not in texto
        assert "token" not in texto.lower().replace("csrf_token", "")

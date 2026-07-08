"""Settings de dominio: viven en la DB, con migración desde el entorno para
usuarios que venían del esquema viejo (EMISOR_* y compañía en el .env)."""

import dataclasses
import logging

import pytest

from facturador import db
from facturador.settings import (
    Emisor,
    Settings,
    load_settings,
    migrate_env_settings,
    save_settings,
)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_defaults_sin_nada_guardado(conn):
    s = load_settings(conn)
    assert s == Settings()
    assert s.punto_venta == 1
    assert s.backup_s3_prefix == "facturador"
    assert not s.emisor.completo


def test_guardar_y_releer_ida_y_vuelta(conn):
    guardado = Settings(
        emisor=Emisor(
            razon_social="MI EMPRESA S.R.L.",
            domicilio="Calle Falsa 123, CABA",
            iibb="901-123456-7",
            inicio_actividades="01/2020",
        ),
        punto_venta=7,
        backup_s3_bucket="mi-bucket",
        backup_s3_prefix="facturas",
    )
    save_settings(conn, guardado)
    assert load_settings(conn) == guardado
    assert load_settings(conn).emisor.completo


def test_emisor_completo_exige_todas_las_lineas_del_encabezado():
    """El comprobante real imprime razón social, domicilio, IIBB (literal,
    p.ej. "Exento") e inicio de actividades: con cualquiera vacío el PDF
    queda con un hueco."""
    completo = Emisor(
        razon_social="X",
        domicilio="Y",
        iibb="Exento",
        inicio_actividades="01/08/2020",
    )
    assert completo.completo
    for campo in ("razon_social", "domicilio", "iibb", "inicio_actividades"):
        assert not dataclasses.replace(completo, **{campo: ""}).completo


# --- migración desde el entorno (primer arranque post-upgrade) ---

ENV_VIEJO = {
    "EMISOR_RAZON_SOCIAL": "MI EMPRESA S.R.L.",
    "EMISOR_DOMICILIO": "Calle Falsa 123, CABA",
    "EMISOR_IIBB": "901-123456-7",
    "EMISOR_INICIO_ACTIVIDADES": "01/2020",
    "ARCA_PUNTO_VTA": "3",
    "BACKUP_S3_BUCKET": "bucket-viejo",
    "BACKUP_S3_PREFIX": "prefijo-viejo",
}


def test_migra_el_env_viejo_a_la_db_y_lo_avisa_en_el_log(conn, caplog):
    with caplog.at_level(logging.INFO, logger="facturador.settings"):
        importadas = migrate_env_settings(conn, ENV_VIEJO)

    assert set(importadas) == set(ENV_VIEJO)
    s = load_settings(conn)
    assert s.emisor.razon_social == "MI EMPRESA S.R.L."
    assert s.emisor.domicilio == "Calle Falsa 123, CABA"
    assert s.emisor.iibb == "901-123456-7"
    assert s.emisor.inicio_actividades == "01/2020"
    assert s.punto_venta == 3
    assert s.backup_s3_bucket == "bucket-viejo"
    assert s.backup_s3_prefix == "prefijo-viejo"
    assert "EMISOR_RAZON_SOCIAL" in caplog.text
    assert "pueden borrarse del .env" in caplog.text


def test_lo_guardado_en_la_db_le_gana_al_entorno(conn):
    """La migración corre en cada arranque pero es de una sola vía: una vez
    que el dato está en la DB (p.ej. editado desde la UI), el .env viejo
    deja de tener efecto."""
    save_settings(
        conn,
        Settings(emisor=Emisor(razon_social="EDITADA EN LA UI", domicilio="D")),
    )
    importadas = migrate_env_settings(conn, ENV_VIEJO)
    assert importadas == []
    assert load_settings(conn).emisor.razon_social == "EDITADA EN LA UI"
    assert load_settings(conn).punto_venta == 1


def test_valores_vacios_del_entorno_no_se_importan(conn):
    importadas = migrate_env_settings(
        conn, {"EMISOR_RAZON_SOCIAL": "  ", "EMISOR_IIBB": ""}
    )
    assert importadas == []
    assert load_settings(conn) == Settings()


def test_sin_variables_viejas_no_hace_nada(conn, caplog):
    with caplog.at_level(logging.INFO, logger="facturador.settings"):
        assert migrate_env_settings(conn, {"PATH": "/usr/bin"}) == []
    assert caplog.text == ""

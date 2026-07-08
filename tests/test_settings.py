"""Settings de dominio: viven en la DB, con el emisor (punto de venta
incluido) guardado por ambiente para que homo y prod nunca se mezclen."""

import dataclasses

import pytest

from facturador import db
from facturador.settings import Emisor, Settings, load_settings, save_settings


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_defaults_sin_nada_guardado(conn):
    s = load_settings(conn, "homo")
    assert s == Settings()
    assert s.emisor.punto_venta == 1
    assert s.backup_s3_prefix == "facturador"
    assert not s.emisor.completo


def test_guardar_y_releer_ida_y_vuelta(conn):
    guardado = Settings(
        emisor=Emisor(
            razon_social="MI EMPRESA S.R.L.",
            domicilio="Calle Falsa 123, CABA",
            iibb="901-123456-7",
            inicio_actividades="01/2020",
            punto_venta=7,
        ),
        backup_s3_bucket="mi-bucket",
        backup_s3_prefix="facturas",
    )
    save_settings(conn, "homo", guardado)
    assert load_settings(conn, "homo") == guardado
    assert load_settings(conn, "homo").emisor.completo


def test_el_emisor_es_por_ambiente(conn):
    """Un emisor por ambiente (PV incluido): configurar homo no toca prod,
    así la numeración y los datos de prueba nunca se mezclan con los reales.
    La config de backups sí es global: el backup cubre la DB entera."""
    save_settings(
        conn,
        "homo",
        Settings(
            emisor=Emisor(razon_social="PRUEBAS", punto_venta=9),
            backup_s3_bucket="bucket-comun",
        ),
    )
    prod = load_settings(conn, "prod")
    assert prod.emisor == Emisor()          # prod sigue sin configurar
    assert prod.emisor.punto_venta == 1
    assert prod.backup_s3_bucket == "bucket-comun"

    save_settings(
        conn,
        "prod",
        Settings(emisor=Emisor(razon_social="REAL S.R.L.", punto_venta=3)),
    )
    assert load_settings(conn, "homo").emisor.razon_social == "PRUEBAS"
    assert load_settings(conn, "homo").emisor.punto_venta == 9
    assert load_settings(conn, "prod").emisor.punto_venta == 3


def test_punto_venta_corrupto_cae_al_default(conn):
    save_settings(conn, "homo", Settings())
    with conn:
        conn.execute(
            "UPDATE settings SET value = 'nueve' WHERE key = ?",
            ("homo.emisor_punto_venta",),
        )
    assert load_settings(conn, "homo").emisor.punto_venta == 1


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

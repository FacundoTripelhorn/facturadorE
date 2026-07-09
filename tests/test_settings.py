"""Settings de dominio: viven en la DB. El emisor es una entidad (tabla
emisores) que declara su ambiente y sus puntos de venta, así homo y prod
nunca mezclan datos ni numeración."""

import dataclasses

import pytest

from facturador import db, repo
from facturador.settings import (
    Emisor,
    Settings,
    load_settings,
    save_settings,
    set_active_emisor,
)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_defaults_sin_nada_guardado(conn):
    s = load_settings(conn, "homo")
    assert s == Settings()
    assert s.emisor.ambiente == "homo"
    assert s.emisor.puntos_venta == (1,)
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
            ambiente="homo",
            puntos_venta=(7, 2),
        ),
        backup_s3_bucket="mi-bucket",
        backup_s3_prefix="facturas",
    )
    save_settings(conn, guardado)
    emisor_id = repo.list_emisores(conn, "homo")[0]["id"]
    set_active_emisor(conn, emisor_id)
    releido = load_settings(conn, "homo")
    assert releido.emisor.id is not None
    assert dataclasses.replace(releido.emisor, id=None) == guardado.emisor
    assert releido.backup_s3_bucket == guardado.backup_s3_bucket
    assert releido.backup_s3_prefix == guardado.backup_s3_prefix
    assert releido.emisor.completo
    assert releido.emisor.punto_venta == 7  # se emite con el primero


def test_guardar_dos_veces_actualiza_el_mismo_emisor(conn):
    save_settings(conn, Settings(emisor=Emisor(razon_social="V1")))
    emisor_id = repo.list_emisores(conn, "homo")[0]["id"]
    set_active_emisor(conn, emisor_id)
    save_settings(
        conn,
        Settings(emisor=Emisor(id=emisor_id, razon_social="V2")),
    )
    assert load_settings(conn, "homo").emisor.razon_social == "V2"
    filas = conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0]
    assert filas == 1


def test_guardar_sin_id_crea_un_emisor_nuevo(conn):
    save_settings(conn, Settings(emisor=Emisor(razon_social="V1")))
    save_settings(conn, Settings(emisor=Emisor(razon_social="V2")))
    assert conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0] == 2


def test_cada_emisor_declara_su_ambiente(conn):
    """El emisor se guarda bajo el ambiente que él mismo declara: configurar
    el de homo no toca el de prod, así la numeración y los datos de prueba
    nunca se mezclan con los reales. La config de backups sí es global: el
    backup cubre la DB entera."""
    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social="PRUEBAS", ambiente="homo", puntos_venta=(9,)
            ),
            backup_s3_bucket="bucket-comun",
        ),
    )
    set_active_emisor(conn, repo.list_emisores(conn, "homo")[0]["id"])
    prod = load_settings(conn, "prod")
    assert prod.emisor == Emisor(ambiente="prod")   # prod sigue sin configurar
    assert prod.backup_s3_bucket == "bucket-comun"

    save_settings(
        conn,
        Settings(
            emisor=Emisor(
                razon_social="REAL S.R.L.", ambiente="prod", puntos_venta=(3,)
            )
        ),
    )
    set_active_emisor(conn, repo.list_emisores(conn, "prod")[0]["id"])
    assert load_settings(conn, "homo").emisor.razon_social == "PRUEBAS"
    assert load_settings(conn, "homo").emisor.punto_venta == 9
    assert load_settings(conn, "prod").emisor.punto_venta == 3


def test_un_ambiente_puede_tener_varios_emisores(conn):
    """Varios emisores en el mismo ambiente: sin selección explícita, ninguno
    opera; con active_emisor_id_<ambiente>, el elegido."""
    primero = repo.create_emisor(
        conn,
        dict.fromkeys(repo.EMISOR_FIELDS, "")
        | {"razon_social": "PRIMERO", "ambiente": "homo", "puntos_venta": "[1]"},
    )
    with conn:  # segundo emisor del mismo ambiente, insertado directo
        conn.execute(
            "INSERT INTO emisores (id, razon_social, ambiente, puntos_venta,"
            " created_at, updated_at)"
            " VALUES ('z-nuevo', 'SEGUNDO', 'homo', '[4]',"
            " '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00')"
        )
    assert not load_settings(conn, "homo").emisor.completo

    from facturador.settings import set_active_emisor

    set_active_emisor(conn, primero["id"])
    assert load_settings(conn, "homo").emisor.razon_social == "PRIMERO"
    set_active_emisor(conn, "z-nuevo")
    assert load_settings(conn, "homo").emisor.razon_social == "SEGUNDO"
    assert load_settings(conn, "homo").emisor.punto_venta == 4


@pytest.mark.parametrize(
    "corrupto", ["nueve", "[]", "[0]", '["7"]', "7", "[1.5]"]
)
def test_puntos_venta_corruptos_caen_al_default(conn, corrupto):
    save_settings(conn, Settings())
    emisor_id = repo.list_emisores(conn, "homo")[0]["id"]
    set_active_emisor(conn, emisor_id)
    with conn:
        conn.execute("UPDATE emisores SET puntos_venta = ?", (corrupto,))
    assert load_settings(conn, "homo").emisor.puntos_venta == (1,)


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

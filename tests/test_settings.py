"""Settings de dominio: viven en la DB. El emisor es una entidad (tabla
emisores) LOCAL al perfil (ADR 0001): la DB entera es de un solo
ambiente y la selección activa es una única clave, sin sufijo de ambiente."""

import dataclasses

import pytest

from facturador import db, repo
from facturador.settings import (
    ACTIVE_EMISOR_KEY,
    Emisor,
    Settings,
    get_active_emisor_id,
    load_settings,
    save_settings,
    set_active_emisor,
)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_defaults_sin_nada_guardado(conn):
    s = load_settings(conn)
    assert s == Settings()
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
            puntos_venta=(7, 2),
        ),
        backup_s3_bucket="mi-bucket",
        backup_s3_prefix="facturas",
    )
    save_settings(conn, guardado)
    emisor_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, emisor_id)
    releido = load_settings(conn)
    assert releido.emisor.id is not None
    assert dataclasses.replace(releido.emisor, id=None) == guardado.emisor
    assert releido.backup_s3_bucket == guardado.backup_s3_bucket
    assert releido.backup_s3_prefix == guardado.backup_s3_prefix
    assert releido.emisor.completo
    assert releido.emisor.punto_venta == 7  # se emite con el primero


def test_guardar_dos_veces_actualiza_el_mismo_emisor(conn):
    save_settings(conn, Settings(emisor=Emisor(razon_social="V1")))
    emisor_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, emisor_id)
    save_settings(
        conn,
        Settings(emisor=Emisor(id=emisor_id, razon_social="V2")),
    )
    assert load_settings(conn).emisor.razon_social == "V2"
    filas = conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0]
    assert filas == 1


def test_guardar_sin_id_crea_un_emisor_nuevo(conn):
    save_settings(conn, Settings(emisor=Emisor(razon_social="V1")))
    save_settings(conn, Settings(emisor=Emisor(razon_social="V2")))
    assert conn.execute("SELECT COUNT(*) FROM emisores").fetchone()[0] == 2


def test_la_seleccion_activa_es_una_unica_clave_del_perfil(conn):
    """Active_emisor_id sin sufijo de ambiente — elegir el emisor
    operativo no necesita saber el ambiente, el perfil YA lo es. No queda
    ninguna clave active_emisor_id_<ambiente>."""
    save_settings(conn, Settings(emisor=Emisor(razon_social="ÚNICO")))
    emisor_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, emisor_id)

    assert get_active_emisor_id(conn) == emisor_id
    claves = [
        row["key"]
        for row in conn.execute("SELECT key FROM settings").fetchall()
        if row["key"].startswith("active_emisor_id")
    ]
    assert claves == [ACTIVE_EMISOR_KEY]
    assert ACTIVE_EMISOR_KEY == "active_emisor_id"


def test_un_perfil_puede_tener_varios_emisores(conn):
    """Varios emisores en el mismo perfil (una sola identidad fiscal): sin
    selección explícita, ninguno opera; con active_emisor_id, el elegido."""
    primero = repo.create_emisor(
        conn,
        dict.fromkeys(repo.EMISOR_FIELDS, "")
        | {"razon_social": "PRIMERO", "ambiente": "homo", "puntos_venta": "[1]"},
    )
    with conn:  # segundo emisor del mismo perfil, insertado directo
        conn.execute(
            "INSERT INTO emisores (id, razon_social, ambiente, puntos_venta,"
            " created_at, updated_at)"
            " VALUES ('z-nuevo', 'SEGUNDO', 'homo', '[4]',"
            " '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00')"
        )
    assert not load_settings(conn).emisor.completo
    assert len(repo.list_emisores(conn)) == 2

    set_active_emisor(conn, primero["id"])
    assert load_settings(conn).emisor.razon_social == "PRIMERO"
    set_active_emisor(conn, "z-nuevo")
    assert load_settings(conn).emisor.razon_social == "SEGUNDO"
    assert load_settings(conn).emisor.punto_venta == 4


def test_la_config_de_backups_es_global_al_perfil(conn):
    save_settings(
        conn,
        Settings(
            emisor=Emisor(razon_social="PRUEBAS", puntos_venta=(9,)),
            backup_s3_bucket="bucket-comun",
        ),
    )
    s = load_settings(conn)
    assert s.backup_s3_bucket == "bucket-comun"
    # Sin selección activa no hay emisor operativo, aunque exista uno.
    assert s.emisor == Emisor()


@pytest.mark.parametrize(
    "corrupto", ["nueve", "[0]", '["7"]', "7", "[1.5]"]
)
def test_puntos_venta_corruptos_caen_al_default(conn, corrupto):
    save_settings(conn, Settings())
    emisor_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, emisor_id)
    with conn:
        conn.execute("UPDATE emisores SET puntos_venta = ?", (corrupto,))
    assert load_settings(conn).emisor.puntos_venta == (1,)


def test_puntos_venta_lista_vacia_queda_sin_pv(conn):
    """``[]`` significa sin PV (point_of_sale_required), no default 1."""
    save_settings(conn, Settings())
    emisor_id = repo.list_emisores(conn)[0]["id"]
    set_active_emisor(conn, emisor_id)
    with conn:
        conn.execute("UPDATE emisores SET puntos_venta = ?", ("[]",))
    assert load_settings(conn).emisor.puntos_venta == ()


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

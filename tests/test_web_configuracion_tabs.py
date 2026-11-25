"""Configuración en cuatro solapas: ``/configuracion?tab=<solapa>``.

El servidor decide qué solapa se muestra (anda sin JavaScript). Una ausente o
desconocida cae en Emisores, y cada acción vuelve a la solapa de su formulario.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from facturador.seed_backup_sync import SeedBackupStatus
from facturador.service import InvoiceService, ServiceError
from facturador.web.constants import (
    ANCLA_BACKUPS,
    TITULOS_TAB_CONFIGURACION,
    AvisoConfiguracion,
    ErrorConfiguracion,
    TabConfiguracion,
)
from facturador.web.routes.configuracion import url_configuracion
from tests.conftest import with_csrf

EMISOR_FORM = {
    "razon_social": "TABS S.R.L.",
    "domicilio": "Calle Falsa 123, CABA",
    "iibb": "Exento",
    "inicio_actividades": "01/08/2020",
    "condicion_iva": "IVA Responsable Inscripto",
    "puntos_venta": "1",
}

KEY_A = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"

# Un trozo de HTML que solo existe en la solapa indicada.
MARCAS: dict[TabConfiguracion, str] = {
    TabConfiguracion.EMISORES: "Emisores registrados",
    TabConfiguracion.BACKUP: "Hacer backup ahora",
    TabConfiguracion.MAQUINAS: 'action="/ui/configuracion/backup/maquinas"',
    TabConfiguracion.S3: 'name="backup_s3_bucket"',
}

_ACTIVA = re.compile(
    r'<a href="/configuracion\?tab=(\w+)[^"]*"\s+aria-current="page"', re.S
)


def _pagina(api, tab: str | None = None, extra: str = ""):
    query = f"?tab={tab}{extra}" if tab is not None else ""
    r = api.get(f"/configuracion{query}")
    assert r.status_code == 200
    return r


def _activas(texto: str) -> list[str]:
    return _ACTIVA.findall(texto)


def _preparar_backup(api, monkeypatch):
    """Con la lista de máquinas no vacía se ofrece "Hacer backup ahora"."""
    maquina = SimpleNamespace(
        nombre="PC", clave=KEY_A, clave_corta="age1ql3z…", clave_larga=KEY_A
    )
    monkeypatch.setattr(InvoiceService, "list_backup_machines", lambda self: [maquina])
    monkeypatch.setattr(InvoiceService, "machine_public_key", lambda self: KEY_A)


def test_hay_cuatro_solapas_con_su_titulo():
    assert list(TITULOS_TAB_CONFIGURACION) == list(TabConfiguracion)
    assert [t.value for t in TabConfiguracion] == [
        "emisores",
        "backup",
        "maquinas",
        "s3",
    ]


@pytest.mark.parametrize("tab", list(TabConfiguracion))
def test_cada_solapa_muestra_solo_su_contenido(api, monkeypatch, tab):
    _preparar_backup(api, monkeypatch)
    texto = _pagina(api, tab).text
    assert MARCAS[tab] in texto
    for otra, marca in MARCAS.items():
        if otra is not tab:
            assert marca not in texto, f"{otra} se filtra en {tab}"
    assert _activas(texto) == [tab.value]
    for titulo in TITULOS_TAB_CONFIGURACION.values():
        assert titulo in texto


def test_contenido_de_cada_solapa(api):
    emisores = _pagina(api, TabConfiguracion.EMISORES).text
    assert "Nuevo emisor" in emisores
    assert "Emisores registrados" in emisores
    assert "Bucket S3" not in emisores

    backup = _pagina(api, TabConfiguracion.BACKUP).text
    assert f'id="{ANCLA_BACKUPS}"' in backup
    assert '<h3 class="seccion">Estado</h3>' in backup
    assert '<h3 class="seccion">Esta máquina</h3>' in backup
    assert "Nuevo emisor" not in backup

    maquinas = _pagina(api, TabConfiguracion.MAQUINAS).text
    assert "Máquinas con acceso al backup" in maquinas
    assert 'id="maquina_clave"' in maquinas
    assert f'id="{ANCLA_BACKUPS}"' not in maquinas

    s3 = _pagina(api, TabConfiguracion.S3).text
    assert "Prefijo en el bucket" in s3
    assert "Guardar backups" in s3
    assert 'id="maquina_clave"' not in s3


@pytest.mark.parametrize(
    "tab",
    [None, "", "desconocida", "BACKUP", "Backup ", "../backup", "<script>x</script>"],
)
def test_tab_ausente_o_invalida_cae_en_emisores(api, tab):
    texto = _pagina(api, tab).text
    assert MARCAS[TabConfiguracion.EMISORES] in texto
    assert _activas(texto) == [TabConfiguracion.EMISORES.value]
    for otra in (TabConfiguracion.BACKUP, TabConfiguracion.S3):
        assert MARCAS[otra] not in texto
    assert "<script>x</script>" not in texto


@pytest.mark.parametrize("extra", ["", "&tab=backup", "&tab=basura"])
def test_editar_un_emisor_abre_emisores_con_el_formulario_cargado(api, extra):
    emisor_id = api.conn.execute("SELECT id FROM emisores LIMIT 1").fetchone()["id"]
    texto = api.get(f"/configuracion?edit={emisor_id}{extra}").text
    assert "Editar emisor" in texto
    assert f'name="emisor_id" value="{emisor_id}"' in texto
    assert _activas(texto) == [TabConfiguracion.EMISORES.value]


def test_el_link_de_editar_conserva_la_solapa(api):
    emisor_id = api.conn.execute("SELECT id FROM emisores LIMIT 1").fetchone()["id"]
    texto = _pagina(api, TabConfiguracion.EMISORES).text
    assert f"/configuracion?tab=emisores&amp;edit={emisor_id}" in texto
    assert "Cancelar edición" not in texto
    editando = api.get(f"/configuracion?tab=emisores&edit={emisor_id}").text
    assert 'href="/configuracion?tab=emisores"' in editando
    assert "Cancelar edición" in editando


def test_edit_inexistente_respeta_la_solapa_pedida(api):
    texto = api.get("/configuracion?tab=s3&edit=no-existe").text
    assert MARCAS[TabConfiguracion.S3] in texto
    assert "Editar emisor" not in texto


def test_el_ancla_vieja_de_backups_lleva_a_la_solapa_backup(api):
    """El servidor no ve el fragmento: fuera de Backup, un script manda a
    ``?tab=backup#backups``; en Backup el ancla existe y no hay salto."""
    destino = url_configuracion(TabConfiguracion.BACKUP, ancla=True)
    assert destino == f"/configuracion?tab=backup#{ANCLA_BACKUPS}"
    fuera = _pagina(api).text
    assert f"location.replace({destino!r}".replace("'", '"') in fuera
    assert f'id="{ANCLA_BACKUPS}"' not in fuera
    dentro = _pagina(api, TabConfiguracion.BACKUP).text
    assert f'id="{ANCLA_BACKUPS}"' in dentro


def test_url_configuracion_arma_la_url_solo_con_enums():
    assert url_configuracion(TabConfiguracion.S3) == "/configuracion?tab=s3"
    assert (
        url_configuracion(TabConfiguracion.MAQUINAS, aviso=AvisoConfiguracion.MAQUINA)
        == "/configuracion?tab=maquinas&aviso=maquina"
    )
    assert (
        url_configuracion(
            TabConfiguracion.BACKUP, error=ErrorConfiguracion.BACKUP, ancla=True
        )
        == f"/configuracion?tab=backup&error=backup#{ANCLA_BACKUPS}"
    )
    # El ancla es solo de la tarjeta de Backup.
    assert "#" not in url_configuracion(TabConfiguracion.S3, ancla=True)


# --- los redirects vuelven a la solapa de origen ---


def _post(api, path, data=None):
    return api.post(path, data=with_csrf(api, data), follow_redirects=False)


def _ubicacion(r) -> str:
    assert r.status_code == 303
    return r.headers["location"]


def test_guardar_emisor_vuelve_a_emisores(api):
    r = _post(api, "/ui/emisores", EMISOR_FORM)
    destino = _ubicacion(r)
    assert destino == "/configuracion?tab=emisores&aviso=guardado"
    texto = api.get(destino).text
    assert "Emisor guardado." in texto
    assert "TABS S.R.L." in texto
    assert _activas(texto) == [TabConfiguracion.EMISORES.value]


def test_activar_emisor_vuelve_a_emisores(api):
    _post(api, "/ui/emisores", EMISOR_FORM)
    nuevo = api.conn.execute(
        "SELECT id FROM emisores WHERE razon_social = ?", ("TABS S.R.L.",)
    ).fetchone()["id"]
    destino = _ubicacion(_post(api, f"/ui/emisores/{nuevo}/activar"))
    assert destino == "/configuracion?tab=emisores&aviso=activado"
    # Un emisor que no existe tampoco saca de la solapa.
    r = _post(api, "/ui/emisores/no-existe/activar")
    assert _ubicacion(r) == "/configuracion?tab=emisores"


def test_guardar_s3_vuelve_a_s3(api):
    r = _post(
        api,
        "/ui/configuracion/backup",
        {"backup_s3_bucket": "mi-bucket", "backup_s3_prefix": "facturador"},
    )
    destino = _ubicacion(r)
    assert destino == "/configuracion?tab=s3&aviso=backup"
    texto = api.get(destino).text
    assert "Configuración de backups guardada." in texto
    assert 'value="mi-bucket"' in texto
    assert _activas(texto) == [TabConfiguracion.S3.value]


@pytest.mark.parametrize(
    ("path", "metodo", "datos", "esperado"),
    [
        (
            "/ui/configuracion/backup/identidad",
            "create_machine_identity",
            {},
            f"/configuracion?tab=backup&aviso=identidad#{ANCLA_BACKUPS}",
        ),
        (
            "/ui/configuracion/backup/esta-maquina",
            "add_this_machine",
            {},
            f"/configuracion?tab=backup&aviso=maquina#{ANCLA_BACKUPS}",
        ),
        (
            "/ui/configuracion/backup/maquinas",
            "add_backup_machine",
            {"nombre": "PC", "clave": KEY_A},
            "/configuracion?tab=maquinas&aviso=maquina",
        ),
        (
            "/ui/configuracion/backup/maquinas/quitar",
            "remove_backup_machine",
            {"clave": KEY_A},
            "/configuracion?tab=maquinas&aviso=maquina_quitada",
        ),
    ],
)
def test_acciones_de_backup_vuelven_a_su_solapa(
    api, monkeypatch, path, metodo, datos, esperado
):
    monkeypatch.setattr(InvoiceService, metodo, lambda self, *args: None)
    assert _ubicacion(_post(api, path, datos)) == esperado


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (
            SeedBackupStatus.OK,
            f"/configuracion?tab=backup&aviso=backup_hecho#{ANCLA_BACKUPS}",
        ),
        (
            SeedBackupStatus.FAILED,
            f"/configuracion?tab=backup&error=backup#{ANCLA_BACKUPS}",
        ),
    ],
)
def test_backup_ahora_vuelve_a_backup(api, monkeypatch, estado, esperado):
    monkeypatch.setattr(
        InvoiceService,
        "run_seed_backup_now",
        lambda self: SimpleNamespace(status=estado),
    )
    destino = _ubicacion(_post(api, "/ui/configuracion/backup/ahora"))
    assert destino == esperado
    assert _activas(api.get(destino).text) == [TabConfiguracion.BACKUP.value]


def test_el_tab_del_request_no_cambia_el_destino_del_redirect(api):
    """Los redirects salen de constantes: un ``tab`` en la URL del POST no
    se usa para armarlos."""
    r = api.post(
        "/ui/emisores?tab=https://evil.example",
        data=with_csrf(api, EMISOR_FORM),
        follow_redirects=False,
    )
    assert _ubicacion(r) == "/configuracion?tab=emisores&aviso=guardado"


# --- los errores también muestran la solapa de origen ---


def test_error_de_emisor_se_ve_en_emisores(api):
    r = _post(api, "/ui/emisores", {**EMISOR_FORM, "razon_social": ""})
    assert r.status_code == 422
    assert "Datos inválidos" in r.text
    assert _activas(r.text) == [TabConfiguracion.EMISORES.value]


def test_error_al_agregar_maquina_se_ve_en_maquinas(api):
    r = _post(
        api,
        "/ui/configuracion/backup/maquinas",
        {"nombre": "Mi PC", "clave": "age1nolaes"},
    )
    assert r.status_code == 422
    assert _activas(r.text) == [TabConfiguracion.MAQUINAS.value]
    assert 'value="age1nolaes"' in r.text
    assert 'value="Mi PC"' in r.text


def test_error_al_quitar_maquina_se_ve_en_maquinas(api):
    r = _post(api, "/ui/configuracion/backup/maquinas/quitar", {"clave": KEY_A})
    assert r.status_code == 422
    assert _activas(r.text) == [TabConfiguracion.MAQUINAS.value]


def test_error_de_identidad_se_ve_en_backup(api, monkeypatch):
    def falla(self):
        raise ServiceError("sin age")

    monkeypatch.setattr(InvoiceService, "create_machine_identity", falla)
    r = _post(api, "/ui/configuracion/backup/identidad")
    assert r.status_code == 422
    assert "sin age" in r.text
    assert _activas(r.text) == [TabConfiguracion.BACKUP.value]


# --- el formulario de emisor es colapsable y queda debajo de la lista ---------


def _detalles_emisor(html: str) -> str:
    """Etiqueta de apertura del <details> del formulario de emisor."""
    m = re.search(r'<details[^>]*id="form-emisor"[^>]*>', html)
    assert m, "falta el formulario colapsable de emisor"
    return m.group(0)


def test_el_formulario_de_emisor_va_debajo_de_la_lista_y_colapsado(api):
    texto = _pagina(api, TabConfiguracion.EMISORES).text
    assert texto.index("Emisores registrados") < texto.index('id="form-emisor"')
    assert " open" not in _detalles_emisor(texto)
    assert "Nuevo emisor" in texto


def test_editar_abre_el_formulario_de_emisor(api):
    emisor_id = api.conn.execute("SELECT id FROM emisores LIMIT 1").fetchone()["id"]
    texto = api.get(f"/configuracion?tab=emisores&edit={emisor_id}").text
    assert " open" in _detalles_emisor(texto)


def test_un_error_de_validacion_deja_abierto_el_formulario_de_emisor(api):
    r = _post(api, "/ui/emisores", {**EMISOR_FORM, "razon_social": ""})
    assert r.status_code == 422
    assert " open" in _detalles_emisor(r.text)


def test_guardar_un_emisor_nuevo_vuelve_con_el_formulario_cerrado(api):
    r = _post(api, "/ui/emisores", EMISOR_FORM)
    assert r.status_code == 303
    texto = api.get(r.headers["location"]).text
    assert " open" not in _detalles_emisor(texto)

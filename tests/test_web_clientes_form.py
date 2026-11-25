"""Pantalla Clientes: la tabla va primero y el formulario es colapsable.

El formulario es un ``<details>`` debajo de la tabla: cerrado por defecto,
abierto al editar un cliente o cuando la validación falla.
"""

from __future__ import annotations

import re

from tests.conftest import with_csrf

RUTA_CLIENTES = "/clientes"
RUTA_ALTA = "/ui/clientes"
STATUS_REDIRECT = 303
STATUS_INVALIDO = 422
RAZON_SOCIAL = "CLIENTE URUGUAY S.A."
PAIS_INEXISTENTE = "999"
ID_FORM = "form-cliente"
TITULO_NUEVO = "Nuevo cliente"
TITULO_EDITAR = "Editar cliente"
TITULO_TABLA = "<h2>Clientes</h2>"

CLIENTE_FORM = {
    "razon_social": RAZON_SOCIAL,
    "domicilio": "Av. Siempreviva 123, Montevideo",
    "pais_dst": "225",
    "cuit_pais": "55000002002",
    "id_impositivo": "RUT 219999830019",
    "moneda_default": "DOL",
    "idioma_default": "1",
    "forma_pago_default": "WIRE TRANSFER",
    "descripcion_default": "Servicios de desarrollo de software",
}

_DETAILS = re.compile(rf"<details\b[^>]*\bid=\"{ID_FORM}\"[^>]*>")


def _details_formulario(html: str) -> str:
    """Etiqueta de apertura del ``<details>`` del formulario (una sola)."""
    encontrados = _DETAILS.findall(html)
    assert len(encontrados) == 1, html
    return encontrados[0]


def _esta_abierto(html: str) -> bool:
    return re.search(r"\sopen\b", _details_formulario(html)) is not None


def _crear_cliente(api) -> str:
    r = api.post(RUTA_ALTA, data=with_csrf(api, CLIENTE_FORM), follow_redirects=False)
    assert r.status_code == STATUS_REDIRECT, r.text
    return api.get("/clients").json()[0]["id"]


def test_formulario_va_debajo_de_la_tabla(api):
    _crear_cliente(api)
    html = api.get(RUTA_CLIENTES).text

    assert html.index(TITULO_TABLA) < html.index(f'id="{ID_FORM}"')
    assert html.index(RAZON_SOCIAL) < html.index(f'id="{ID_FORM}"')


def test_formulario_va_debajo_de_la_tabla_sin_clientes(api):
    html = api.get(RUTA_CLIENTES).text

    assert html.index(TITULO_TABLA) < html.index(f'id="{ID_FORM}"')


def test_formulario_cerrado_por_defecto(api):
    html = api.get(RUTA_CLIENTES).text

    assert not _esta_abierto(html)
    assert TITULO_NUEVO in html
    assert TITULO_EDITAR not in html


def test_formulario_abierto_al_editar_con_datos_cargados(api):
    cliente_id = _crear_cliente(api)
    html = api.get(f"{RUTA_CLIENTES}?edit={cliente_id}").text

    assert _esta_abierto(html)
    assert TITULO_EDITAR in html
    assert f'value="{RAZON_SOCIAL}"' in html
    assert f'name="client_id" value="{cliente_id}"' in html


def test_formulario_abierto_con_error_de_validacion(api):
    r = api.post(
        RUTA_ALTA,
        data=with_csrf(api, {**CLIENTE_FORM, "pais_dst": PAIS_INEXISTENTE}),
    )

    assert r.status_code == STATUS_INVALIDO
    assert "pais_dst" in r.text
    assert _esta_abierto(r.text)
    assert r.text.index(TITULO_TABLA) < r.text.index(f'id="{ID_FORM}"')


def test_guardar_cliente_nuevo_deja_el_formulario_cerrado(api):
    r = api.post(RUTA_ALTA, data=with_csrf(api, CLIENTE_FORM), follow_redirects=True)

    assert r.status_code == 200
    assert RAZON_SOCIAL in r.text
    assert not _esta_abierto(r.text)


def test_formulario_conserva_cuenta_recaudadora(api):
    cliente_id = _crear_cliente(api)
    api.post(
        RUTA_ALTA,
        data=with_csrf(
            api,
            {
                **CLIENTE_FORM,
                "client_id": cliente_id,
                "cuenta_recaudadora": "PLATAFORMA X",
            },
        ),
        follow_redirects=False,
    )

    html = api.get(f"{RUTA_CLIENTES}?edit={cliente_id}").text

    assert 'name="cuenta_recaudadora"' in html
    assert 'value="PLATAFORMA X"' in html

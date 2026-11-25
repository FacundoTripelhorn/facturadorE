"""Pantalla "Orden de pago" del menú: primero el cliente, después sus
facturas autorizadas. Comprobantes queda solo de consulta."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from facturador.orden_pago import RutaOrdenPago
from tests.test_orden_pago import (
    OTRO_CLIENTE,
    _autorizada,
    _client_id,
    _como_reconstruida,
)
from tests.test_web import _crear_cliente_por_form, _generar_borrador

NUEVA = RutaOrdenPago.NUEVA.value
CLIENTE_A = "CLIENTE URUGUAY S.A."
CLIENTE_B = OTRO_CLIENTE["razon_social"]


def _nav(html: str) -> str:
    return html[html.index('<nav class="principal"') : html.index("</nav>")]


def _checkboxes(html: str) -> list[str]:
    """Ids de las facturas con casilla en la página."""
    return re.findall(r'<input type="checkbox" name="ids" value="([^"]+)"', html)


def _marcadas(html: str) -> list[str]:
    return [
        m.group(1)
        for m in re.finditer(
            r'<input type="checkbox" name="ids" value="([^"]+)"[^>]*\schecked', html
        )
    ]


@dataclass
class DosClientes:
    a: str  # id del cliente A
    b: str  # id del cliente B
    facturas_a: list[str]  # autorizadas de A
    factura_b: str  # autorizada de B
    borrador_a: str  # borrador de A (no entra en una orden)


@pytest.fixture
def dos_clientes(api, arca) -> DosClientes:
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE, id_impositivo="RUT 2222")
    a = _client_id(api, CLIENTE_A)
    b = _client_id(api, CLIENTE_B)
    facturas_a = [_autorizada(api, client_id=a), _autorizada(api, client_id=a)]
    factura_b = _autorizada(api, client_id=b)
    borrador_a = _generar_borrador(api, client_id=a)
    return DosClientes(a, b, facturas_a, factura_b, borrador_a)


# --- menú ---------------------------------------------------------------------


def test_menu_principal_tiene_la_opcion_orden_de_pago(api):
    nav = _nav(api.get("/").text)
    assert f'href="{NUEVA}"' in nav
    assert ">Orden de pago</a>" in nav
    # Junto a Emitir, Comprobantes y Constatar CAE.
    for ruta in ("/", "/comprobantes", "/constatacion"):
        assert f'href="{ruta}"' in nav


def test_la_opcion_del_menu_esta_activa_en_la_pantalla(api, dos_clientes):
    en_orden = _nav(api.get(NUEVA).text)
    assert re.search(
        rf'href="{NUEVA}"\s+aria-current="page">Orden de pago</a>', en_orden
    )
    assert en_orden.count("aria-current") == 1

    # También en el resultado, que es parte del mismo flujo.
    ids = "&".join(f"ids={i}" for i in dos_clientes.facturas_a)
    resultado = _nav(api.get(f"/orden-de-pago?{ids}").text)
    assert 'aria-current="page">Orden de pago</a>' in resultado

    for otra in ("/", "/comprobantes", "/constatacion"):
        assert not re.search(
            rf'href="{NUEVA}"\s+aria-current', _nav(api.get(otra).text)
        )


# --- paso 1: selector de cliente ----------------------------------------------


def test_paso_uno_ofrece_los_clientes_y_no_lista_facturas(api, dos_clientes):
    r = api.get(NUEVA)

    assert r.status_code == 200
    html = r.text
    assert f'<form class="cuerpo" method="get" action="{NUEVA}">' in html
    assert '<select id="cliente" name="cliente"' in html
    assert f'<option value="{dos_clientes.b}">{CLIENTE_B}</option>' in html
    # El cliente por defecto llega elegido; las facturas esperan al clic.
    assert f'<option value="{dos_clientes.a}" selected>{CLIENTE_A}</option>' in html
    assert '<option value="">Elegí un cliente</option>' in html
    assert html.count(" selected>") == 1
    assert _checkboxes(html) == []
    assert "Armar orden de pago" not in html


def test_paso_uno_sin_cliente_por_defecto_pide_elegir(api, dos_clientes):
    api.conn.execute("UPDATE clients SET is_default = 0")
    api.conn.commit()

    html = api.get(NUEVA).text

    assert '<option value="" selected>Elegí un cliente</option>' in html
    assert html.count(" selected>") == 1
    assert _checkboxes(html) == []


def test_el_cliente_elegido_gana_sobre_el_por_defecto(api, dos_clientes):
    html = api.get(f"{NUEVA}?cliente={dos_clientes.b}").text

    assert f'<option value="{dos_clientes.b}" selected>{CLIENTE_B}</option>' in html
    assert f'<option value="{dos_clientes.a}">{CLIENTE_A}</option>' in html
    assert html.count(" selected>") == 1
    assert _checkboxes(html) == [dos_clientes.factura_b]


def test_sin_clientes_invita_a_crear_uno(api):
    html = api.get(NUEVA).text
    assert "No hay clientes cargados" in html
    assert 'href="/clientes"' in html
    assert "<select" not in html


# --- paso 2: facturas del cliente ---------------------------------------------


def test_paso_dos_lista_solo_las_autorizadas_del_cliente(api, dos_clientes):
    r = api.get(f"{NUEVA}?cliente={dos_clientes.a}")

    assert r.status_code == 200
    html = r.text
    # Más nueva primero: las dos de A, sin la de B ni el borrador.
    assert _checkboxes(html) == list(reversed(dos_clientes.facturas_a))
    assert dos_clientes.factura_b not in html
    assert dos_clientes.borrador_a not in html
    assert f'<option value="{dos_clientes.a}" selected>{CLIENTE_A}</option>' in html
    assert 'action="/orden-de-pago" id="form-orden"' in html
    assert 'id="armar-orden" disabled' in html  # nada marcado todavía
    assert "Armar orden de pago (" in html


def test_cada_cliente_ve_solo_sus_facturas(api, dos_clientes):
    html = api.get(f"{NUEVA}?cliente={dos_clientes.b}").text
    assert _checkboxes(html) == [dos_clientes.factura_b]


def test_cliente_sin_autorizadas_lo_dice_y_no_ofrece_armar(api, arca):
    _crear_cliente_por_form(api)
    _generar_borrador(api)
    cliente = api.get("/clients").json()[0]["id"]

    html = api.get(f"{NUEVA}?cliente={cliente}").text

    assert "todavía no tiene facturas autorizadas" in html
    assert _checkboxes(html) == []
    assert "armar-orden" not in html


def test_reconstruida_del_mismo_receptor_aparece_con_su_cliente(api, arca):
    """Las reconstruidas desde ARCA no tienen client_id: entran por receptor."""
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE, id_impositivo="RUT 2222")
    a, b = _client_id(api, CLIENTE_A), _client_id(api, CLIENTE_B)
    de_a = _autorizada(api, client_id=a)
    nueva_de_a = _autorizada(api, client_id=a)
    de_b = _autorizada(api, client_id=b)
    _como_reconstruida(api, de_a)

    en_a = _checkboxes(api.get(f"{NUEVA}?cliente={a}").text)
    en_b = _checkboxes(api.get(f"{NUEVA}?cliente={b}").text)

    assert sorted(en_a) == sorted([de_a, nueva_de_a])
    assert en_b == [de_b]


def test_cada_casilla_lleva_su_cliente_y_su_receptor_para_validar_la_mezcla(api, arca):
    """El script de la pantalla replica ``mismo_cliente`` con estos datos: si
    cambia el ID impositivo del cliente, las reconstruidas con la identidad
    vieja y las nuevas se listan juntas pero no se pueden unir."""
    _crear_cliente_por_form(api)
    a = _client_id(api, CLIENTE_A)
    vieja = _autorizada(api, client_id=a)
    nueva = _autorizada(api, client_id=a)
    _como_reconstruida(api, vieja)
    api.conn.execute(
        "UPDATE invoices SET id_impositivo = 'RUT CAMBIADO' WHERE id = ?", (nueva,)
    )
    api.conn.commit()

    html = api.get(f"{NUEVA}?cliente={a}").text
    atributos = {
        i: (c, r)
        for i, c, r in re.findall(
            r'name="ids" value="([^"]+)"\s+data-cliente="([^"]*)"\s+'
            r'data-receptor="([^"]*)"',
            html,
        )
    }

    assert set(atributos) == {vieja, nueva}
    assert atributos[vieja][0] == ""  # reconstruida: sin client_id
    assert atributos[nueva][0] == a
    assert atributos[vieja][1] != atributos[nueva][1]  # receptores distintos
    # El backend rechaza esa combinación: la pantalla la avisa antes.
    assert "Elegí facturas de un solo cliente" in html
    assert api.get(f"/orden-de-pago?ids={vieja}&ids={nueva}").status_code == 422


def test_reconstruida_de_otro_receptor_no_aparece(api, arca):
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE, id_impositivo="RUT 2222")
    a, b = _client_id(api, CLIENTE_A), _client_id(api, CLIENTE_B)
    de_a = _autorizada(api, client_id=a)
    de_b = _autorizada(api, client_id=b)
    _como_reconstruida(api, de_b)

    assert _checkboxes(api.get(f"{NUEVA}?cliente={a}").text) == [de_a]
    assert _checkboxes(api.get(f"{NUEVA}?cliente={b}").text) == [de_b]


# --- el parámetro de cliente es entrada no confiable --------------------------


@pytest.mark.parametrize(
    "valor",
    [
        "no-existe",
        "<script>alert(1)</script>",
        "' OR '1'='1",
        "https://ejemplo.invalid/",
        "//ejemplo.invalid",
    ],
)
def test_cliente_inexistente_se_rechaza_sin_ecoarlo(api, dos_clientes, valor):
    r = api.get(NUEVA, params={"cliente": valor}, follow_redirects=False)

    assert r.status_code == 422
    assert "El cliente elegido no existe" in r.text
    assert valor not in r.text
    assert "ejemplo.invalid" not in r.text
    assert _checkboxes(r.text) == []
    assert "location" not in r.headers  # nunca es destino de un redirect


def test_cliente_vacio_es_el_paso_uno_sin_error(api, dos_clientes):
    r = api.get(f"{NUEVA}?cliente=")
    assert r.status_code == 200
    assert "El cliente elegido no existe" not in r.text
    assert _checkboxes(r.text) == []


# --- de la pantalla al resultado ----------------------------------------------


def test_se_arma_una_orden_de_principio_a_fin(api, dos_clientes):
    html = api.get(f"{NUEVA}?cliente={dos_clientes.a}").text
    elegidas = _checkboxes(html)
    assert len(elegidas) == 2

    # Lo que manda el formulario: GET /orden-de-pago?ids=…
    r = api.get("/orden-de-pago", params={"ids": elegidas})

    assert r.status_code == 200, r.text
    assert CLIENTE_A in r.text
    assert "USD 3.000,00" in r.text
    assert 'aria-label="Unir facturas E"' in r.text


def test_el_backend_sigue_rechazando_clientes_distintos(api, dos_clientes):
    """Aunque la pantalla no lo permite, un pedido armado a mano se rechaza."""
    mezcla = [dos_clientes.facturas_a[0], dos_clientes.factura_b]

    pantalla = api.get("/orden-de-pago", params={"ids": mezcla})
    facturas_e = api.get("/orden-de-pago/facturas-e", params={"ids": mezcla})

    assert pantalla.status_code == 422
    assert "Elegí facturas de un solo cliente" in pantalla.text
    assert facturas_e.status_code == 422
    assert facturas_e.json() == {"detail": "Elegí facturas de un solo cliente."}


def test_el_resultado_enlaza_a_cambiar_las_facturas(api, dos_clientes):
    ids = dos_clientes.facturas_a
    r = api.get("/orden-de-pago", params={"ids": ids})

    assert "Cambiar las facturas de la orden" in r.text
    enlace = re.search(r'href="(/orden-de-pago/nueva\?[^"]+)"', r.text)
    assert enlace is not None
    href = enlace.group(1).replace("&amp;", "&")
    assert f"cliente={dos_clientes.a}" in href

    # Al volver, la pantalla ya viene con las facturas marcadas.
    vuelta = api.get(href)
    assert vuelta.status_code == 200
    assert sorted(_marcadas(vuelta.text)) == sorted(ids)
    assert "Armar orden de pago (<span" in vuelta.text
    assert 'id="armar-orden" disabled' not in vuelta.text


def _enlace_de_vuelta(html: str) -> str | None:
    enlace = re.search(r'href="(/orden-de-pago/nueva[^"]*)"[^>]*>Cambiar', html)
    return enlace.group(1).replace("&amp;", "&") if enlace else None


def test_volver_de_una_orden_toda_reconstruida_conserva_cliente_y_seleccion(
    api, dos_clientes
):
    """Sin client_id en ninguna, el cliente se resuelve por su receptor."""
    ids = dos_clientes.facturas_a
    for factura in ids:
        _como_reconstruida(api, factura)

    r = api.get("/orden-de-pago", params={"ids": ids})

    assert r.status_code == 200
    href = _enlace_de_vuelta(r.text)
    assert href is not None
    assert f"cliente={dos_clientes.a}" in href
    vuelta = api.get(href)
    assert vuelta.status_code == 200
    assert sorted(_marcadas(vuelta.text)) == sorted(ids)


def test_volver_de_una_orden_reconstruida_sin_cliente_conocido_va_al_paso_uno(
    api, dos_clientes
):
    """Si ningún cliente tiene ese receptor (cambió su ID impositivo), no hay
    a quién volver: se vuelve al paso 1 sin inventar uno."""
    ids = dos_clientes.facturas_a
    for factura in ids:
        _como_reconstruida(api, factura)
    api.conn.execute(
        "UPDATE clients SET id_impositivo = 'RUT NUEVO' WHERE id = ?",
        (dos_clientes.a,),
    )
    api.conn.commit()

    href = _enlace_de_vuelta(api.get("/orden-de-pago", params={"ids": ids}).text)

    assert href == NUEVA
    assert api.get(href).status_code == 200


def test_error_del_resultado_vuelve_a_la_pantalla_nueva(api, dos_clientes):
    r = api.get("/orden-de-pago")
    assert r.status_code == 422
    assert f'href="{NUEVA}"' in r.text
    assert "/comprobantes?estado=autorizadas" not in r.text


def test_preseleccion_ignora_facturas_ajenas_al_cliente(api, dos_clientes):
    propia = dos_clientes.facturas_a[0]
    ajena = dos_clientes.factura_b

    html = api.get(
        f"{NUEVA}?cliente={dos_clientes.a}&ids={propia}&ids={ajena}&ids=no-existe"
    ).text

    assert _marcadas(html) == [propia]
    assert ajena not in html
    assert "no-existe" not in html


# --- Comprobantes: solo consulta ----------------------------------------------


@pytest.mark.parametrize("estado", ["todas", "borradores", "autorizadas", "atencion"])
def test_comprobantes_ya_no_tiene_casillas_ni_boton_de_orden(api, dos_clientes, estado):
    html = api.get(f"/comprobantes?estado={estado}").text

    assert 'type="checkbox"' not in html
    assert "Armar orden de pago" not in html
    assert "Elegir para la orden de pago" not in html
    for resto in ("armar-orden", "form-orden", "conteo-orden", "col-elegir"):
        assert resto not in html


def test_comprobantes_autorizadas_sigue_listando_las_facturas(api, dos_clientes):
    html = api.get("/comprobantes?estado=autorizadas").text
    for invoice_id in (*dos_clientes.facturas_a, dos_clientes.factura_b):
        assert f'href="/facturas/{invoice_id}"' in html
        assert f"/invoices/{invoice_id}/pdf?descargar=1" in html
    assert dos_clientes.borrador_a not in html

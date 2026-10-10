"""Resumen de facturación: totales del año por mes, cliente y moneda.

Solo cuentan los autorizados del ambiente activo, cada importe en su moneda
original; nunca se suman monedas distintas.
"""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

from facturador import repo
from facturador.constants import InvoiceStatus
from facturador.resumen import resumir
from facturador.web.grafico import grafico_por_mes

_NUMEROS = itertools.count(1)


def _comprobante(
    conn,
    *,
    fecha: str,
    importe: str,
    moneda: str = "DOL",
    cliente: str = "CLIENTE URUGUAY S.A.",
    client_id: str | None = None,
    status: InvoiceStatus = InvoiceStatus.AUTHORIZED,
    environment: str = "homo",
) -> None:
    data = {f: "" for f in repo.INVOICE_FIELDS}
    data.update(
        emisor_id=None,
        client_id=client_id,
        cbte_tipo=19,
        punto_venta=1,
        fecha_cbte=fecha,
        fecha_pago=fecha,
        tipo_expo=2,
        dst_cmp=225,
        cliente=cliente,
        cuit_pais_cliente=55000002002,
        moneda_id=moneda,
        moneda_ctz="1",
        idioma_cbte=1,
        imp_total=importe,
        pdf_render_version=1,
        environment=environment,
    )
    inv = repo.create_invoice(conn, data, [])
    if status == InvoiceStatus.AUTHORIZED:
        nro = next(_NUMEROS)
        repo.update_invoice(
            conn, inv["id"], status=status, cbte_nro=nro, cae=f"{nro:014d}"
        )
    elif status != InvoiceStatus.DRAFT:
        repo.update_invoice(conn, inv["id"], status=status)


def _fila(fecha, importe, moneda="DOL", cliente="A", client_id=None):
    return {
        "fecha_cbte": fecha,
        "imp_total": importe,
        "moneda_id": moneda,
        "cliente": cliente,
        "client_id": client_id,
    }


# --- agregación ---


def test_resumir_suma_por_mes_y_moneda_sin_mezclar():
    r = resumir(
        2026,
        [
            _fila("20260105", "1000.00"),
            _fila("20260120", "500.50"),
            _fila("20260310", "200.00", moneda="060"),
            _fila("20260315", "300.00"),
        ],
    )
    assert [m.moneda for m in r.monedas] == ["DOL", "060"]  # más usada primero
    dol, eur = r.monedas
    assert dol.por_mes[0].importe == Decimal("1500.50")
    assert dol.por_mes[0].cantidad == 2
    assert dol.por_mes[2].importe == Decimal("300.00")
    assert dol.anual.importe == Decimal("1800.50") and dol.anual.cantidad == 3
    assert eur.por_mes[2].importe == Decimal("200.00")
    assert eur.anual.importe == Decimal("200.00") and eur.anual.cantidad == 1
    # Los meses sin comprobantes quedan en cero, sin sumar otras monedas.
    assert eur.por_mes[0].importe == 0 and eur.por_mes[0].cantidad == 0


def test_resumir_por_cliente_ordena_por_total_dentro_de_cada_moneda():
    r = resumir(
        2026,
        [
            _fila("20260101", "100.00", cliente="Chico", client_id="c1"),
            _fila("20260102", "900.00", cliente="Grande", client_id="c2"),
            _fila("20260103", "50.00", cliente="Chico", client_id="c1"),
            _fila("20260104", "70.00", moneda="060", cliente="Grande", client_id="c2"),
        ],
    )
    filas = [
        (f.cliente, f.moneda, f.total.importe, f.total.cantidad)
        for f in r.por_cliente
    ]
    assert filas == [
        ("Grande", "DOL", Decimal("900.00"), 1),
        ("Chico", "DOL", Decimal("150.00"), 2),
        ("Grande", "060", Decimal("70.00"), 1),
    ]


def test_resumir_agrupa_cliente_renombrado_y_usa_el_nombre_mas_reciente():
    r = resumir(
        2026,
        [
            _fila("20260101", "100.00", cliente="Viejo SA", client_id="c1"),
            _fila("20260601", "100.00", cliente="Nuevo SA", client_id="c1"),
            # Sin id (reconstruido desde ARCA): se agrupa por nombre.
            _fila("20260701", "10.00", cliente="Suelto"),
            _fila("20260801", "10.00", cliente="Suelto"),
        ],
    )
    assert [(f.cliente, f.total.cantidad) for f in r.por_cliente] == [
        ("Nuevo SA", 2),
        ("Suelto", 2),
    ]


def test_resumir_sin_comprobantes_esta_vacio():
    r = resumir(2026, [])
    assert r.vacio and r.monedas == [] and r.por_cliente == []


# --- gráfico ---


def test_grafico_barras_proporcionales_al_mes_mas_alto():
    r = resumir(2026, [_fila("20260201", "100.00"), _fila("20260501", "50.00")])
    g = grafico_por_mes(r.monedas[0].por_mes)
    assert len(g.barras) == 12
    feb, may = g.barras[1], g.barras[4]
    assert feb.alto == g.base - g.tope  # el máximo ocupa todo el alto útil
    assert may.alto == feb.alto / 2
    assert g.barras[0].alto == 0
    assert g.maximo == Decimal("100.00")
    assert [b.abrev for b in g.barras[:3]] == ["Ene", "Feb", "Mar"]


# --- consultas ---


def test_solo_autorizados_del_ambiente_y_del_anio(api):
    conn = api.conn
    _comprobante(conn, fecha="20260110", importe="100.00")
    _comprobante(conn, fecha="20251231", importe="1.00")
    _comprobante(conn, fecha="20260110", importe="2.00", status=InvoiceStatus.DRAFT)
    _comprobante(conn, fecha="20260110", importe="3.00", status=InvoiceStatus.REJECTED)
    _comprobante(conn, fecha="20260110", importe="4.00", status=InvoiceStatus.UNKNOWN)
    _comprobante(conn, fecha="20260110", importe="5.00", environment="prod")

    filas = repo.list_authorized_in_year(conn, "homo", 2026)
    assert [f["imp_total"] for f in filas] == ["100.00"]
    assert repo.authorized_years(conn, "homo") == [2026, 2025]
    assert repo.authorized_years(conn, "prod") == [2026]


# --- página ---


def test_pagina_resumen_totales_por_moneda(api):
    conn = api.conn
    _comprobante(conn, fecha="20260115", importe="1000.00", cliente="ACME LLC")
    _comprobante(conn, fecha="20260120", importe="500.00", cliente="ACME LLC")
    _comprobante(conn, fecha="20260305", importe="2500.00", cliente="Globex")
    _comprobante(
        conn, fecha="20260305", importe="800.00", moneda="060", cliente="Initech"
    )
    _comprobante(
        conn, fecha="20260305", importe="9999.00", status=InvoiceStatus.REJECTED
    )

    r = api.get("/comprobantes/resumen?anio=2026")
    assert r.status_code == 200
    html = r.text
    assert 'href="/comprobantes/resumen" aria-current="page"' in html
    # Total anual por moneda, separados: 4.000,00 USD y 800,00 en la otra.
    assert "4.000,00" in html and "800,00" in html
    assert "4.800,00" not in html  # nunca se suman monedas distintas
    assert "9.999,00" not in html  # rechazado no cuenta
    assert "1.500,00" in html  # enero
    assert html.count("<svg viewBox") == 2  # un gráfico por moneda
    assert "ACME LLC" in html and "Globex" in html and "Initech" in html
    assert html.index("Globex") < html.index("ACME LLC")  # ordenado por total


def test_pagina_resumen_vacia_y_anio_por_defecto(api):
    anio = dt.date.today().year
    r = api.get("/comprobantes/resumen")
    assert r.status_code == 200
    assert f"Todavía no hay comprobantes autorizados en {anio}." in r.text
    assert f'<option value="{anio}" selected>' in r.text
    # Un año inválido cae en el actual, incluidos dígitos no ASCII que
    # str.isdigit() acepta pero int() no.
    for invalido in ("xx", "20266", "²²²²"):
        r = api.get("/comprobantes/resumen", params={"anio": invalido})
        assert r.status_code == 200
        assert f"autorizados en {anio}." in r.text


def test_selector_ofrece_los_anios_con_comprobantes(api):
    _comprobante(api.conn, fecha="20240210", importe="10.00")
    html = api.get("/comprobantes/resumen?anio=2024").text
    assert '<option value="2024" selected>' in html
    assert f'<option value="{dt.date.today().year}"' not in html
    assert '<option value="2025"' not in html


def test_listado_tiene_pestanas_listado_y_resumen(api):
    html = api.get("/comprobantes").text
    assert 'href="/comprobantes" aria-current="page">Listado' in html
    assert 'href="/comprobantes/resumen"' in html
    assert 'aria-current="page">Resumen' not in html

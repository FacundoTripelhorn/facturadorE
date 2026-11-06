"""Orden de pago del banco: unir Facturas E y los invoices de la recaudadora.

Reglas: solo facturas autorizadas de un solo cliente; las Facturas E salen
en orden de número ascendente y los invoices de la recaudadora en el orden
en que se subieron; cada invoice necesita su número y la cuenta un nombre.
Lo subido se une en memoria y no se guarda.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fpdf import FPDF
from pypdf import PdfReader, PdfWriter
from starlette import formparsers

from facturador import repo
from facturador.api.setup_guard import is_html_ui_route, requires_ready_profile
from facturador.migrations import MIGRATIONS, current_version, migrate
from facturador.orden_pago import (
    MAX_UPLOAD_BYTES,
    OrdenPagoError,
    PdfSubido,
    identidad_receptor,
    mismo_cliente,
    nombre_facturas_e,
    nombre_recaudadora,
    normalizar_cuenta,
    numero_desde_nombre,
    unir_pdfs,
    validar_numero,
    validar_pdf_subido,
)
from tests.conftest import with_csrf
from tests.test_web import (
    CLIENTE_FORM,
    _autorizar,
    _crear_cliente_por_form,
    _generar_borrador,
)

# --- PDFs de prueba ---------------------------------------------------------


def _pdf_con_texto(texto: str, paginas: int = 1) -> bytes:
    pdf = FPDF()
    pdf.set_font("helvetica", size=14)
    for i in range(paginas):
        pdf.add_page()
        pdf.cell(text=f"{texto} p{i + 1}")
    return bytes(pdf.output())


def _pdf_encriptado() -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(_pdf_con_texto("secreto")))
    writer.encrypt("clave")
    salida = io.BytesIO()
    writer.write(salida)
    return salida.getvalue()


def _pdf_grande() -> bytes:
    """PDF válido de más de 2 MB (un adjunto incompresible)."""
    writer = PdfWriter(clone_from=io.BytesIO(_pdf_con_texto("GRANDE")))
    writer.add_attachment("relleno.bin", os.urandom(2 * 1024 * 1024 + 1))
    salida = io.BytesIO()
    writer.write(salida)
    return salida.getvalue()


def _textos(pdf: bytes) -> list[str]:
    return [p.extract_text().strip() for p in PdfReader(io.BytesIO(pdf)).pages]


# --- reglas puras -------------------------------------------------------------


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [
        ("Invoice-42.pdf", "42"),
        ("cobro_2026_0042.pdf", "2026"),
        ("INV0043.PDF", "0043"),
        ("sin numero.pdf", ""),
        ("factura.v2", ""),  # la extensión no cuenta
        ("C:\\Users\\yo\\Cobro 44.pdf", "44"),
        ("45", "45"),
    ],
)
def test_numero_se_precarga_con_el_primer_grupo_de_digitos(nombre, esperado):
    assert numero_desde_nombre(nombre) == esperado


@pytest.mark.parametrize(
    ("cuenta", "esperado"),
    [
        ("recaudadora", "recaudadora"),
        ("Recaudadora", "recaudadora"),
        ("Cobros Sur S.A.", "cobrossursa"),
        ("  Co-bros  ", "cobros"),
        ("Pagos Ñandú", "pagosnandu"),
        ("../../etc", "etc"),
        ("!!!", ""),
    ],
)
def test_cuenta_se_normaliza_para_el_nombre_del_archivo(cuenta, esperado):
    assert normalizar_cuenta(cuenta) == esperado


def test_nombre_facturas_e_va_en_orden_ascendente_sin_punto_de_venta():
    assert nombre_facturas_e([14, 12, 13]) == "e_invoices_12_13_14.pdf"


def test_nombre_recaudadora_respeta_el_orden_de_subida():
    assert (
        nombre_recaudadora("Recaudadora", ["44", "42", "43"])
        == "recaudadora_invoices_44_42_43.pdf"
    )


def test_nombre_recaudadora_exige_nombre_de_cuenta():
    with pytest.raises(OrdenPagoError, match="cuenta recaudadora"):
        nombre_recaudadora("  ?? ", ["42"])


@pytest.mark.parametrize("numero", ["", "   ", "42/43", "4 2", "x" * 41])
def test_numero_invalido_o_vacio_se_rechaza(numero):
    with pytest.raises(OrdenPagoError, match="a.pdf"):
        validar_numero(numero, "a.pdf")


def test_unir_respeta_el_orden_y_conserva_las_paginas():
    unido = unir_pdfs(
        [("b.pdf", _pdf_con_texto("B", 2)), ("a.pdf", _pdf_con_texto("A"))]
    )
    assert _textos(unido) == ["B p1", "B p2", "A p1"]


def test_no_pdf_se_rechaza_nombrando_el_archivo():
    with pytest.raises(OrdenPagoError, match="foto.png no es un PDF"):
        validar_pdf_subido(PdfSubido("foto.png", b"\x89PNG\r\n"))


def test_pdf_encriptado_se_rechaza_nombrando_el_archivo():
    with pytest.raises(OrdenPagoError, match="cerrado.pdf está protegido"):
        validar_pdf_subido(PdfSubido("cerrado.pdf", _pdf_encriptado()))


def test_pdf_corrupto_se_rechaza_nombrando_el_archivo():
    with pytest.raises(OrdenPagoError, match="No se pudo leer roto.pdf"):
        validar_pdf_subido(PdfSubido("roto.pdf", b"%PDF-1.7\nbasura sin xref"))


# --- datos por la app real ----------------------------------------------------

# Se pisa sobre CLIENTE_FORM: no es el cliente habitual.
OTRO_CLIENTE = {"razon_social": "OTRO CLIENTE S.A.", "is_default": "false"}


def _client_id(api, razon_social: str) -> str:
    return next(
        c["id"] for c in api.get("/clients").json() if c["razon_social"] == razon_social
    )


def _autorizada(api, **overrides) -> str:
    invoice_id = _generar_borrador(api, **overrides)
    r = _autorizar(api, invoice_id)
    assert r.status_code == 303, r.text
    return invoice_id


@pytest.fixture
def tres_autorizadas(api, arca):
    """Tres facturas autorizadas (nros 1, 2, 3) del cliente default, con la
    cuenta recaudadora cargada en el cliente."""
    _crear_cliente_por_form(api, cuenta_recaudadora="Recaudadora")
    ids = [
        _autorizada(api, imp_total=monto)
        for monto in ("1000,00", "1500,50", "499,50")
    ]
    return ids


def _ids_query(ids) -> str:
    return "&".join(f"ids={i}" for i in ids)


def _subir(api, ids, archivos, numeros, cuenta="recaudadora"):
    # Dict con listas: httpx manda el multipart con Content-Length, como un
    # navegador (una lista de tuplas lo mandaría chunked).
    form: dict[str, str | list[str]] = {**with_csrf(api, {"cuenta": cuenta})}
    form["ids"] = list(ids)
    form["numeros"] = list(numeros)
    files = [
        ("archivos", (nombre, contenido, "application/pdf"))
        for nombre, contenido in archivos
    ]
    return api.post("/ui/orden-de-pago/recaudadora", data=form, files=files)


# --- Comprobantes: selección ---------------------------------------------------


def test_solapa_autorizadas_ofrece_checkbox_y_boton(api, tres_autorizadas):
    borrador = _generar_borrador(api)
    html = api.get("/comprobantes?estado=autorizadas").text

    assert html.count('type="checkbox" name="ids"') == 3
    for invoice_id in tres_autorizadas:
        assert f'value="{invoice_id}"' in html
    assert borrador not in html
    assert 'id="armar-orden" disabled' in html
    assert "Armar orden de pago (" in html
    assert 'action="/orden-de-pago"' in html
    assert "Elegí facturas de un solo cliente" in html  # aviso de mezcla (JS)


def test_otras_solapas_no_tienen_seleccion(api, tres_autorizadas):
    _generar_borrador(api)
    for estado in ("todas", "borradores", "atencion"):
        html = api.get(f"/comprobantes?estado={estado}").text
        assert 'type="checkbox" name="ids"' not in html
        assert "armar-orden" not in html


def test_checkbox_lleva_el_cliente_para_detectar_mezcla(api, arca):
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE)
    uno = _autorizada(api)
    otro = _autorizada(api, client_id=_client_id(api, "OTRO CLIENTE S.A."))

    html = api.get("/comprobantes?estado=autorizadas").text

    for invoice_id in (uno, otro):
        factura = repo.get_invoice(api.conn, invoice_id)
        assert f'data-cliente="{factura["client_id"]}"' in html
        assert f'data-receptor="{identidad_receptor(factura)}"' in html
    assert repo.get_invoice(api.conn, uno)["client_id"] != repo.get_invoice(
        api.conn, otro
    )["client_id"]


def _como_reconstruida(api, invoice_id: str) -> None:
    """Deja la factura como la deja el rebuild desde ARCA: sin client_id."""
    api.conn.execute(
        "UPDATE invoices SET client_id = NULL WHERE id = ?", (invoice_id,)
    )
    api.conn.commit()


def test_reconstruida_y_nueva_del_mismo_cliente_se_pueden_unir(api, arca):
    """Tras un restore, las facturas reconstruidas desde ARCA no tienen
    client_id y las nuevas sí: siguen siendo del mismo cliente."""
    _crear_cliente_por_form(api, cuenta_recaudadora="Recaudadora")
    vieja = _autorizada(api)
    nueva = _autorizada(api)
    _como_reconstruida(api, vieja)

    html = api.get("/comprobantes?estado=autorizadas").text
    assert 'data-cliente=""' in html  # la reconstruida, sin id

    pagina = api.get(f"/orden-de-pago?ids={vieja}&ids={nueva}")
    assert pagina.status_code == 200, pagina.text
    assert 'value="Recaudadora"' in pagina.text  # del cliente de la que sí tiene id
    pdf = api.get(f"/orden-de-pago/facturas-e?ids={vieja}&ids={nueva}")
    assert pdf.status_code == 200
    assert 'filename="e_invoices_1_2.pdf"' in pdf.headers["content-disposition"]


def test_reconstruida_de_otro_receptor_no_se_mezcla(api, arca):
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE, id_impositivo="RUT 1111")
    uno = _autorizada(api)
    otro = _autorizada(api, client_id=_client_id(api, "OTRO CLIENTE S.A."))
    _como_reconstruida(api, otro)

    r = api.get(f"/orden-de-pago/facturas-e?ids={uno}&ids={otro}")
    assert r.status_code == 422
    assert r.json() == {"detail": "Elegí facturas de un solo cliente."}


def _fila(client_id, cliente="ACME", id_impositivo="RUT 1", cuit_pais=1):
    return {
        "client_id": client_id,
        "cliente": cliente,
        "id_impositivo": id_impositivo,
        "cuit_pais_cliente": cuit_pais,
    }


@pytest.mark.parametrize(
    ("facturas", "esperado"),
    [
        # Mismo id aunque el cliente se haya renombrado.
        ([_fila("c1", "ACME"), _fila("c1", "ACME LLC", "RUT 2")], True),
        # Reconstruida (sin id) + nueva: mismo receptor.
        ([_fila(None), _fila("c1")], True),
        ([_fila(None, id_impositivo=" rut  1 "), _fila("c1")], True),
        # Sin ID impositivo: el nombre del snapshot decide.
        ([_fila(None, "Acme ", ""), _fila("c1", "ACME", "")], True),
        ([_fila(None, "Acme", ""), _fila("c1", "Otra", "")], False),
        # Ids distintos: nunca, aunque compartan receptor.
        ([_fila("c1"), _fila("c2")], False),
        ([_fila(None), _fila("c1"), _fila("c2")], False),
        ([_fila("c1"), _fila("c2", id_impositivo="RUT 2")], False),
        ([_fila(None), _fila("c1", id_impositivo="RUT 2")], False),
        ([_fila(None), _fila(None, cuit_pais=2)], False),
    ],
)
def test_mismo_cliente(facturas, esperado):
    assert mismo_cliente(facturas) is esperado


# --- Pantalla "Orden de pago" -------------------------------------------------


def test_pantalla_lista_facturas_en_orden_y_suma_total(api, tres_autorizadas):
    r = api.get(f"/orden-de-pago?{_ids_query(reversed(tres_autorizadas))}")

    assert r.status_code == 200, r.text
    html = r.text
    posiciones = [html.index(f"00001-0000000{n}") for n in (1, 2, 3)]
    assert posiciones == sorted(posiciones)
    assert "USD 3.000,00" in html            # 1000 + 1500,50 + 499,50
    assert 'value="Recaudadora"' in html            # precargada desde el cliente
    assert "CLIENTE URUGUAY S.A." in html
    assert 'aria-label="Unir facturas E">Unir</button>' in html
    assert 'aria-label="Unir invoices recaudadora">Unir</button>' in html
    assert 'id="descargar-recaudadora" disabled' in html
    assert 'enctype="multipart/form-data"' in html
    assert "__descargaPdfLista" in html
    assert 'data-titulo-error="No se pudieron unir las facturas E."' in html
    assert (
        'data-titulo-error="No se pudieron unir los invoices de la recaudadora."'
        in html
    )


def test_pantalla_sin_cuenta_en_el_cliente_deja_el_campo_vacio(api, arca):
    _crear_cliente_por_form(api)
    invoice_id = _autorizada(api)
    html = api.get(f"/orden-de-pago?ids={invoice_id}").text
    assert 'id="cuenta" name="cuenta" value=""' in html


def test_pantalla_rechaza_clientes_mezclados(api, arca):
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE)
    uno = _autorizada(api)
    otro = _autorizada(api, client_id=_client_id(api, "OTRO CLIENTE S.A."))

    r = api.get(f"/orden-de-pago?ids={uno}&ids={otro}")

    assert r.status_code == 422
    assert "Elegí facturas de un solo cliente" in r.text
    assert "Unir facturas E" not in r.text
    assert "Unir invoices recaudadora" not in r.text


def test_pantalla_rechaza_facturas_no_autorizadas(api, tres_autorizadas):
    borrador = _generar_borrador(api)
    r = api.get(f"/orden-de-pago?ids={tres_autorizadas[0]}&ids={borrador}")
    assert r.status_code == 409
    assert "solo admite facturas autorizadas" in r.text


def test_pantalla_sin_facturas_o_con_id_inexistente(api, tres_autorizadas):
    assert api.get("/orden-de-pago").status_code == 422
    assert api.get("/orden-de-pago?ids=no-existe").status_code == 404


# --- Descarga: Facturas E -----------------------------------------------------


def test_descarga_facturas_e_une_en_orden_ascendente(api, tres_autorizadas):
    r = api.get(
        f"/orden-de-pago/facturas-e?{_ids_query(reversed(tres_autorizadas))}"
    )

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == (
        'attachment; filename="e_invoices_1_2_3.pdf"'
    )
    individuales = [
        api.get(f"/invoices/{i}/pdf?descargar=1").content for i in tres_autorizadas
    ]
    # Cada página es la de la descarga individual, en orden de número.
    esperado = [t for pdf in individuales for t in _textos(pdf)]
    assert len(esperado) == 3
    assert _textos(r.content) == esperado


def test_descarga_facturas_e_valida_en_el_backend(api, arca):
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE)
    uno = _autorizada(api)
    otro = _autorizada(api, client_id=_client_id(api, "OTRO CLIENTE S.A."))
    borrador = _generar_borrador(api)

    mezcla = api.get(f"/orden-de-pago/facturas-e?ids={uno}&ids={otro}")
    assert mezcla.status_code == 422
    assert mezcla.json() == {"detail": "Elegí facturas de un solo cliente."}

    no_autorizada = api.get(f"/orden-de-pago/facturas-e?ids={uno}&ids={borrador}")
    assert no_autorizada.status_code == 409
    assert "autorizadas" in no_autorizada.json()["detail"]


# --- Descarga: invoices de la recaudadora --------------------------------------


def test_descarga_recaudadora_une_en_orden_de_subida(api, tres_autorizadas):
    r = _subir(
        api,
        tres_autorizadas,
        [
            ("Invoice-44.pdf", _pdf_con_texto("I44")),
            ("Invoice-42.pdf", _pdf_con_texto("I42", 2)),
        ],
        ["44", "42"],
        cuenta="Cobros Sur S.A.",
    )

    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == (
        'attachment; filename="cobrossursa_invoices_44_42.pdf"'
    )
    assert _textos(r.content) == ["I44 p1", "I42 p1", "I42 p2"]


def test_descarga_recaudadora_sin_numero_es_4xx(api, tres_autorizadas):
    r = _subir(
        api,
        tres_autorizadas,
        [("a.pdf", _pdf_con_texto("A")), ("sin-numero.pdf", _pdf_con_texto("B"))],
        ["1", "  "],
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "Falta el número del invoice sin-numero.pdf."}


def test_descarga_recaudadora_sin_tantos_numeros_como_pdfs_es_4xx(
    api, tres_autorizadas
):
    r = _subir(
        api,
        tres_autorizadas,
        [("a.pdf", _pdf_con_texto("A")), ("b.pdf", _pdf_con_texto("B"))],
        ["1"],
    )
    assert r.status_code == 422
    assert "necesita su número" in r.json()["detail"]


def test_descarga_recaudadora_sin_cuenta_es_4xx(api, tres_autorizadas):
    r = _subir(
        api, tres_autorizadas, [("a.pdf", _pdf_con_texto("A"))], ["1"], cuenta=" "
    )
    assert r.status_code == 422
    assert "cuenta recaudadora" in r.json()["detail"]


def test_descarga_recaudadora_sin_pdfs_es_4xx(api, tres_autorizadas):
    r = _subir(api, tres_autorizadas, [], [])
    assert r.status_code == 422
    assert "Subí al menos un PDF" in r.json()["detail"]


@pytest.mark.parametrize(
    ("archivo", "mensaje"),
    [
        (("foto.png", b"\x89PNG\r\n\x1a\n"), "foto.png no es un PDF"),
        (("cerrado.pdf", None), "cerrado.pdf está protegido"),
        (("roto.pdf", b"%PDF-1.7\nbasura"), "No se pudo leer roto.pdf"),
    ],
)
def test_descarga_recaudadora_rechaza_archivos_invalidos(
    api, tres_autorizadas, archivo, mensaje
):
    nombre, contenido = archivo
    r = _subir(
        api,
        tres_autorizadas,
        [("ok.pdf", _pdf_con_texto("OK")), (nombre, contenido or _pdf_encriptado())],
        ["1", "2"],
    )
    assert r.status_code == 422
    assert mensaje in r.json()["detail"]


def test_descarga_recaudadora_valida_las_facturas(api, arca):
    _crear_cliente_por_form(api)
    _crear_cliente_por_form(api, **OTRO_CLIENTE)
    uno = _autorizada(api)
    otro = _autorizada(api, client_id=_client_id(api, "OTRO CLIENTE S.A."))
    borrador = _generar_borrador(api)
    pdfs = [("a.pdf", _pdf_con_texto("A"))]

    assert _subir(api, [uno, otro], pdfs, ["1"]).status_code == 422
    assert _subir(api, [uno, borrador], pdfs, ["1"]).status_code == 409
    assert _subir(api, [], pdfs, ["1"]).status_code == 422


def test_descarga_recaudadora_exige_csrf(api, tres_autorizadas):
    r = api.post(
        "/ui/orden-de-pago/recaudadora",
        data={"cuenta": "recaudadora", "ids": tres_autorizadas[0], "numeros": "1"},
        files=[("archivos", ("a.pdf", _pdf_con_texto("A"), "application/pdf"))],
    )
    assert r.status_code == 403


def test_subida_demasiado_grande_se_rechaza_sin_leerla(api, tres_autorizadas):
    r = api.post(
        "/ui/orden-de-pago/recaudadora",
        content=b"x",
        headers={
            "content-type": "multipart/form-data; boundary=x",
            "content-length": str(MAX_UPLOAD_BYTES + 1),
        },
    )
    assert r.status_code == 413
    assert "MB" in r.json()["detail"]


def _archivos_del_perfil(root: Path) -> dict[str, int]:
    return {
        str(p.relative_to(root)): p.stat().st_size
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))
    }


def _filas(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    tablas = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
        for t in tablas
    }


def test_lo_subido_no_se_guarda_ni_en_disco_ni_en_la_base(
    api, tres_autorizadas, test_profile, monkeypatch
):
    # El parser multipart guarda cada archivo en un SpooledTemporaryFile que
    # pasa a disco al superar su umbral: se registra si eso pasa.
    a_disco: list[int] = []

    class Spool(tempfile.SpooledTemporaryFile):
        def rollover(self) -> None:
            a_disco.append(self.tell())
            super().rollover()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Spool)
    root = test_profile.paths.root
    antes_archivos = _archivos_del_perfil(root)
    antes_filas = _filas(api.conn)

    # Más grande que el umbral de 1 MB con el que Starlette pasa a disco.
    grande = _pdf_grande()
    assert len(grande) > 2 * 1024 * 1024
    r = _subir(
        api,
        tres_autorizadas,
        [("Invoice-42.pdf", grande), ("Invoice-43.pdf", _pdf_con_texto("I43"))],
        ["42", "43"],
    )

    assert r.status_code == 200, r.text
    assert a_disco == []
    assert _archivos_del_perfil(root) == antes_archivos
    assert _filas(api.conn) == antes_filas


def test_orden_de_pago_exige_perfil_listo():
    assert requires_ready_profile("GET", "/orden-de-pago")
    assert requires_ready_profile("GET", "/orden-de-pago/facturas-e")
    assert requires_ready_profile("POST", "/ui/orden-de-pago/recaudadora")
    # La pantalla redirige a /setup; las descargas responden JSON.
    assert is_html_ui_route("GET", "/orden-de-pago")
    assert not is_html_ui_route("GET", "/orden-de-pago/facturas-e")
    assert not is_html_ui_route("POST", "/ui/orden-de-pago/recaudadora")


# --- Clientes: cuenta recaudadora ---------------------------------------------


def test_cliente_guarda_la_cuenta_recaudadora_por_form(api, arca):
    _crear_cliente_por_form(api, cuenta_recaudadora="  Recaudadora  ")
    cliente = api.get("/clients").json()[0]
    assert cliente["cuenta_recaudadora"] == "Recaudadora"

    edit = api.get(f"/clientes?edit={cliente['id']}").text
    assert 'name="cuenta_recaudadora"' in edit
    assert 'value="Recaudadora"' in edit

    r = api.post(
        "/ui/clientes",
        data=with_csrf(
            api, {**CLIENTE_FORM, "client_id": cliente["id"], "cuenta_recaudadora": ""}
        ),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert api.get("/clients").json()[0]["cuenta_recaudadora"] is None


def test_editar_la_cuenta_en_la_orden_no_toca_el_cliente(api, tres_autorizadas):
    r = _subir(
        api,
        tres_autorizadas,
        [("a.pdf", _pdf_con_texto("A"))],
        ["7"],
        cuenta="Otra Cuenta",
    )
    assert r.status_code == 200
    assert "otracuenta_invoices_7.pdf" in r.headers["content-disposition"]
    assert api.get("/clients").json()[0]["cuenta_recaudadora"] == "Recaudadora"


def test_migracion_agrega_la_cuenta_recaudadora_a_clientes_existentes(tmp_path):
    conn = sqlite3.connect(tmp_path / "v1.db")
    conn.row_factory = sqlite3.Row
    try:
        migrate(conn, migrations=MIGRATIONS[:1])
        conn.execute(
            "INSERT INTO clients (id, razon_social, pais_dst, cuit_pais,"
            " created_at, updated_at) VALUES ('c1', 'Viejo', 225, 1, 't', 't')"
        )
        conn.commit()

        migrate(conn)

        assert current_version(conn) == 2
        fila = conn.execute("SELECT * FROM clients WHERE id = 'c1'").fetchone()
        assert fila["razon_social"] == "Viejo"
        assert fila["cuenta_recaudadora"] is None
    finally:
        conn.close()

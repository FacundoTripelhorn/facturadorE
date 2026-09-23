"""Render del comprobante: fila SQLite → PDF (fpdf2).

El layout replica el comprobante real de Comprobantes en Línea analizado en
design.md §0.1: reglas horizontales mínimas (sin marco exterior), divisor
central solo en la cabecera, tabla de ítems con recuadro únicamente en el
encabezado y bloque de totales/CAE anclado al pie. Todos los valores
impresos salen del snapshot inmutable de la factura e ítems: emisor,
CUIT fiscal, receptor, descripciones de params y ``pdf_render_version``.
No se relee emisores, clients, settings ni arca_params.

Los textos libres (razón social, domicilio, descripciones, del emisor y
del cliente) son hostiles (checklist §2.1.1 punto 4). fpdf2 los dibuja
como texto literal: nunca usar ``markdown=True`` ni los modos HTML de
fpdf2 con datos del usuario.

El despacho elige el renderer por ``pdf_render_version``. Los PDF
generados son cache local opcional bajo ``ProfilePaths.pdf_dir``; se pueden
borrar sin perder el registro fiscal (la DB es la fuente de verdad).

El motor es fpdf2: Python puro, sin browser ni subprocesos. El
comprobante ocupa una sola página A4; si el contenido no entra se levanta
``PdfLayoutError`` en lugar de recortar o partir el comprobante.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

from fpdf import FPDF

from ..constants import MONEDA_DISPLAY
from ..settings import Emisor, emisor_from_invoice_snapshot
from .qr import build_qr_payload, qr_png, qr_url
from .registry import get_pdf_renderer, register_pdf_renderer

_FONTS_DIR = Path(__file__).parent / "fonts"


class PdfLayoutError(ValueError):
    """El comprobante no entra en una página A4 (no hay soporte multipágina)."""


# --- formato de valores (paridad con el comprobante real) ----------------


def _fecha_larga(aaaammdd: str) -> str:
    return f"{aaaammdd[6:]}/{aaaammdd[4:6]}/{aaaammdd[:4]}"


def _num(valor: str | Decimal, decimales: int) -> str:
    """Formato numérico del comprobante real: coma decimal, sin separador
    de miles (cantidades y precios unitarios van con 6 decimales; importes,
    con 2)."""
    return f"{Decimal(str(valor)):.{decimales}f}".replace(".", ",")


def _moneda(code: str) -> str:
    # DOL → USD para el lector; el código ARCA viaja solo en el XML.
    # Alias atado a PDF_RENDER_VERSION / renderer v1: cambiarlo
    # exige bump de versión + renderer nuevo, no releer settings.
    return MONEDA_DISPLAY.get(code, code)


def _ctz(valor: str | Decimal) -> str:
    # La cotización es el único número que el comprobante real imprime con
    # punto decimal (6 decimales).
    return f"{Decimal(str(valor)):.6f}"


def invoice_pdf_filename(inv: sqlite3.Row) -> str:
    # Incluye cbte_tipo: la numeración ARCA es por (punto_venta, cbte_tipo),
    # así Factura E / NC E / ND E con el mismo nro no colisionan en el cache.
    return (
        f"factura-E-{inv['cbte_tipo']}"
        f"-{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}"
        f"-{inv['environment']}.pdf"
    )


def invoice_pdf_cache_path(pdf_dir: Path, inv: sqlite3.Row) -> Path:
    """Ruta del cache local opcional para el PDF de esta factura."""
    return pdf_dir / invoice_pdf_filename(inv)


def _require_cuit_emisor(inv: sqlite3.Row) -> int:
    raw = inv["cuit_emisor"] if "cuit_emisor" in inv.keys() else None
    if raw is None or str(raw).strip() == "":
        raise ValueError(
            f"La factura {inv['id']} no tiene CUIT fiscal snapshot; "
            "no se puede regenerar el PDF."
        )
    return int(raw)


def _umed_label(item: sqlite3.Row) -> str:
    """Etiqueta de U. Medida del ítem (snapshot); fallback al código."""
    keys = item.keys()
    if "pro_umed_ds" in keys and item["pro_umed_ds"]:
        return str(item["pro_umed_ds"])
    return str(item["pro_umed"])


# --- renderer v1 (fpdf2) ---------------------------------------------------

_PT = 25.4 / 72  # mm por punto tipográfico
_LEFT, _RIGHT, _TOP = 10.0, 200.0, 10.0  # A4 con márgenes de 10 mm
_WIDTH = _RIGHT - _LEFT
_BOTTOM = _TOP + 272.0  # alto útil del comprobante (pie anclado acá)
_INK = (17, 17, 17)
_HOMO_RED = (187, 0, 0)
_ITEM_COLS = (12.0, 76.0, 30.0, 34.0, 38.0)  # Ítem, Descripción, Cant., P.U., Total
_CELL_PAD = 1.5


def _lh(size: float) -> float:
    """Alto de línea para un cuerpo de ``size`` puntos (line-height normal)."""
    return size * _PT * 1.15


class _ComprobanteV1(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4", unit="mm")
        self.set_margins(_LEFT, _TOP, self.w - _RIGHT)
        self.set_auto_page_break(False)
        self.add_font("sans", "", str(_FONTS_DIR / "LiberationSans-Regular.ttf"))
        self.add_font("sans", "B", str(_FONTS_DIR / "LiberationSans-Bold.ttf"))
        self.set_text_color(*_INK)
        self.c_margin = 0

    def campo(
        self, x: float, w: float, y: float, label: str, value: str, size: float = 9
    ) -> float:
        """Dibuja 'Rótulo: valor' dentro de [x, x + w]; devuelve la y siguiente.

        El rótulo va en negrita y el valor en regular; si no entra en una
        línea, el texto sigue debajo dentro del mismo ancho.
        """
        self.set_left_margin(x)
        self.set_right_margin(self.w - (x + w))
        self.set_xy(x, y)
        self.set_font("sans", "B", size)
        self.write(_lh(size), f"{label} ")
        self.set_font("sans", "", size)
        self.write(_lh(size), value)
        y_next = self.get_y() + _lh(size) + 1
        self.set_left_margin(_LEFT)
        self.set_right_margin(self.w - _RIGHT)
        return y_next

    def regla(self, y: float, width_pt: float = 0.5) -> None:
        self.set_line_width(width_pt * _PT)
        self.set_draw_color(0, 0, 0)
        self.line(_LEFT, y, _RIGHT, y)

    def lineas(self, w: float, texto: str, size: float, style: str = "") -> int:
        """Cantidad de líneas que ocupa ``texto`` en un ancho ``w``."""
        self.set_font("sans", style, size)
        lines = self.multi_cell(w, _lh(size), texto, dry_run=True, output="LINES")
        assert isinstance(lines, list)
        return max(1, len(lines))


def _banner_homo(pdf: _ComprobanteV1, y: float) -> float:
    pdf.set_draw_color(*_HOMO_RED)
    pdf.set_line_width(1.5 * _PT)
    pdf.set_dash_pattern(dash=1.6, gap=1.2)
    alto = 4 + _lh(10)
    pdf.rect(_LEFT, y, _WIDTH, alto)
    pdf.set_dash_pattern()
    pdf.set_text_color(*_HOMO_RED)
    pdf.set_font("sans", "B", 10)
    pdf.set_xy(_LEFT, y)
    pdf.cell(
        _WIDTH, alto, "COMPROBANTE DE HOMOLOGACIÓN — SIN VALOR FISCAL", align="C"
    )
    pdf.set_text_color(*_INK)
    return y + alto + 3


def _cabecera(
    pdf: _ComprobanteV1,
    y: float,
    inv: sqlite3.Row,
    emisor: Emisor,
    cuit_emisor: int,
) -> float:
    """Dos columnas (emisor | comprobante) con divisor y caja de tipo."""
    y_cab = y
    pdf.regla(y_cab, 1)
    medio = _LEFT + _WIDTH / 2
    col_w = _WIDTH / 2 - 3

    # Columna izquierda: emisor.
    yl = y_cab + 11
    pdf.set_font("sans", "B", 13)
    pdf.set_xy(_LEFT, yl)
    pdf.multi_cell(col_w, _lh(13), emisor.razon_social or "—", align="C")
    yl = pdf.get_y() + 3
    yl = pdf.campo(_LEFT, col_w, yl, "Razón Social:", emisor.razon_social)
    yl = pdf.campo(_LEFT, col_w, yl, "Domicilio Comercial:", emisor.domicilio)
    yl = pdf.campo(
        _LEFT, col_w, yl, "Condición frente al IVA:", emisor.condicion_iva
    )

    # Columna derecha: datos del comprobante. En el comprobante real la
    # leyenda de exento cierra esta columna (no es una banda centrada).
    xr = medio + 3
    yr = y_cab + 11
    pdf.set_font("sans", "B", 12)
    pdf.set_xy(xr, yr)
    pdf.multi_cell(col_w, _lh(12), "FACTURA DE EXPORTACIÓN", align="C")
    yr = pdf.get_y() + 3
    nro = f"{inv['punto_venta']:05d}-{inv['cbte_nro']:08d}"
    yr = pdf.campo(xr, col_w, yr, "Compr. Nro:", nro)
    yr = pdf.campo(
        xr, col_w, yr, "Fecha de Emisión:", _fecha_larga(inv["fecha_cbte"])
    )
    yr = pdf.campo(xr, col_w, yr, "CUIT:", str(cuit_emisor))
    yr = pdf.campo(xr, col_w, yr, "Ingresos Brutos:", emisor.iibb)
    yr = pdf.campo(
        xr, col_w, yr, "Fecha de Inicio de Actividades:", emisor.inicio_actividades
    )
    pdf.set_font("sans", "B", 9)
    pdf.set_xy(xr, yr + 1)
    pdf.multi_cell(col_w, _lh(9), "IVA EXENTO OPERACIÓN DE EXPORTACIÓN")
    yr = pdf.get_y() + 1

    y_fin = max(yl, yr) + 2
    pdf.set_line_width(1 * _PT)
    pdf.line(medio, y_cab, medio, y_fin)

    # Caja "E / COD. nn" centrada sobre el divisor (tapa la línea).
    bw = 16.0
    bh = 1 + 20 * _PT + 6 * _PT * 1.2 + 1
    x0 = medio - bw / 2
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(x0, y_cab, bw, bh, style="F")
    pdf.line(x0, y_cab, x0, y_cab + bh)
    pdf.line(x0 + bw, y_cab, x0 + bw, y_cab + bh)
    pdf.line(x0, y_cab + bh, x0 + bw, y_cab + bh)
    pdf.set_font("sans", "B", 20)
    pdf.set_xy(x0, y_cab + 1)
    pdf.cell(bw, 20 * _PT, "E", align="C")
    pdf.set_font("sans", "", 6)
    pdf.set_xy(x0, y_cab + 1 + 20 * _PT)
    pdf.cell(bw, 6 * _PT * 1.2, f"COD. {inv['cbte_tipo']}", align="C")
    return y_fin


def _bloques_receptor_y_pago(
    pdf: _ComprobanteV1, y: float, inv: sqlite3.Row, divisa: str
) -> float:
    mitad = _WIDTH / 2
    tercio = _WIDTH / 3

    # Receptor.
    pdf.regla(y)
    y += 2
    y = max(
        pdf.campo(_LEFT, mitad, y, "Señor(es):", inv["cliente"]),
        pdf.campo(_LEFT + mitad, mitad, y, "Domicilio:", inv["domicilio_cliente"]),
    )
    cuit_pais = str(inv["cuit_pais_cliente"])
    if inv["cuit_pais_cliente_ds"]:
        cuit_pais += f" ({inv['cuit_pais_cliente_ds']})"
    y = pdf.campo(_LEFT, _WIDTH, y, "CUIT País:", cuit_pais)
    y = pdf.campo(_LEFT, _WIDTH, y, "ID Impositivo:", inv["id_impositivo"]) + 2

    # Divisa y destino ("Destino del Comprobante" es el PAÍS, design.md §0.1).
    pdf.regla(y)
    y += 2
    y = pdf.campo(_LEFT, _WIDTH, y, "Divisa:", divisa)
    destino = inv["dst_cmp_ds"] or str(inv["dst_cmp"])
    y = pdf.campo(_LEFT, _WIDTH, y, "Destino del Comprobante:", destino) + 2

    # Forma de pago.
    pdf.regla(y)
    y += 2
    fecha_pago = _fecha_larga(inv["fecha_pago"]) if inv["fecha_pago"] else ""
    return (
        max(
            pdf.campo(_LEFT, tercio, y, "Forma de Pago:", inv["forma_pago"] or ""),
            pdf.campo(_LEFT + tercio, tercio, y, "Fecha de Pago:", fecha_pago),
            pdf.campo(
                _LEFT + 2 * tercio, tercio, y, "Incoterms:", inv["incoterms"] or ""
            ),
        )
        + 2
    )


def _tabla_items(
    pdf: _ComprobanteV1, y: float, items: list[sqlite3.Row], moneda: str
) -> float:
    pdf.regla(y)
    y += 3
    titulos = (
        "Ítem",
        "Descripción",
        "Cantidad",
        f"Precio Unit. ({moneda})",
        f"Total por ítem ({moneda})",
    )
    alineacion = ("C", "C", "R", "R", "R")
    pdf.set_font("sans", "B", 8)
    pdf.set_fill_color(238, 238, 238)
    pdf.set_line_width(0.5 * _PT)
    alto_th = 2 * _CELL_PAD + _lh(8)
    pdf.c_margin = _CELL_PAD
    x = _LEFT
    for ancho, titulo, al in zip(_ITEM_COLS, titulos, alineacion, strict=True):
        pdf.set_xy(x, y)
        pdf.cell(ancho, alto_th, titulo, border=1, align=al, fill=True)
        x += ancho
    pdf.c_margin = 0
    y += alto_th

    w_item, w_desc, w_cant, w_pu, w_total = _ITEM_COLS
    for idx, item in enumerate(items, start=1):
        y_fila = y + _CELL_PAD
        # El comprobante real imprime "código - descripción".
        desc = item["pro_ds"]
        if item["pro_codigo"]:
            desc = f"{item['pro_codigo']} - {desc}"
        pdf.set_font("sans", "", 9)
        pdf.set_xy(_LEFT + w_item + _CELL_PAD, y_fila)
        pdf.multi_cell(w_desc - 2 * _CELL_PAD, _lh(9), desc)
        y_desc = pdf.get_y()

        pdf.set_xy(_LEFT + _CELL_PAD, y_fila)
        pdf.cell(w_item - 2 * _CELL_PAD, _lh(9), f"{idx:04d}")
        # U. Medida no es columna propia: segunda línea bajo la cantidad.
        x = _LEFT + w_item + w_desc
        pdf.set_xy(x, y_fila)
        pdf.cell(w_cant - _CELL_PAD, _lh(9), _num(item["pro_qty"], 6), align="R")
        pdf.set_font("sans", "", 8)
        pdf.set_xy(x, y_fila + _lh(9))
        pdf.cell(
            w_cant - _CELL_PAD, _lh(8), f"U. Medida: {_umed_label(item)}", align="R"
        )
        pdf.set_font("sans", "", 9)
        x += w_cant
        pdf.set_xy(x, y_fila)
        pdf.cell(
            w_pu - _CELL_PAD, _lh(9), _num(item["pro_precio_uni"], 6), align="R"
        )
        x += w_pu
        pdf.set_xy(x, y_fila)
        pdf.cell(
            w_total - _CELL_PAD, _lh(9), _num(item["pro_total_item"], 2), align="R"
        )
        y = max(y_desc, y_fila + _lh(9) + _lh(8)) + _CELL_PAD
    return y


def _pie(
    pdf: _ComprobanteV1,
    inv: sqlite3.Row,
    cuit_emisor: int,
    moneda: str,
    divisa: str,
    y_contenido: float,
) -> None:
    """Totales, QR y CAE anclados al pie; observaciones justo arriba.

    Se arma de abajo hacia arriba. Si el contenido ya dibujado invade el
    pie, el comprobante no entra en una página: ``PdfLayoutError``.
    """
    mitad = _WIDTH / 2
    y_descargo = _BOTTOM - 6.5 * _PT * 1.2 - 1
    y_qr = y_descargo - 32
    y_total = y_qr - 4 - _lh(11)
    y_totales = y_total - 5 - (_lh(9) + 2)

    y_tope = y_totales
    obs = inv["obs"]
    if obs:
        alto_obs = pdf.lineas(_WIDTH, f"Observaciones: {obs}", 9, "B") * _lh(9) + 5
        y_tope = y_totales - alto_obs

    if y_contenido + 2 > y_tope:
        raise PdfLayoutError(
            "El comprobante no entra en una página A4: los ítems, las "
            "descripciones o las observaciones son demasiado largos. "
            "El comprobante sigue autorizado en ARCA; solo falla su PDF."
        )

    if obs:
        pdf.regla(y_tope)
        pdf.campo(_LEFT, _WIDTH, y_tope + 2, "Observaciones:", obs)

    # La divisa se repite junto a la cotización, como en el comprobante real.
    ctz = _ctz(inv["moneda_ctz"])
    y_rot = (
        max(
            pdf.campo(_LEFT, mitad, y_totales + 2, "Tipo de Cambio:", ctz),
            pdf.campo(_LEFT + mitad, mitad, y_totales + 2, "Divisa:", divisa),
        )
        + 1
    )
    pdf.regla(y_rot)
    pdf.set_font("sans", "B", 11)
    pdf.set_xy(_LEFT, y_rot + 2)
    pdf.cell(
        _WIDTH,
        _lh(11),
        f"Importe Total: {moneda} {_num(inv['imp_total'], 2)}",
        align="R",
    )

    # QR RG 4892 + leyenda + CAE.
    png = qr_png(qr_url(build_qr_payload(inv, cuit_emisor)))
    pdf.image(io.BytesIO(png), x=_LEFT, y=y_qr + 2, w=28, h=28)
    y_medio = y_qr + 16
    pdf.set_font("sans", "B", 9)
    pdf.set_xy(_LEFT + 34, y_medio - _lh(9) / 2)
    pdf.cell(60, _lh(9), "Comprobante Autorizado")

    cae = str(inv["cae"])
    pdf.set_font("sans", "B", 12)
    w_cae = pdf.get_string_width(cae)
    pdf.set_font("sans", "B", 9)
    rot_cae = "CAE N°: "
    w_rot_cae = pdf.get_string_width(rot_cae)
    y_cae = y_medio - _lh(12) + 0.5
    pdf.set_xy(_RIGHT - w_cae - w_rot_cae, y_cae + (_lh(12) - _lh(9)))
    pdf.cell(w_rot_cae, _lh(9), rot_cae)
    pdf.set_font("sans", "B", 12)
    pdf.set_xy(_RIGHT - w_cae, y_cae)
    pdf.cell(w_cae, _lh(12), cae)

    vto = _fecha_larga(inv["cae_fch_vto"])
    rot_vto = "Fecha de Vto. de CAE: "
    pdf.set_font("sans", "", 9)
    w_vto = pdf.get_string_width(vto)
    pdf.set_font("sans", "B", 9)
    w_rot_vto = pdf.get_string_width(rot_vto)
    pdf.set_xy(_RIGHT - w_vto - w_rot_vto, y_cae + _lh(12) + 1)
    pdf.cell(w_rot_vto, _lh(9), rot_vto)
    pdf.set_font("sans", "", 9)
    pdf.cell(w_vto, _lh(9), vto)

    pdf.set_font("sans", "", 6.5)
    pdf.set_text_color(51, 51, 51)
    pdf.set_xy(_LEFT, y_descargo + 1)
    pdf.cell(
        _WIDTH,
        6.5 * _PT * 1.2,
        "Esta Agencia no se responsabiliza por la veracidad de los datos "
        "ingresados en el detalle de la operación",
    )
    pdf.set_text_color(*_INK)


def _render_pdf_v1(inv: sqlite3.Row, items: list[sqlite3.Row]) -> bytes:
    """Renderer PDF versión 1: layout del comprobante real (una página A4)."""
    emisor: Emisor = emisor_from_invoice_snapshot(inv)
    if not emisor.completo:
        raise ValueError(
            "La factura no tiene snapshot completo del emisor; "
            "no se puede generar el PDF."
        )
    cuit_emisor = _require_cuit_emisor(inv)
    moneda = _moneda(inv["moneda_id"])
    moneda_ds = inv["moneda_ds"] or ""
    divisa = f"{moneda} - {moneda_ds}" if moneda_ds else moneda

    pdf = _ComprobanteV1()
    pdf.add_page()
    y = _TOP
    # El ambiente del comprobante es el snapshot de la fila, no la config
    # actual: un PDF de homologación se marca siempre como tal.
    if inv["environment"] == "homo":
        y = _banner_homo(pdf, y)
    y = _cabecera(pdf, y, inv, emisor, cuit_emisor)
    y = _bloques_receptor_y_pago(pdf, y, inv, divisa)
    y = _tabla_items(pdf, y, items, moneda)
    _pie(pdf, inv, cuit_emisor, moneda, divisa, y)
    return bytes(pdf.output())


# Contrato actual: versión 1. Futuros cambios de layout registran v2+.
register_pdf_renderer(1, _render_pdf_v1)


def _pdf_render_version(inv: sqlite3.Row) -> int:
    version = (
        inv["pdf_render_version"]
        if "pdf_render_version" in inv.keys()
        else None
    )
    if version is None:
        raise ValueError(
            f"La factura {inv['id']} no tiene pdf_render_version; "
            "crear un borrador nuevo con el esquema actual."
        )
    return int(version)


def render_invoice_pdf(inv: sqlite3.Row, items: list[sqlite3.Row]) -> bytes:
    """PDF del comprobante: despacha por ``pdf_render_version``."""
    version = _pdf_render_version(inv)
    return get_pdf_renderer(version)(inv, items)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Escribe ``data`` en ``path`` vía temp + replace (sin PDF a medias)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# Locks en proceso por ruta de cache. Single-process (AGENTS.md):
# un ``threading.Lock`` alcanza; las rutas sync corren en el threadpool.
_render_locks: dict[Path, threading.Lock] = {}
_render_locks_guard = threading.Lock()


def _render_lock_for(path: Path) -> threading.Lock:
    with _render_locks_guard:
        lock = _render_locks.get(path)
        if lock is None:
            lock = _render_locks[path] = threading.Lock()
        return lock


def get_or_render_invoice_pdf(
    inv: sqlite3.Row,
    items: list[sqlite3.Row],
    pdf_dir: Path,
) -> bytes:
    """Sirve el cache local si existe; si no, regenera desde el snapshot.

    El archivo bajo ``pdf_dir`` es descartable: borrarlo no afecta el
    registro fiscal en SQLite. Tras regenerar se vuelve a cachear con
    escritura atómica (temp + ``os.replace``) para que un hit concurrente
    no lea un PDF a medias.
    """
    path = invoice_pdf_cache_path(pdf_dir, inv)
    if path.is_file():
        return path.read_bytes()
    # Un solo render por comprobante: si dos pedidos llegan a la vez con el
    # cache vacío, el segundo espera al primero y sirve el cache en lugar
    # de renderizar de nuevo.
    with _render_lock_for(path):
        if path.is_file():
            return path.read_bytes()
        pdf = render_invoice_pdf(inv, items)
        _atomic_write_bytes(path, pdf)
        return pdf

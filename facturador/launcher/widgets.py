"""Ventana y controles del launcher en Tk, con el tema de la app.

El launcher tiene una sola ventana, de la elección hasta que abre la app:
el selector, la confirmación de Producción, el progreso y el error son
vistas que se montan en ella (``ventana()``), así nunca hay un momento sin
ventana. Arriba lleva ícono + wordmark; abajo, el contenido de la vista y un
pie para los botones. ``cerrar_ventana()`` la cierra antes de abrir la app.

Los controles se dibujan con Pillow (``theme``) sobre un ``Canvas`` y
llevan el anillo de foco de ``--acento`` cuando tienen el foco del teclado.

Importa ``tkinter`` al cargarse: los callers lo importan dentro de un
``try`` y, si falla, caen al menú de texto o al stderr.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from typing import Any, Literal

from PIL import Image, ImageTk

from ..constants import ArcaEnvironment
from ..marca import ICONO_SVG, WORDMARK_SVG, rasterizar
from .frozen import set_window_icon
from .theme import (
    FUENTES_MONO,
    FUENTES_TEXTO,
    MARGEN,
    Paleta,
    barra_de_titulo_oscura,
    caja,
    cargar_paleta,
    circulo,
    elegir_familia,
    icono_error,
    sistema_en_oscuro,
)

TITULO = "FacturadorE"
# Medidas del board, en px a 96 dpi; ``Ventana.px`` las escala.
ANCHO_VENTANA = 460
ALTO_MINIMO = 388  # 420 con la barra de título
PADDING_X = 24
ANCHO_CONTENIDO = ANCHO_VENTANA - 2 * PADDING_X
RADIO = 8

Variante = Literal["primario", "secundario", "peligro", "enlace"]

# Colores de la píldora de ambiente: los mismos que en la app.
_PILDORA = {
    ArcaEnvironment.HOMO: ("aviso-fondo", "aviso", "aviso-borde"),
    ArcaEnvironment.PROD: ("prod-fondo", "prod-texto", "prod-fondo"),
}
# Punto de cada ambiente en las tarjetas del selector.
PUNTO = {ArcaEnvironment.HOMO: "aviso", ArcaEnvironment.PROD: "error"}


_abierta: Ventana | None = None


def ventana(*, oscuro: bool | None = None) -> Ventana:
    """La ventana del launcher lista para montar una vista: la que ya está
    abierta (vaciada) o una nueva."""
    global _abierta
    if _abierta is not None and _abierta.viva():
        _abierta.limpiar()
        return _abierta
    _abierta = Ventana(oscuro=oscuro)
    return _abierta


def hay_ventana() -> bool:
    """True si la ventana del launcher está abierta."""
    return _abierta is not None and _abierta.viva()


def cerrar_ventana() -> None:
    """Cierra la ventana del launcher (antes de abrir la app o al salir)."""
    global _abierta
    if _abierta is not None:
        _abierta.destruir()
        _abierta = None


class Ventana:
    """La ventana del launcher. Cada vista se monta con ``ventana()`` y
    ``ejecutar`` bloquea hasta que la vista llama a ``terminar``; la ventana
    sigue visible para la vista siguiente."""

    def __init__(self, *, oscuro: bool | None = None) -> None:
        self.paleta: Paleta = cargar_paleta(
            sistema_en_oscuro() if oscuro is None else oscuro
        )
        self.root = tk.Tk()
        try:
            self._armar()
        except Exception:
            # Que no quede una ventana oculta viva si el caller cae al menú
            # de texto.
            self.destruir()
            raise

    def _armar(self) -> None:
        # Oculta hasta tener tamaño y barra de título: sin parpadeo.
        self.root.withdraw()
        self.escala = max(1.0, self.root.winfo_fpixels("1i") / 96)
        familias = tkfont.families(self.root)
        self._familia = elegir_familia(familias, FUENTES_TEXTO) or str(
            tkfont.nametofont("TkDefaultFont").actual("family")
        )
        self._familia_mono = elegir_familia(familias, FUENTES_MONO) or str(
            tkfont.nametofont("TkFixedFont").actual("family")
        )
        self._fuentes: dict[tuple[float, bool, bool, bool], tkfont.Font] = {}
        self._fotos: list[ImageTk.PhotoImage] = []
        self._fotos_marca: list[ImageTk.PhotoImage] = []
        self._teclas: list[str] = []
        self._pendientes: list[str] = []
        self._visible = False
        self.foco_inicial: tk.Misc | None = None

        fondo = self.color("fondo")
        self.root.title(TITULO)
        self.root.resizable(False, False)
        self.root.configure(bg=fondo)
        set_window_icon(self.root)

        cuerpo = tk.Frame(
            self.root,
            bg=fondo,
            padx=self.px(PADDING_X - MARGEN),
        )
        cuerpo.pack(fill="both", expand=True, pady=(self.px(22), self.px(20 - MARGEN)))
        self._marca(cuerpo)
        self.pie = tk.Frame(cuerpo, bg=fondo)
        self.pie.pack(side="bottom", fill="x", pady=(self.px(12), 0))
        self.contenido = tk.Frame(cuerpo, bg=fondo)
        self.contenido.pack(side="top", fill="both", expand=True, pady=(self.px(16), 0))

    # ---- medidas, colores y fuentes ----

    def px(self, valor: float) -> int:
        return round(valor * self.escala)

    def color(self, token: str) -> str:
        return self.paleta.tk(token)

    def fuente(
        self,
        tamanio: float,
        *,
        negrita: bool = False,
        mono: bool = False,
        subrayada: bool = False,
    ) -> tkfont.Font:
        """Fuente de ``tamanio`` px (negativo en Tk = píxeles)."""
        clave = (tamanio, negrita, mono, subrayada)
        if clave not in self._fuentes:
            self._fuentes[clave] = tkfont.Font(
                root=self.root,
                family=self._familia_mono if mono else self._familia,
                size=-self.px(tamanio),
                weight="bold" if negrita else "normal",
                underline=subrayada,
            )
        return self._fuentes[clave]

    def foto(self, imagen: Image.Image) -> ImageTk.PhotoImage:
        """PhotoImage que vive lo que la ventana (Tk no guarda la referencia)."""
        foto = ImageTk.PhotoImage(imagen, master=self.root)
        self._fotos.append(foto)
        return foto

    def _marca(self, cuerpo: tk.Frame) -> None:
        fondo = self.paleta.pil("fondo")
        fila = tk.Frame(cuerpo, bg=self.color("fondo"))
        fila.pack(side="top", fill="x", padx=self.px(MARGEN))
        icono = rasterizar(ICONO_SVG, self.px(28), fondo=fondo)
        wordmark = rasterizar(
            WORDMARK_SVG,
            self.px(18),
            fondo=fondo,
            color_actual=self.paleta.pil("tinta"),
            colores={"wordmark-e": self.paleta.pil("acento")},
        )
        for imagen, separacion in ((icono, 0), (wordmark, self.px(10))):
            foto = ImageTk.PhotoImage(imagen, master=self.root)
            self._fotos_marca.append(foto)
            tk.Label(
                fila,
                image=foto,
                bg=self.color("fondo"),
                bd=0,
                highlightthickness=0,
            ).pack(side="left", padx=(separacion, 0))

    # ---- bloques de texto ----

    def texto(
        self,
        parent: tk.Misc,
        contenido: str,
        *,
        tamanio: float = 13,
        token: str = "tinta",
        negrita: bool = False,
        mono: bool = False,
    ) -> tk.Label:
        return tk.Label(
            parent,
            text=contenido,
            bg=self.color("fondo"),
            fg=self.color(token),
            font=self.fuente(tamanio, negrita=negrita, mono=mono),
            justify="left",
            anchor="w",
            wraplength=self.px(ANCHO_CONTENIDO),
            bd=0,
            padx=0,
            pady=0,
        )

    # ---- ciclo de vida ----

    def tecla(self, secuencia: str, accion: Callable[[Any], object]) -> None:
        """Atajo de teclado de la vista actual (se suelta al cambiar de vista)."""
        self.root.bind(secuencia, accion)
        self._teclas.append(secuencia)

    def despues(self, ms: int, accion: Callable[[], object]) -> None:
        """``after()`` de la vista actual: se cancela al cambiar de vista o al
        cerrar la ventana (si no, Tk llamaría a widgets que ya no existen)."""
        self._pendientes = [
            p for p in self._pendientes if p in self.root.tk.call("after", "info")
        ]
        self._pendientes.append(self.root.after(ms, accion))

    def _cancelar_pendientes(self) -> None:
        for pendiente in self._pendientes:
            try:
                self.root.after_cancel(pendiente)
            except tk.TclError:
                pass
        self._pendientes = []

    def mostrar(self) -> None:
        """Muestra la vista montada sin esperar: la primera vez centra la
        ventana y aplica la barra de título del tema; después solo ajusta el
        alto y deja la ventana donde está."""
        root = self.root
        root.update_idletasks()
        ancho = self.px(ANCHO_VENTANA)
        alto = max(self.px(ALTO_MINIMO), root.winfo_reqheight())
        if self._visible:
            root.geometry(f"{ancho}x{alto}")
        else:
            x = max(0, (root.winfo_screenwidth() - ancho) // 2)
            y = max(0, (root.winfo_screenheight() - alto) // 2)
            root.geometry(f"{ancho}x{alto}+{x}+{y}")
            if self.paleta.oscuro:
                barra_de_titulo_oscura(root)
            root.deiconify()
            root.lift()
            try:
                root.focus_force()
            except tk.TclError:
                pass
            self._visible = True
        if self.foco_inicial is not None:
            self.foco_inicial.focus_set()
        root.update_idletasks()

    def ejecutar(self) -> None:
        """Muestra la vista y bloquea hasta que llama a ``terminar``."""
        self.mostrar()
        self.root.mainloop()

    def terminar(self) -> None:
        """Termina la vista actual; la ventana queda abierta."""
        try:
            self.root.quit()
        except tk.TclError:
            pass

    def limpiar(self) -> None:
        """Vacía el contenido y el pie para la vista siguiente."""
        self._cancelar_pendientes()
        for marco in (self.contenido, self.pie):
            for hijo in marco.winfo_children():
                hijo.destroy()
        for secuencia in self._teclas:
            self.root.unbind(secuencia)
        self._teclas = []
        self._fotos = []
        self.foco_inicial = None
        # Entre vistas, la X no hace nada: cada vista define la suya.
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

    def viva(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def destruir(self) -> None:
        try:
            self._cancelar_pendientes()
            self.root.destroy()
        except tk.TclError:
            pass


class Control(tk.Canvas):
    """Base de los controles: hover, presionado y foco con anillo."""

    def __init__(
        self,
        ventana: Ventana,
        parent: tk.Misc,
        *,
        command: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            bg=ventana.color("fondo"),
            highlightthickness=0,
            bd=0,
            takefocus=1,
            cursor="hand2",
        )
        self.ventana = ventana
        self.command = command
        self.hover = False
        self.presionado = False
        self.con_foco = False
        self._fondo_id: int | None = None
        self._foto: ImageTk.PhotoImage | None = None
        self.bind("<Enter>", self._al_entrar)
        self.bind("<Leave>", self._al_salir)
        self.bind("<ButtonPress-1>", self._al_presionar)
        self.bind("<ButtonRelease-1>", self._al_soltar)
        self.bind("<FocusIn>", self._al_enfocar)
        self.bind("<FocusOut>", self._al_desenfocar)
        self.bind("<space>", self._por_teclado)

    def invocar(self) -> None:
        if self.command is not None:
            self.command()

    # Subclases: la imagen de fondo según el estado.
    def imagen(self) -> Image.Image:
        raise NotImplementedError

    def redibujar(self) -> None:
        if not self.winfo_exists():
            return
        foto = ImageTk.PhotoImage(self.imagen(), master=self)
        if self._fondo_id is None:
            self._fondo_id = self.create_image(0, 0, anchor="nw", image=foto)
        else:
            self.itemconfigure(self._fondo_id, image=foto)
        self.tag_lower(self._fondo_id)
        self._foto = foto

    def _al_entrar(self, _event: tk.Event) -> None:
        self.hover = True
        self.redibujar()

    def _al_salir(self, _event: tk.Event) -> None:
        self.hover = False
        self.presionado = False
        self.redibujar()

    def _al_presionar(self, _event: tk.Event) -> None:
        self.presionado = True
        self.focus_set()
        self.redibujar()

    def _al_soltar(self, event: tk.Event) -> None:
        adentro = (
            0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        )
        estaba = self.presionado
        self.presionado = False
        self.redibujar()
        if estaba and adentro:
            self.invocar()

    def _al_enfocar(self, _event: tk.Event) -> None:
        self.con_foco = True
        self.redibujar()

    def _al_desenfocar(self, _event: tk.Event) -> None:
        self.con_foco = False
        self.redibujar()

    def _por_teclado(self, _event: tk.Event) -> str:
        self.invocar()
        return "break"


class Boton(Control):
    """Botón de 34 px (o de texto, ``enlace``). Enter y espacio lo activan."""

    def __init__(
        self,
        ventana: Ventana,
        parent: tk.Misc,
        texto: str,
        *,
        variante: Variante = "secundario",
        command: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(ventana, parent, command=command)
        self.variante = variante
        self._texto_id = self.create_text(0, 0, anchor="center", text="")
        self.bind("<Return>", self._por_teclado)
        self.bind("<KP_Enter>", self._por_teclado)
        self.cambiar_texto(texto)

    def cambiar_texto(self, texto: str) -> None:
        v = self.ventana
        enlace = self.variante == "enlace"
        self._fuente = v.fuente(12.5 if enlace else 13, negrita=True)
        self._fuente_hover = v.fuente(12.5, negrita=True, subrayada=True)
        relleno_x = 0 if enlace else (14 if self.variante == "secundario" else 16)
        self.ancho = self._fuente.measure(texto) + 2 * v.px(relleno_x)
        self.alto = self._fuente.metrics("linespace") if enlace else v.px(34)
        m = v.px(MARGEN)
        self.configure(width=self.ancho + 2 * m, height=self.alto + 2 * m)
        self.itemconfigure(self._texto_id, text=texto)
        self.redibujar()

    def _colores(self) -> tuple[str | None, str, str | None]:
        """(relleno, texto, borde) en tokens para el estado actual."""
        if self.variante == "primario":
            return ("acento-hover" if self.hover else "acento", "sobre-acento", None)
        if self.variante == "peligro":
            return ("prod-fondo", "prod-texto", None)
        if self.variante == "enlace":
            return (None, "acento", None)
        relleno = "superficie-hover" if self.hover else "superficie"
        return (relleno, "tinta", "borde-control")

    def imagen(self) -> Image.Image:
        v = self.ventana
        p = v.paleta
        relleno, texto, borde = self._colores()
        m = v.px(MARGEN)
        desplazamiento = 1 if self.presionado else 0
        self.coords(
            self._texto_id,
            m + self.ancho / 2,
            m + self.alto / 2 + desplazamiento,
        )
        hover_enlace = self.variante == "enlace" and self.hover
        self.itemconfigure(
            self._texto_id,
            fill=v.color(texto),
            font=self._fuente_hover if hover_enlace else self._fuente,
        )
        return caja(
            self.ancho,
            self.alto,
            fondo=p.pil("fondo"),
            relleno=p.pil(relleno) if relleno else None,
            radio=v.px(4 if self.variante == "enlace" else RADIO),
            borde=p.pil(borde) if borde else None,
            margen=m,
            anillo=p.pil("acento") if self.con_foco else None,
            # Peligro no tiene un token de hover: anillo gris como la píldora
            # de la app.
            sombra=(
                p.pil("borde-fuerte")
                if self.variante == "peligro" and self.hover
                else None
            ),
        )


class Tarjeta(Control):
    """Tarjeta seleccionable del selector: punto, título, etiqueta y texto."""

    def __init__(
        self,
        ventana: Ventana,
        parent: tk.Misc,
        *,
        titulo: str,
        descripcion: str,
        punto: str,
        etiqueta: str | None = None,
        command: Callable[[], None] | None = None,
        al_abrir: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(ventana, parent, command=command)
        self.seleccionada = False
        self._punto = punto
        v = ventana
        m = v.px(MARGEN)
        self._pad_x, self._pad_y = v.px(16), v.px(14)
        self._punto_d = v.px(10)
        texto_x = m + self._pad_x + self._punto_d + v.px(12)
        self.ancho = v.px(ANCHO_CONTENIDO)
        ancho_texto = self.ancho - (texto_x - m) - self._pad_x

        fuente_titulo = v.fuente(14, negrita=True)
        self._titulo_id = self.create_text(
            texto_x, m + self._pad_y, anchor="nw", text=titulo, font=fuente_titulo
        )
        alto_titulo = fuente_titulo.metrics("linespace")
        self._etiqueta: tuple[int, int, int, int] | None = None
        self._etiqueta_id: int | None = None
        if etiqueta:
            fuente_etiqueta = v.fuente(11, negrita=True)
            ex = texto_x + fuente_titulo.measure(titulo) + v.px(8)
            ew = fuente_etiqueta.measure(etiqueta) + 2 * v.px(7)
            eh = fuente_etiqueta.metrics("linespace") + 2 * v.px(2)
            ey = m + self._pad_y + (alto_titulo - eh) // 2
            self._etiqueta = (ex, ey, ew, eh)
            self._etiqueta_id = self.create_text(
                ex + ew / 2,
                ey + eh / 2,
                anchor="center",
                text=etiqueta,
                font=fuente_etiqueta,
            )
        self._descripcion_id = self.create_text(
            texto_x,
            m + self._pad_y + alto_titulo + v.px(4),
            anchor="nw",
            text=descripcion,
            font=v.fuente(12.5),
            width=ancho_texto,
        )
        bbox = self.bbox(self._descripcion_id)
        abajo = bbox[3] if bbox else m + self._pad_y + alto_titulo
        self.alto = abajo - m + self._pad_y
        self._punto_y = m + self._pad_y + (alto_titulo - self._punto_d) // 2
        self.configure(width=self.ancho + 2 * m, height=self.alto + 2 * m)
        self.al_abrir = al_abrir
        self.bind("<Double-Button-1>", self._doble_click)
        self.redibujar()

    def seleccionar(self, seleccionada: bool) -> None:
        self.seleccionada = seleccionada
        self.redibujar()

    def _doble_click(self, _event: tk.Event) -> None:
        if self.al_abrir is not None:
            self.al_abrir()

    def imagen(self) -> Image.Image:
        v = self.ventana
        p = v.paleta
        m = v.px(MARGEN)
        if self.seleccionada:
            relleno, borde, grosor = "acento-tinte", "acento", 2
        elif self.hover:
            relleno, borde, grosor = "superficie-hover", "borde-fuerte", 1
        else:
            relleno, borde, grosor = "superficie", "borde", 1
        img = caja(
            self.ancho,
            self.alto,
            fondo=p.pil("fondo"),
            relleno=p.pil(relleno),
            radio=v.px(RADIO),
            borde=p.pil(borde),
            grosor=v.px(grosor),
            margen=m,
            anillo=p.pil("acento") if self.con_foco else None,
        )
        circulo(
            img, m + self._pad_x, self._punto_y, self._punto_d, p.pil(self._punto)
        )
        self.itemconfigure(self._titulo_id, fill=v.color("tinta"))
        self.itemconfigure(self._descripcion_id, fill=v.color("tinta-suave"))
        if self._etiqueta is not None and self._etiqueta_id is not None:
            ex, ey, ew, eh = self._etiqueta
            pastilla = caja(
                ew,
                eh,
                fondo=p.pil(relleno),
                relleno=p.pil("neutro-fondo"),
                radio=eh / 2,
            )
            img.paste(pastilla, (ex, ey))
            self.itemconfigure(self._etiqueta_id, fill=v.color("tinta-suave"))
        return img


def pildora(
    ventana: Ventana, parent: tk.Misc, ambiente: ArcaEnvironment, texto: str
) -> tk.Canvas:
    """Píldora del ambiente, como en el encabezado de la app."""
    v = ventana
    p = v.paleta
    fondo, tinta, borde = _PILDORA[ambiente]
    fuente = v.fuente(12, negrita=True)
    alto = v.px(26)
    punto = v.px(8)
    pad = v.px(11)
    ancho = pad + punto + v.px(7) + fuente.measure(texto) + pad
    canvas = tk.Canvas(
        parent,
        width=ancho,
        height=alto,
        bg=v.color("fondo"),
        highlightthickness=0,
        bd=0,
        takefocus=0,
    )
    img = caja(
        ancho,
        alto,
        fondo=p.pil("fondo"),
        relleno=p.pil(fondo),
        radio=alto / 2,
        borde=p.pil(borde),
    )
    circulo(img, pad, (alto - punto) // 2, punto, p.pil(tinta))
    canvas.create_image(0, 0, anchor="nw", image=v.foto(img))
    canvas.create_text(
        pad + punto + v.px(7),
        alto / 2,
        anchor="w",
        text=texto,
        font=fuente,
        fill=v.color(tinta),
    )
    return canvas


def panel_error(
    ventana: Ventana,
    parent: tk.Misc,
    texto: str,
    *,
    titulo: str | None = None,
    con_icono: bool = False,
) -> tk.Canvas:
    """Panel de error de la app: fondo, borde y texto ``--error-*``."""
    return _panel(ventana, parent, texto, "error", titulo, con_icono)


def panel_aviso(
    ventana: Ventana,
    parent: tk.Misc,
    texto: str,
    *,
    titulo: str | None = None,
    con_icono: bool = False,
) -> tk.Canvas:
    """Panel de aviso de la app: fondo, borde y texto ``--aviso-*``."""
    return _panel(ventana, parent, texto, "aviso", titulo, con_icono)


def _panel(
    ventana: Ventana,
    parent: tk.Misc,
    texto: str,
    tono: str,
    titulo: str | None,
    con_icono: bool,
) -> tk.Canvas:
    v = ventana
    p = v.paleta
    ancho = v.px(ANCHO_CONTENIDO)
    pad_x, pad_y = (v.px(16), v.px(14)) if con_icono else (v.px(14), v.px(12))
    canvas = tk.Canvas(
        parent,
        width=ancho,
        height=1,
        bg=v.color("fondo"),
        highlightthickness=0,
        bd=0,
        takefocus=0,
    )
    lado_icono = v.px(18)
    x = pad_x + (lado_icono + v.px(10) if con_icono else 0)
    ancho_texto = ancho - x - pad_x
    y = pad_y
    tinta = v.color(f"{tono}-tinta")
    if titulo:
        item = canvas.create_text(
            x, y, anchor="nw", text=titulo, font=v.fuente(14, negrita=True),
            fill=tinta, width=ancho_texto,
        )
        caja_titulo = canvas.bbox(item)
        y = (caja_titulo[3] if caja_titulo else y) + v.px(6)
    item = canvas.create_text(
        x, y, anchor="nw", text=texto, font=v.fuente(13), fill=tinta, width=ancho_texto
    )
    caja_texto = canvas.bbox(item)
    alto = (caja_texto[3] if caja_texto else y) + pad_y
    canvas.configure(height=alto)
    img = caja(
        ancho,
        alto,
        fondo=p.pil("fondo"),
        relleno=p.pil(f"{tono}-fondo"),
        radio=v.px(RADIO),
        borde=p.pil(f"{tono}-borde"),
    )
    if con_icono:
        icono = icono_error(
            lado_icono, color=p.pil(tono), fondo=p.pil(f"{tono}-fondo")
        )
        img.paste(icono, (pad_x, pad_y + v.px(1)))
    fondo_id = canvas.create_image(0, 0, anchor="nw", image=v.foto(img))
    canvas.tag_lower(fondo_id)
    return canvas

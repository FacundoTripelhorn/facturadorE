"""Progreso del launcher: arrancar (o cambiar de ambiente) sin quedar sin
ventana.

Entre elegir el ambiente y que abra la app, la ventana del launcher muestra
el progreso: "Iniciando <ambiente>" o, en un cambio, "Cambiando a
<destino>" con el paso "<actual> cerrada". El trabajo lento
(``supervisor.start()`` / ``stop()``) corre en un hilo; el ``mainloop`` de
Tk sigue en el hilo principal y anima la barra con ``after()``.

"Cancelar" (o Esc, o la X) detiene el backend que se estaba levantando:
``stop()``, esperar al hilo y ``stop()`` otra vez, por si el hijo arrancó
justo después del primero. Así no quedan procesos huérfanos.

Sin ventana (con terminal y sin selector gráfico, bajo pytest, con
``FACTURADOR_NO_DIALOGS`` o con ``--no-browser``) todo corre igual que
antes, en el hilo principal.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ..constants import ArcaEnvironment
from .frozen import has_interactive_terminal

_log = logging.getLogger(__name__)

T = TypeVar("T")

_TITULOS = {
    ArcaEnvironment.HOMO: "Homologación",
    ArcaEnvironment.PROD: "Producción",
}
_VIGILAR_MS = 50
_CUADRO_MS = 16
# Una pasada de la barra indeterminada, en segundos.
_PASADA_S = 1.4


class StartupCancelled(Exception):
    """El usuario canceló el arranque desde la ventana de progreso."""


class StartupUI(Protocol):
    """Cómo se muestra el arranque. Los tests inyectan un doble."""

    def start(
        self,
        environment: ArcaEnvironment,
        start: Callable[[], T],
        cancel: Callable[[], None],
        *,
        switching_from: ArcaEnvironment | None = None,
    ) -> T:
        """Corre ``start``; ``StartupCancelled`` si el usuario canceló (ya
        con ``cancel`` aplicado)."""
        ...

    def stop_for_switch(
        self,
        current: ArcaEnvironment,
        target: ArcaEnvironment,
        stop: Callable[[], None],
    ) -> bool:
        """Corre ``stop`` del ambiente actual; True si el usuario pidió
        cancelar mientras tanto (no se arranca el destino)."""
        ...


def window_open() -> bool:
    """True si la ventana del launcher está abierta (sin importar Tk si
    nunca se usó)."""
    widgets = sys.modules.get(f"{__package__}.widgets")
    return bool(widgets is not None and widgets.hay_ventana())


def close_window() -> None:
    """Cierra la ventana del launcher, si hay una: antes de abrir la app o
    al terminar."""
    widgets = sys.modules.get(f"{__package__}.widgets")
    if widgets is not None:
        widgets.cerrar_ventana()


@dataclass
class StartupWindow:
    """``StartupUI`` por defecto: la vista de progreso en la ventana del
    launcher cuando corresponde; si no, el trabajo en el hilo principal."""

    show: bool = True

    def wanted(self) -> bool:
        if not self.show:
            return False
        if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get(
            "FACTURADOR_NO_DIALOGS"
        ):
            return False
        # Con terminal, solo si ya hay ventana (se eligió en el selector).
        return window_open() or not has_interactive_terminal()

    def start(
        self,
        environment: ArcaEnvironment,
        start: Callable[[], T],
        cancel: Callable[[], None],
        *,
        switching_from: ArcaEnvironment | None = None,
    ) -> T:
        vista = self._vista(environment, switching_from=switching_from, cerrando=False)
        if vista is None:
            return start()
        valor, cancelado = vista.correr(start, cancelar=cancel)
        if cancelado:
            raise StartupCancelled
        return valor

    def stop_for_switch(
        self,
        current: ArcaEnvironment,
        target: ArcaEnvironment,
        stop: Callable[[], None],
    ) -> bool:
        vista = self._vista(target, switching_from=current, cerrando=True)
        if vista is None:
            stop()
            return False
        # El apagado no se interrumpe (invariante: el actual se apaga antes
        # de arrancar el otro); cancelar solo evita arrancar el destino.
        _, cancelado = vista.correr(stop, cancelar=None)
        return cancelado

    def _vista(
        self,
        environment: ArcaEnvironment,
        *,
        switching_from: ArcaEnvironment | None,
        cerrando: bool,
    ) -> _VistaProgreso | None:
        if not self.wanted():
            return None
        try:
            return _VistaProgreso(
                environment, switching_from=switching_from, cerrando=cerrando
            )
        except Exception:
            # Sin Tk o sin pantalla: se arranca igual, sin ventana.
            _log.warning("no se pudo mostrar el progreso", exc_info=True)
            return None


def textos(
    environment: ArcaEnvironment,
    *,
    switching_from: ArcaEnvironment | None,
    cerrando: bool,
) -> tuple[str, str, list[tuple[bool, str]]]:
    """(título, explicación, pasos) de la vista. Cada paso es
    (cumplido, texto)."""
    destino = _TITULOS[environment]
    if switching_from is None:
        return (
            f"Iniciando {destino}",
            "Preparando el servidor local. La app se abre sola cuando esté lista.",
            [(False, "Esperando al servidor…")],
        )
    actual = _TITULOS[switching_from]
    pasos = (
        [(False, f"Cerrando {actual}…")]
        if cerrando
        else [(True, f"{actual} cerrada"), (False, f"Iniciando {destino}…")]
    )
    return (
        f"Cambiando a {destino}",
        f"Cerramos {actual} y abrimos {destino}. La app se abre sola cuando "
        "esté lista.",
        pasos,
    )


class _VistaProgreso:
    """La vista de progreso montada en la ventana del launcher."""

    def __init__(
        self,
        environment: ArcaEnvironment,
        *,
        switching_from: ArcaEnvironment | None,
        cerrando: bool,
    ) -> None:
        import tkinter as tk

        from .theme import MARGEN, caja, icono_check
        from .widgets import ANCHO_CONTENIDO, Boton, pildora, ventana

        v = ventana()
        self.v = v
        p = v.paleta
        margen = v.px(MARGEN)
        titulo, explicacion, pasos = textos(
            environment, switching_from=switching_from, cerrando=cerrando
        )

        pildora(v, v.contenido, environment, _TITULOS[environment]).pack(
            side="top", anchor="w", padx=margen
        )
        v.texto(v.contenido, titulo, tamanio=17, negrita=True).pack(
            side="top", anchor="w", padx=margen, pady=(v.px(16), 0)
        )
        v.texto(v.contenido, explicacion, token="tinta-suave").pack(
            side="top", anchor="w", padx=margen, pady=(v.px(12), 0)
        )

        # Barra indeterminada: un tramo de --acento que recorre la pista.
        ancho, alto = v.px(ANCHO_CONTENIDO), v.px(4)
        self._ancho = ancho
        self._tramo = round(ancho * 0.34)
        barra = tk.Canvas(
            v.contenido,
            width=ancho,
            height=alto,
            bg=v.color("fondo"),
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        barra.pack(side="top", anchor="w", padx=margen, pady=(v.px(20), 0))
        pista = caja(
            ancho,
            alto,
            fondo=p.pil("fondo"),
            relleno=p.pil("neutro-fondo"),
            radio=alto / 2,
        )
        tramo = caja(
            self._tramo,
            alto,
            fondo=p.pil("neutro-fondo"),
            relleno=p.pil("acento"),
            radio=alto / 2,
        )
        barra.create_image(0, 0, anchor="nw", image=v.foto(pista))
        self._barra = barra
        self._tramo_id = barra.create_image(
            -self._tramo, 0, anchor="nw", image=v.foto(tramo)
        )
        self._inicio = time.monotonic()

        lista = tk.Frame(v.contenido, bg=v.color("fondo"))
        lista.pack(side="top", anchor="w", padx=margen, pady=(v.px(12), 0))
        lado = v.px(12)
        check = v.foto(icono_check(lado, color=p.pil("ok"), fondo=p.pil("fondo")))
        self._estado: tk.Label | None = None
        for cumplido, texto in pasos:
            fila = tk.Frame(lista, bg=v.color("fondo"))
            fila.pack(side="top", anchor="w", pady=(0, v.px(6)))
            token = "ok" if cumplido else "tinta-suave"
            marca: dict[str, Any] = (
                {"image": check}
                if cumplido
                else {"text": "·", "font": v.fuente(11.5, mono=True)}
            )
            tk.Label(
                fila,
                width=lado if cumplido else 0,
                bg=v.color("fondo"),
                fg=v.color(token),
                bd=0,
                padx=0 if cumplido else v.px(3),
                pady=0,
                **marca,
            ).pack(side="left")
            etiqueta = v.texto(fila, texto, tamanio=11.5, token=token, mono=True)
            etiqueta.pack(side="left", padx=(v.px(7), 0))
            if not cumplido:
                self._estado = etiqueta

        self._cancelar_boton = Boton(v, v.pie, "Cancelar", command=self._cancelar)
        self._cancelar_boton.pack(side="right")
        v.tecla("<Escape>", lambda _e: self._cancelar())
        v.root.protocol("WM_DELETE_WINDOW", self._cancelar)
        v.foco_inicial = self._cancelar_boton

        self._arranque: _Arranque[Any] | None = None
        # Visible ya, antes de que arranque el trabajo lento.
        v.mostrar()

    # ---- trabajo en segundo plano ----

    def correr(
        self, tarea: Callable[[], T], *, cancelar: Callable[[], None] | None
    ) -> tuple[T, bool]:
        """Corre ``tarea`` en un hilo mientras la ventana anima la barra.

        Devuelve (resultado, cancelado); ver ``_Arranque.resultado``.
        """
        arranque = _Arranque(tarea, cancelar)
        self._arranque = arranque
        arranque.iniciar()
        self._animar()
        # Desde el mainloop: un quit() antes de que arranque se perdería y la
        # vista quedaría esperando (p. ej. si start() falla enseguida).
        self.v.despues(_VIGILAR_MS, self._vigilar)
        try:
            self.v.ejecutar()
        except KeyboardInterrupt:
            # Ctrl+C con la ventana abierta: no dejar el backend a medias.
            arranque.detener()
            raise
        return arranque.resultado()

    def _vigilar(self) -> None:
        if self._arranque is not None and self._arranque.terminado():
            self.v.terminar()
            return
        self.v.despues(_VIGILAR_MS, self._vigilar)

    def _animar(self) -> None:
        if not self._barra.winfo_exists():
            return
        fraccion = ((time.monotonic() - self._inicio) / _PASADA_S) % 1.0
        x = -self._tramo + (self._ancho + self._tramo) * fraccion
        self._barra.coords(self._tramo_id, x, 0)
        self.v.despues(_CUADRO_MS, self._animar)

    def _cancelar(self) -> None:
        if self._arranque is None or not self._arranque.pedir_cancelar():
            return
        self._cancelar_boton.command = None
        self._cancelar_boton.cambiar_texto("Cancelando…")
        if self._estado is not None:
            self._estado.configure(text="Deteniendo…")


class _Arranque[R]:
    """Una tarea lenta en un hilo, con cancelación sin carreras.

    Con ``cancelar`` (arranque), cancelar es: ``cancelar()``, esperar al
    hilo y ``cancelar()`` otra vez, por si el hijo arrancó justo después
    del primero; corre en otro hilo para no trabar la ventana. Sin
    ``cancelar`` (cierre del ambiente actual), la tarea termina igual y solo
    se informa el pedido.
    """

    def __init__(
        self, tarea: Callable[[], R], cancelar: Callable[[], None] | None
    ) -> None:
        self._tarea = tarea
        self._cancelar = cancelar
        self._valor: R | None = None
        self._error: BaseException | None = None
        self._hilo = threading.Thread(
            target=self._trabajar, name="launcher-progreso", daemon=True
        )
        self._hecho = threading.Event()
        self._detenido = threading.Event()
        self.cancelado = False

    def iniciar(self) -> None:
        self._hilo.start()

    def _trabajar(self) -> None:
        try:
            self._valor = self._tarea()
        except BaseException as exc:  # se relanza en el hilo principal
            self._error = exc
        finally:
            self._hecho.set()

    def pedir_cancelar(self) -> bool:
        """Pide cancelar; False si ya se había pedido."""
        if self.cancelado:
            return False
        self.cancelado = True
        if self._cancelar is not None:
            threading.Thread(
                target=self.detener, name="launcher-cancelar", daemon=True
            ).start()
        return True

    def detener(self) -> None:
        """Apaga lo que se estaba levantando y espera al hilo."""
        try:
            if self._cancelar is not None:
                _intentar(self._cancelar)
            self._hilo.join()
            # Si el hijo arrancó después del primer cancelar(), este lo apaga.
            if self._cancelar is not None:
                _intentar(self._cancelar)
        finally:
            self._detenido.set()

    def terminado(self) -> bool:
        """True cuando la ventana puede pasar a lo que sigue."""
        if self.cancelado and self._cancelar is not None:
            return self._detenido.is_set()
        return self._hecho.is_set()

    def esperar(self, timeout: float | None = None) -> bool:
        """Espera a ``terminado()``; para usar sin ventana."""
        evento = (
            self._detenido
            if self.cancelado and self._cancelar is not None
            else self._hecho
        )
        return evento.wait(timeout)

    def resultado(self) -> tuple[R, bool]:
        """(valor, cancelado). Con ``cancelar`` y cancelado, sin valor (lo
        que haya arrancado ya se apagó). Si no, los errores de la tarea se
        relanzan acá, en el hilo principal."""
        if self.cancelado and self._cancelar is not None:
            self._detenido.wait()
            return None, True  # type: ignore[return-value]
        self._hecho.wait()
        if self._error is not None:
            raise self._error
        return self._valor, self.cancelado  # type: ignore[return-value]


def _intentar(accion: Callable[[], None]) -> None:
    try:
        accion()
    except Exception:
        _log.warning("no se pudo detener el arranque", exc_info=True)

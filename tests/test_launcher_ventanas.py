"""Humo de las ventanas Tk del launcher (selector, confirmación de
Producción y error): se programan teclas o clicks y se verifica el
resultado. Sin tkinter o sin pantalla, se saltean."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

import pytest

from facturador.constants import ArcaEnvironment
from facturador.launcher.chooser import ENVIRONMENT_OPTIONS, prompt_environment_gui
from facturador.launcher.failure_window import FailureAction, show_failure_window
from facturador.launcher.fallback_window import (
    HELP_URL,
    show_browser_fallback_window,
)
from facturador.launcher.production_ack import prompt_production_confirm_gui
from facturador.launcher.progress import StartupCancelled, StartupWindow
from facturador.launcher.supervisor import LauncherError

tkinter = pytest.importorskip("tkinter")


@pytest.fixture
def programar(
    monkeypatch,
) -> Iterator[Callable[[Callable[[tkinter.Tk], None]], None]]:
    """Corre ``accion(root)`` apenas arranca el mainloop de la ventana."""
    try:
        sonda = tkinter.Tk()
        sonda.destroy()
    except tkinter.TclError as exc:
        pytest.skip(f"display no disponible para tkinter: {exc}")

    acciones: list[Callable[[tkinter.Tk], None]] = []
    original = tkinter.Tk.mainloop

    def _mainloop(self: tkinter.Tk, n: int = 0) -> None:
        accion = acciones.pop(0)

        def _correr() -> None:
            self.update()
            accion(self)

        self.after(50, _correr)
        original(self, n)

    monkeypatch.setattr(tkinter.Tk, "mainloop", _mainloop)
    yield acciones.append
    # Las vistas comparten la ventana: se cierra al terminar cada test.
    from facturador.launcher.widgets import cerrar_ventana

    cerrar_ventana()


def _botones(root: tkinter.Misc) -> dict[str, tkinter.Misc]:
    from facturador.launcher.widgets import Boton

    encontrados: dict[str, tkinter.Misc] = {}

    def _recorrer(widget: tkinter.Misc) -> None:
        if isinstance(widget, Boton):
            encontrados[widget.itemcget(widget._texto_id, "text")] = widget
        for hijo in widget.winfo_children():
            _recorrer(hijo)

    _recorrer(root)
    return encontrados


def test_selector_abre_el_preseleccionado(programar):
    programar(lambda root: _botones(root)["Abrir Producción"].invocar())
    elegido = prompt_environment_gui(
        ENVIRONMENT_OPTIONS, selected=ArcaEnvironment.PROD
    )
    assert elegido is ArcaEnvironment.PROD


def test_selector_flechas_y_enter(programar):
    def _teclas(root: tkinter.Tk) -> None:
        root.event_generate("<Down>")
        root.update()
        assert "Abrir Producción" in _botones(root)
        root.event_generate("<Return>")

    programar(_teclas)
    assert prompt_environment_gui(ENVIRONMENT_OPTIONS) is ArcaEnvironment.PROD


def test_selector_esc_cancela(programar):
    programar(lambda root: root.event_generate("<Escape>"))
    assert prompt_environment_gui(ENVIRONMENT_OPTIONS) is None


def test_confirmacion_arranca_con_foco_en_cancelar(programar):
    def _enter(root: tkinter.Tk) -> None:
        foco = root.focus_get()
        assert foco is _botones(root)["Cancelar"]
        foco.event_generate("<Return>")

    programar(_enter)
    assert prompt_production_confirm_gui() is False


def test_confirmacion_acepta(programar):
    programar(lambda root: _botones(root)["Entiendo: abrir Producción"].invocar())
    assert prompt_production_confirm_gui() is True


def test_error_reintentar(programar):
    programar(lambda root: _botones(root)["Reintentar"].invocar())
    accion = show_failure_window(
        "puerto ocupado", environment=ArcaEnvironment.HOMO, can_choose=True
    )
    assert accion is FailureAction.RETRY


@pytest.mark.parametrize(
    ("can_choose", "esperada", "boton"),
    [
        (True, FailureAction.CHOOSE, "Elegir otro ambiente"),
        (False, FailureAction.CLOSE, "Cerrar"),
    ],
)
def test_error_esc_vuelve_o_cierra(programar, can_choose, esperada, boton):
    def _esc(root: tkinter.Tk) -> None:
        assert boton in _botones(root)
        root.event_generate("<Escape>")

    programar(_esc)
    accion = show_failure_window(
        "puerto ocupado", environment=ArcaEnvironment.PROD, can_choose=can_choose
    )
    assert accion is esperada


def test_error_sin_ambiente_no_ofrece_reintentar(programar):
    def _revisar(root: tkinter.Tk) -> None:
        assert set(_botones(root)) == {"Copiar detalle técnico", "Cerrar"}
        _botones(root)["Cerrar"].invocar()

    programar(_revisar)
    accion = show_failure_window(
        "--restore requiere --env", environment=None, can_choose=False
    )
    assert accion is FailureAction.CLOSE


def test_aviso_de_navegador_solo_abrir_cierra_el_aviso(programar, tmp_path):
    """Ver registro, Copiar y Ver ayuda no cierran el aviso ni abren la app;
    recién "Abrir en el navegador" devuelve el control al launcher."""
    registro = tmp_path / "launcher.log"
    registro.write_text("x", encoding="utf-8")
    archivos: list[object] = []
    urls: list[str] = []
    vistos: list[set[str]] = []

    def _recorrer(root: tkinter.Tk) -> None:
        botones = _botones(root)
        vistos.append(set(botones))
        botones["Ver registro"].invocar()
        botones["Copiar detalle técnico"].invocar()
        botones["Ver ayuda"].invocar()
        assert "Python.Runtime" in root.clipboard_get()
        assert "Homologación" in root.clipboard_get()
        botones["Abrir en el navegador"].invocar()

    programar(_recorrer)
    show_browser_fallback_window(
        "Failed to resolve Python.Runtime.Loader.Initialize",
        environment=ArcaEnvironment.HOMO,
        log_path=registro,
        open_file=archivos.append,
        open_url=urls.append,
    )
    assert vistos == [
        {"Abrir en el navegador", "Ver registro", "Copiar detalle técnico", "Ver ayuda"}
    ]
    assert archivos == [registro]
    assert urls == [HELP_URL]


def test_aviso_de_navegador_esc_sigue_al_navegador(programar):
    def _esc(root: tkinter.Tk) -> None:
        # Sin launcher.log (fuera del exe) no se ofrece "Ver registro".
        assert "Ver registro" not in _botones(root)
        root.event_generate("<Escape>")

    programar(_esc)
    show_browser_fallback_window("no gui", environment=ArcaEnvironment.PROD)


# --- una sola ventana: selector → progreso ------------------------------------


class _ConVentana(StartupWindow):
    """El progreso con ventana aunque corra bajo pytest."""

    def wanted(self) -> bool:
        return True


def test_el_progreso_usa_la_misma_ventana_del_selector(programar):
    from facturador.launcher import widgets

    programar(lambda root: _botones(root)["Abrir Producción"].invocar())
    assert (
        prompt_environment_gui(ENVIRONMENT_OPTIONS, selected=ArcaEnvironment.PROD)
        is ArcaEnvironment.PROD
    )
    raiz = widgets._abierta.root  # type: ignore[union-attr]
    assert widgets.hay_ventana()  # sigue abierta entre vistas

    hilos: list[str] = []

    def _start() -> str:
        hilos.append(threading.current_thread().name)
        time.sleep(0.3)
        return "listo"

    programar(lambda root: None)
    assert _ConVentana().start(ArcaEnvironment.PROD, _start, lambda: None) == "listo"
    # Corrió en el hilo de trabajo, con la ventana de progreso.
    assert hilos == ["launcher-progreso"]
    assert widgets._abierta is not None and widgets._abierta.root is raiz
    widgets.cerrar_ventana()
    assert not widgets.hay_ventana()


def test_progreso_esc_cancela_y_apaga(programar):
    soltar = threading.Event()
    detenidos: list[str] = []

    def _start() -> str:
        soltar.wait(5)
        return "listo"

    def _stop() -> None:
        detenidos.append("stop")
        soltar.set()

    programar(lambda root: root.event_generate("<Escape>"))
    with pytest.raises(StartupCancelled):
        _ConVentana().start(ArcaEnvironment.HOMO, _start, _stop)
    # Se reintenta mientras el arranque sigue y una vez más al final.
    assert len(detenidos) >= 2 and set(detenidos) == {"stop"}


def test_progreso_relanza_el_error_del_arranque(programar):
    def _start() -> str:
        raise LauncherError("puerto ocupado")

    programar(lambda root: None)
    with pytest.raises(LauncherError, match="puerto ocupado"):
        _ConVentana().start(ArcaEnvironment.HOMO, _start, lambda: None)


def test_cambio_cancelado_espera_el_cierre_del_actual(programar):
    cerrado = threading.Event()

    def _stop_actual() -> None:
        time.sleep(0.3)
        cerrado.set()

    programar(lambda root: _botones(root)["Cancelar"].invocar())
    cancelado = _ConVentana().stop_for_switch(
        ArcaEnvironment.HOMO, ArcaEnvironment.PROD, _stop_actual
    )
    assert cancelado is True
    assert cerrado.is_set()

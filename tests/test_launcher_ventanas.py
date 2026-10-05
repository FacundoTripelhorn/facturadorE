"""Humo de las ventanas Tk del launcher (selector, confirmación de
Producción y error): se programan teclas o clicks y se verifica el
resultado. Sin tkinter o sin pantalla, se saltean."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from facturador.constants import ArcaEnvironment
from facturador.launcher.chooser import ENVIRONMENT_OPTIONS, prompt_environment_gui
from facturador.launcher.failure_window import FailureAction, show_failure_window
from facturador.launcher.production_ack import prompt_production_confirm_gui

tkinter = pytest.importorskip("tkinter")


@pytest.fixture
def programar(monkeypatch) -> Callable[[Callable[[tkinter.Tk], None]], None]:
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
    return acciones.append


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

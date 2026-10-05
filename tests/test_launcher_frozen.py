"""Ejecutable empaquetado de Windows: modo backend, streams, PATH, ícono.

El build real (PyInstaller) corre en CI de Windows
(.github/workflows/windows.yml); acá se prueba la lógica de
``facturador.launcher.frozen`` simulando el entorno congelado.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from facturador.launcher import __main__ as launcher_main
from facturador.launcher import frozen
from facturador.launcher.command import build_backend_command


@pytest.fixture
def congelado(monkeypatch, tmp_path):
    """Simula el exe de PyInstaller: sys.frozen, sys._MEIPASS y el exe."""
    bundle = tmp_path / "_internal"
    bundle.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "FacturadorE.exe"))
    return bundle


def test_version_del_build_en_el_exe(congelado):
    from facturador.launcher.failure_window import app_version

    (congelado / frozen.VERSION_NAME).write_text("dev-ee54723", encoding="utf-8")
    assert frozen.build_version() == "dev-ee54723"
    assert app_version() == "dev-ee54723"


def test_version_sin_archivo_usa_la_del_paquete(congelado):
    from importlib import metadata

    from facturador.launcher.failure_window import app_version

    assert frozen.build_version() is None
    assert app_version() == metadata.version("facturador")


def test_backend_fuera_del_exe_usa_python_m_facturador():
    assert build_backend_command() == [sys.executable, "-m", "facturador"]


def test_backend_en_el_exe_relanza_el_mismo_exe(congelado, tmp_path):
    assert build_backend_command() == [
        str(tmp_path / "FacturadorE.exe"),
        frozen.BACKEND_FLAG,
    ]
    # Un python explícito (tests / herramientas) sigue ganando.
    assert build_backend_command(python="py") == ["py", "-m", "facturador"]


def test_main_despacha_backend_o_launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTURADOR_APP_DATA", str(tmp_path))
    llamadas: list[object] = []
    monkeypatch.setattr(
        "facturador.__main__.main", lambda: llamadas.append("backend")
    )
    monkeypatch.setattr(
        launcher_main, "main", lambda argv: llamadas.append(argv) or 7
    )

    assert frozen.main([frozen.BACKEND_FLAG]) == 0
    assert frozen.main(["--env", "homo"]) == 7
    assert llamadas == ["backend", ["--env", "homo"]]


def test_sin_consola_la_salida_va_a_launcher_log(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    log = frozen.redirect_missing_streams(tmp_path / "app-data")

    assert log == tmp_path / "app-data" / frozen.LAUNCHER_LOG
    print("hola", flush=True)
    print("error", file=sys.stderr, flush=True)
    assert log.read_text(encoding="utf-8") == "hola\nerror\n"
    sys.stdout.close()


def test_con_consola_no_toca_los_streams(tmp_path):
    antes = (sys.stdout, sys.stderr)
    assert frozen.redirect_missing_streams(tmp_path) is None
    assert (sys.stdout, sys.stderr) == antes


def test_log_grande_se_recorta(monkeypatch, tmp_path):
    log = tmp_path / frozen.LAUNCHER_LOG
    log.write_text("x" * (frozen._LOG_MAX_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    frozen.redirect_missing_streams(tmp_path)
    print("nuevo", flush=True)

    assert log.read_text(encoding="utf-8") == "nuevo\n"
    sys.stdout.close()


def test_log_que_no_se_puede_abrir_no_rompe(monkeypatch, tmp_path):
    bloqueo = tmp_path / "archivo"
    bloqueo.write_text("no es un directorio", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    assert frozen.redirect_missing_streams(bloqueo / "sub") == Path(os.devnull)
    print("se pierde", flush=True)  # no levanta
    sys.stdout.close()


def test_age_del_bundle_entra_al_path(congelado):
    (congelado / "bin").mkdir()
    env = {"PATH": "otro"}

    frozen.add_bundled_tools_to_path(env)
    frozen.add_bundled_tools_to_path(env)  # idempotente

    assert env["PATH"].split(os.pathsep) == [str(congelado / "bin"), "otro"]


def test_fuera_del_exe_el_path_no_cambia():
    env = {"PATH": "x"}
    frozen.add_bundled_tools_to_path(env)
    assert env == {"PATH": "x"}


def test_icono_solo_si_viaja_en_el_bundle(congelado):
    assert frozen.app_icon_path() is None
    (congelado / frozen.ICON_NAME).write_bytes(b"ico")
    assert frozen.app_icon_path() == congelado / frozen.ICON_NAME


def test_set_window_icon_tolera_errores_de_tk(congelado):
    (congelado / frozen.ICON_NAME).write_bytes(b"ico")

    class RootQueFalla:
        def iconbitmap(self, **kwargs):
            raise RuntimeError("bitmap inválido")

    frozen.set_window_icon(RootQueFalla())  # no levanta


def test_report_failure_sin_streams_no_rompe(monkeypatch):
    """El exe de ventana arranca con stdin/stderr en None."""
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert launcher_main._is_interactive_terminal() is False
    launcher_main._report_failure("algo falló")  # bajo pytest: sin diálogo


@pytest.mark.parametrize(
    "linea",
    [
        # Linux, macOS y Windows (texto del SO traducido): asyncio pone el
        # errno entre corchetes.
        "ERROR:    [Errno 98] error while attempting to bind on address "
        "('127.0.0.1', 8399): address already in use",
        "ERROR:    [Errno 48] error while attempting to bind on address "
        "('127.0.0.1', 8399): address already in use",
        "ERROR:    [Errno 10048] error while attempting to bind on address "
        "('127.0.0.1', 8399): solo se permite un uso de cada dirección de socket",
    ],
)
def test_puerto_ocupado_se_reconoce_en_cada_sistema(linea):
    from facturador.launcher.supervisor import _PUERTO_OCUPADO

    assert _PUERTO_OCUPADO.search(linea)


def test_otro_error_de_bind_no_se_confunde_con_puerto_ocupado():
    from facturador.launcher.supervisor import _PUERTO_OCUPADO

    assert not _PUERTO_OCUPADO.search(
        "[Errno 10013] error while attempting to bind on address: acceso denegado"
    )


def test_chooser_sin_tkinter_ni_stdin_explica_en_vez_de_romper(monkeypatch):
    """Exe de ventana (stdin None) donde tkinter no abre: ChooserUnavailable,
    que el launcher muestra como error, no un AttributeError."""
    from facturador.launcher.chooser import ChooserUnavailable, choose_environment
    from facturador.launcher.production_ack import confirm_production_first_use

    monkeypatch.setattr(sys, "stdin", None)

    def sin_gui(*args, **kwargs):
        raise ChooserUnavailable("tkinter no disponible")

    with pytest.raises(ChooserUnavailable):
        choose_environment(prompt_gui=sin_gui)
    with pytest.raises(ChooserUnavailable):
        confirm_production_first_use(prompt_gui=sin_gui)


def test_log_que_no_se_puede_recortar_sigue_agregando(monkeypatch, tmp_path):
    """Windows: otro launcher tiene el log abierto y no se puede borrar."""
    log = tmp_path / frozen.LAUNCHER_LOG
    log.write_text("x" * (frozen._LOG_MAX_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    def ocupado(self, *args, **kwargs):
        raise PermissionError("en uso por otro proceso")

    monkeypatch.setattr(Path, "unlink", ocupado)

    assert frozen.redirect_missing_streams(tmp_path) == log
    print("sigue", flush=True)
    assert log.read_text(encoding="utf-8").endswith("sigue\n")
    sys.stdout.close()

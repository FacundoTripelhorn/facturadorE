"""Launcher con el tema de la app: colores desde app.css, marca dibujada con
el renderer compartido, último ambiente usado y la ventana de error en el
flujo (Reintentar / Elegir otro ambiente / Cerrar)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from facturador.constants import ArcaEnvironment
from facturador.launcher import theme
from facturador.launcher.failure_window import (
    FailureAction,
    prompt_failure,
    technical_detail,
)
from facturador.launcher.last_environment import (
    LAST_ENVIRONMENT_FILENAME,
    read_last_environment,
    save_last_environment,
)
from facturador.launcher.supervisor import LauncherError
from facturador.marca import WORDMARK_SVG, rasterizar

LAUNCHER_DIR = Path(theme.__file__).resolve().parent
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", re.IGNORECASE)


# --- guardias: una sola fuente de colores ----------------------------------


@pytest.mark.parametrize(
    "modulo", sorted(LAUNCHER_DIR.glob("*.py")), ids=lambda p: p.name
)
def test_launcher_sin_colores_en_el_codigo(modulo: Path):
    texto = modulo.read_text(encoding="utf-8")
    sueltos = _COLOR.findall(texto)
    assert not sueltos, f"{modulo.name}: colores {sueltos}; usar tokens de app.css"


def test_cada_token_del_launcher_existe_en_los_dos_temas():
    claro, oscuro = theme.leer_tokens(theme.APP_CSS.read_text(encoding="utf-8"))
    for token in theme.TOKENS:
        assert token in claro, f"--{token} falta en el tema claro"
        assert token in oscuro, f"--{token} falta en el tema oscuro"


def test_paleta_lee_claro_y_oscuro_de_app_css():
    claro = theme.cargar_paleta(False)
    oscuro = theme.cargar_paleta(True)
    css = theme.APP_CSS.read_text(encoding="utf-8")
    acento_claro, acento_oscuro = re.findall(r"--acento:\s*(\S+);", css)
    assert claro.tk("acento") == acento_claro.lower()
    assert oscuro.tk("acento") == acento_oscuro.lower()
    assert not claro.oscuro and oscuro.oscuro


def test_paleta_sin_token_falla_claro(tmp_path):
    css = tmp_path / "app.css"
    css.write_text(
        "/* tokens:inicio */ :root { --fondo: #FFFFFF; } "
        "@media (prefers-color-scheme: dark) { :root { --fondo: #000000; } } "
        "/* tokens:fin */",
        encoding="utf-8",
    )
    with pytest.raises(theme.TemaNoDisponible, match="faltan tokens"):
        theme.cargar_paleta(False, css)


def test_fuera_de_windows_el_tema_es_claro(monkeypatch):
    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme.sistema_en_oscuro() is False
    theme.barra_de_titulo_oscura(object())  # no-op, no levanta


def test_elegir_familia_respeta_el_orden_y_las_mayusculas():
    disponibles = ["Arial", "segoe ui", "Consolas"]
    assert theme.elegir_familia(disponibles, theme.FUENTES_TEXTO) == "segoe ui"
    assert theme.elegir_familia(disponibles, theme.FUENTES_MONO) == "Consolas"
    assert theme.elegir_familia(["Arial"], theme.FUENTES_TEXTO) is None


# --- formas y marca ---------------------------------------------------------


def test_caja_con_anillo_de_foco_y_esquinas_redondeadas():
    paleta = theme.cargar_paleta(False)
    fondo, relleno, acento = (
        paleta.pil("fondo"),
        paleta.pil("superficie"),
        paleta.pil("acento"),
    )
    img = theme.caja(
        40, 20, fondo=fondo, relleno=relleno, radio=8, margen=3, anillo=acento
    )
    assert img.size == (46, 26)
    assert img.getpixel((23, 13)) == relleno  # centro
    assert _cerca(img.getpixel((23, 0)), acento)  # anillo, arriba
    # Separación de 1 px entre el anillo y la caja: más fondo que acento.
    sep = img.getpixel((23, 2))
    assert _distancia(sep, fondo) < _distancia(sep, acento)
    assert not _cerca(img.getpixel((3, 3)), relleno)  # esquina redondeada


def _distancia(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def _cerca(a: tuple[int, ...], b: tuple[int, ...], tolerancia: int = 8) -> bool:
    """Igual salvo el suavizado del borde (LANCZOS)."""
    return _distancia(a, b) <= tolerancia


def test_wordmark_usa_tinta_y_la_e_en_acento():
    paleta = theme.cargar_paleta(True)
    img = rasterizar(
        WORDMARK_SVG,
        72,
        fondo=paleta.pil("fondo"),
        color_actual=paleta.pil("tinta"),
        colores={"wordmark-e": paleta.pil("acento")},
    )
    assert img.height == 72
    assert img.width == round(72 * 585 / 79.2)
    # Asta de la "F" (x≈15 en el viewBox) y de la "E" (x≈550).
    escala = 72 / 79.2
    y = round((-30 + 78.4) * escala)
    assert img.getpixel((round((15 - 1.7) * escala), y)) == paleta.pil("tinta")
    assert img.getpixel((round((550 - 1.7) * escala), y)) == paleta.pil("acento")


# --- último ambiente usado --------------------------------------------------


def test_ultimo_ambiente_ida_y_vuelta(tmp_path):
    assert read_last_environment(tmp_path) is None
    path = save_last_environment(ArcaEnvironment.PROD, tmp_path)
    assert path == tmp_path / LAST_ENVIRONMENT_FILENAME
    assert read_last_environment(tmp_path) is ArcaEnvironment.PROD


@pytest.mark.parametrize(
    "contenido", ["no es json", "[]", '{"environment": 3}', '{"environment": "x"}']
)
def test_ultimo_ambiente_corrupto_es_none(tmp_path, contenido):
    (tmp_path / LAST_ENVIRONMENT_FILENAME).write_text(contenido, encoding="utf-8")
    assert read_last_environment(tmp_path) is None


def test_chooser_preselecciona_el_ultimo_usado(monkeypatch):
    from facturador.launcher import chooser, last_environment

    monkeypatch.setattr(
        last_environment, "read_last_environment", lambda: ArcaEnvironment.PROD
    )
    gui = chooser._default_gui(chooser.ENVIRONMENT_OPTIONS, None)
    assert gui.keywords == {  # type: ignore[attr-defined]
        "selected": ArcaEnvironment.PROD,
        "last_used": ArcaEnvironment.PROD,
    }


def test_chooser_sin_dato_preselecciona_homologacion(monkeypatch):
    from facturador.launcher import chooser, last_environment

    monkeypatch.setattr(last_environment, "read_last_environment", lambda: None)
    gui = chooser._default_gui(chooser.ENVIRONMENT_OPTIONS, None)
    assert gui.keywords["selected"] is ArcaEnvironment.HOMO  # type: ignore[attr-defined]


def test_chooser_en_cambio_preselecciona_el_otro_y_marca_el_actual():
    from facturador.launcher import chooser

    gui = chooser._default_gui(chooser.ENVIRONMENT_OPTIONS, ArcaEnvironment.HOMO)
    assert gui.keywords == {  # type: ignore[attr-defined]
        "selected": ArcaEnvironment.PROD,
        "current": ArcaEnvironment.HOMO,
    }


def test_cambio_de_ambiente_pasa_el_actual_al_chooser_por_defecto(monkeypatch):
    from facturador.launcher import __main__ as launcher_main

    llamadas: list[object] = []

    def _fake(**kwargs):
        llamadas.append(kwargs)
        return None

    monkeypatch.setattr(launcher_main, "choose_environment", _fake)
    launcher_main._choose_for_switch(_fake, ArcaEnvironment.PROD)
    assert llamadas == [{"current": ArcaEnvironment.PROD}]
    # Uno inyectado se llama sin argumentos.
    inyectado = launcher_main._choose_for_switch(
        lambda: ArcaEnvironment.HOMO, ArcaEnvironment.PROD
    )
    assert inyectado is ArcaEnvironment.HOMO


# --- flujo: recordar el ambiente y la ventana de error ----------------------


class _Supervisor:
    """Arranca bien salvo los ambientes en ``fallan`` (una vez cada uno)."""

    fallan: list[ArcaEnvironment] = []
    intentos: list[ArcaEnvironment] = []

    def __init__(self, *, environment, **_kwargs):
        self.environment = environment
        self.process = None
        self.is_running = False

    def start(self):
        type(self).intentos.append(self.environment)
        if self.environment in type(self).fallan:
            type(self).fallan.remove(self.environment)
            raise LauncherError("puerto ocupado")
        from facturador.launcher.command import plan_backend_launch
        from facturador.launcher.supervisor import LaunchResult

        return LaunchResult(
            plan=plan_backend_launch(self.environment, port=8399), reused=True
        )

    def stop(self):
        return None


@pytest.fixture
def supervisor():
    _Supervisor.fallan = []
    _Supervisor.intentos = []
    return _Supervisor


def test_recuerda_el_ambiente_solo_si_arranco(supervisor):
    from facturador.launcher.__main__ import main

    supervisor.fallan = [ArcaEnvironment.PROD]
    recordados: list[ArcaEnvironment] = []
    picks = iter([ArcaEnvironment.PROD, ArcaEnvironment.HOMO])
    code = main(
        [],
        choose=lambda: next(picks),
        supervisor_factory=supervisor,
        report_failure=lambda _msg: None,
        failure_prompt=lambda *_a, **_k: FailureAction.CHOOSE,
        remember_environment=recordados.append,
    )
    assert code == 0
    assert recordados == [ArcaEnvironment.HOMO]


def test_reintentar_vuelve_a_arrancar_el_mismo_ambiente(supervisor):
    from facturador.launcher.__main__ import main

    supervisor.fallan = [ArcaEnvironment.PROD]
    pedidos: list[dict[str, object]] = []

    def _prompt(message, **kwargs):
        pedidos.append({"message": message, **kwargs})
        return FailureAction.RETRY

    picks = iter([ArcaEnvironment.PROD])
    code = main(
        [],
        choose=lambda: next(picks),
        supervisor_factory=supervisor,
        report_failure=lambda _msg: None,
        failure_prompt=_prompt,
        remember_environment=lambda _env: None,
    )
    assert code == 0
    assert supervisor.intentos == [ArcaEnvironment.PROD, ArcaEnvironment.PROD]
    assert pedidos == [
        {
            "message": "puerto ocupado",
            "environment": ArcaEnvironment.PROD,
            "can_choose": True,
        }
    ]


def test_con_env_la_ventana_no_ofrece_volver_al_chooser(supervisor):
    from facturador.launcher.__main__ import main

    supervisor.fallan = [ArcaEnvironment.HOMO]
    pedidos: list[dict[str, object]] = []

    def _prompt(_message, **kwargs):
        pedidos.append(kwargs)
        return FailureAction.CLOSE

    code = main(
        ["--env", "homo"],
        choose=lambda: (_ for _ in ()).throw(AssertionError("no chooser")),
        supervisor_factory=supervisor,
        report_failure=lambda _msg: None,
        failure_prompt=_prompt,
        remember_environment=lambda _env: None,
    )
    assert code == 1
    assert pedidos == [{"environment": ArcaEnvironment.HOMO, "can_choose": False}]


def test_errores_sin_ambiente_pasan_por_la_ventana(supervisor):
    from facturador.launcher.__main__ import main

    pedidos: list[dict[str, object]] = []
    code = main(
        ["--restore", "--env", "homo"],
        report_failure=lambda _msg: None,
        failure_prompt=lambda message, **kwargs: pedidos.append(kwargs),
    )
    assert code == 2
    assert pedidos == [{"environment": None, "can_choose": False}]


def test_bajo_pytest_no_se_abre_la_ventana_de_error():
    assert prompt_failure("algo", environment=ArcaEnvironment.HOMO) is None


def test_detalle_tecnico_trae_mensaje_ambiente_y_version():
    detalle = technical_detail("puerto ocupado", ArcaEnvironment.PROD)
    assert "Error: puerto ocupado" in detalle
    assert "Ambiente: Producción" in detalle
    assert re.search(r"^FacturadorE \S+$", detalle.splitlines()[0])
    assert "Ambiente: sin elegir" in technical_detail("x", None)

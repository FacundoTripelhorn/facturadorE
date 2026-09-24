"""Guardia: el repo no menciona issues del tracker del proyecto.

Los IDs y links de issues en código, comentarios, tests o docs quedan viejos,
generan conflictos y hacen que los agentes preserven historia en lugar de
describir el comportamiento actual. El link al issue va en el PR y el commit.

Citar un issue de un proyecto de terceros como fuente (p.ej. en la base de
conocimiento de ARCA) sí está permitido: es documentación externa, no el
tracker de este repo.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
# Armado por partes para que este archivo no se detecte a sí mismo.
_PREFIX = "FA" + "C"
_REPO = "FacundoTripelhorn/facturadorE"
_ISSUE_REFS = (
    # Clave del issue en Linear.
    re.compile(rf"\b{_PREFIX}-\d+\b"),
    # Link a un issue o review de Linear (cualquier workspace).
    re.compile(r"linear\.app/[^/\s]+/(?:issue|review)/", re.IGNORECASE),
    # Issue o PR de este repo en GitHub, en cualquier forma: URL con http o
    # https, con o sin www, ruta relativa (/owner/repo/issues/N) o la forma
    # corta owner/repo#N.
    re.compile(re.escape(_REPO) + r"(?:/(?:issues|pull)/|#)\d+", re.IGNORECASE),
    # Número suelto de PR, issue o pregunta de grill: la palabra seguida de
    # numeral y número, con o sin espacio.
    re.compile(r"\b(?:PR|MR|pull request|issue|grill)s?\s?#\d+", re.IGNORECASE),
)
_SKIP = {"uv.lock"}


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git no disponible: no se puede listar el repo")
    return [_ROOT / name for name in out.splitlines() if Path(name).name not in _SKIP]


def _texto(path: Path) -> str | None:
    """Contenido del archivo si es texto; None si es binario o no existe.

    Se decide por contenido y no por extensión, para que ningún tipo de
    archivo de texto (``.js``, ``.css``, archivos sin extensión…) quede afuera.
    """
    try:
        datos = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    if b"\0" in datos:
        return None
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError:
        return None


def test_el_repo_no_menciona_ids_de_issues():
    hallazgos: list[str] = []
    for path in _tracked_files():
        texto = _texto(path)
        if texto is None:
            continue
        for nro, linea in enumerate(texto.splitlines(), start=1):
            if any(p.search(linea) for p in _ISSUE_REFS):
                rel = path.relative_to(_ROOT)
                hallazgos.append(f"{rel}:{nro}: {linea.strip()[:100]}")
    assert not hallazgos, (
        "Referencias a issues del proyecto en el repo (ver AGENTS.md, "
        "'Writing code, comments and docs'):\n" + "\n".join(hallazgos)
    )


@pytest.mark.parametrize(
    "linea",
    [
        "ver " + _PREFIX + "-123",
        "https://linear.app/ftripelhorn/" + "issue/" + _PREFIX.lower() + "-12/x",
        "https://github.com/" + _REPO + "/issues/7",
        "https://github.com/" + _REPO.lower() + "/pull/77",
        "http://www.github.com/" + _REPO + "/pull/3",
        "ver [/" + _REPO + "/issues/9](/" + _REPO + "/issues/9)",
        "cerrado en " + _REPO + "#12",
        "https://linear.app/ftripelhorn/" + "review/algo-9c35881d7580",
        "review del PR" + " #8",
        "ver pull request" + " #34",
        "(grill" + " #4b)",
        "Issue" + "#3",
    ],
)
def test_detecta_referencias_al_tracker_propio(linea):
    assert any(p.search(linea) for p in _ISSUE_REFS)


@pytest.mark.parametrize(
    "linea",
    [
        "[afip.php#95](https://github.com/AfipSDK/afip.php/issues/95)",
        "https://github.com/" + _REPO + "/blob/master/README.md",
        "git clone https://github.com/" + _REPO + ".git",
        "FACTURA-1 no es una clave de issue",
        "CMS/PKCS#7",
        "el clock skew es la causa #1 de errores",
    ],
)
def test_permite_fuentes_externas(linea):
    assert not any(p.search(linea) for p in _ISSUE_REFS)


def test_revisa_cualquier_archivo_de_texto_y_saltea_binarios(tmp_path):
    js = tmp_path / "app.js"
    js.write_text("// ver " + _PREFIX + "-5\n", encoding="utf-8")
    sin_extension = tmp_path / "Makefile"
    sin_extension.write_bytes(b"x\n")
    binario = tmp_path / "fuente.ttf"
    binario.write_bytes(b"\x00\x01" + (_PREFIX + "-5").encode())
    assert _texto(js) is not None
    assert _texto(sin_extension) == "x\n"
    assert _texto(binario) is None
    assert _texto(tmp_path / "no-existe.txt") is None

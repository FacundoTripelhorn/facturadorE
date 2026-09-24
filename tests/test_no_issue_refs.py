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
_REPO_URL = "https://github.com/FacundoTripelhorn/facturadorE"
_ISSUE_REFS = (
    # Clave del issue en Linear.
    re.compile(rf"\b{_PREFIX}-\d+\b"),
    # Link a un issue de Linear (cualquier workspace).
    re.compile(r"linear\.app/[^/\s]+/issue/", re.IGNORECASE),
    # Link a un issue o PR de este mismo repo en GitHub.
    re.compile(re.escape(_REPO_URL) + r"/(?:issues|pull)/\d+", re.IGNORECASE),
)
_TEXT_SUFFIXES = {
    ".py", ".md", ".mdc", ".html", ".sql", ".toml", ".yml", ".yaml",
    ".txt", ".xml", ".cfg", ".ini", ".json", ".sh", ".cmd", ".command", "",
}
_SKIP = {"uv.lock"}


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git no disponible: no se puede listar el repo")
    return [
        _ROOT / name
        for name in out.splitlines()
        if Path(name).name not in _SKIP and Path(name).suffix in _TEXT_SUFFIXES
    ]


def test_el_repo_no_menciona_ids_de_issues():
    hallazgos: list[str] = []
    for path in _tracked_files():
        try:
            texto = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
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
        _REPO_URL + "/issues/7",
        _REPO_URL.lower() + "/pull/77",
    ],
)
def test_detecta_referencias_al_tracker_propio(linea):
    assert any(p.search(linea) for p in _ISSUE_REFS)


@pytest.mark.parametrize(
    "linea",
    [
        "[afip.php#95](https://github.com/AfipSDK/afip.php/issues/95)",
        _REPO_URL + "/blob/master/README.md",
        "FACTURA-1 no es una clave de issue",
    ],
)
def test_permite_fuentes_externas(linea):
    assert not any(p.search(linea) for p in _ISSUE_REFS)

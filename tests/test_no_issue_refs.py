"""Guardia: el repo no menciona IDs del issue tracker.

Los IDs de issues en código, comentarios, tests o docs quedan viejos, generan
conflictos y hacen que los agentes preserven historia en lugar de describir el
comportamiento actual. El link al issue va en el PR y el commit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
# Armado por partes para que este archivo no se detecte a sí mismo.
_PREFIX = "FA" + "C"
_ISSUE_REF = re.compile(rf"\b{_PREFIX}-\d+\b")
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
            if _ISSUE_REF.search(linea):
                rel = path.relative_to(_ROOT)
                hallazgos.append(f"{rel}:{nro}: {linea.strip()[:100]}")
    assert not hallazgos, (
        "IDs de issues en el repo (ver AGENTS.md, 'Writing code, comments "
        "and docs'):\n" + "\n".join(hallazgos)
    )

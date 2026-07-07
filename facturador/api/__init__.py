"""Capa HTTP (contrato design.md §2.3): un router por recurso."""

from .app import create_app

__all__ = ["create_app"]

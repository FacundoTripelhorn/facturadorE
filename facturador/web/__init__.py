"""Frontend mínimo (spike.md §2.4): Jinja2 + HTMX, servido por la misma app.

Cero build tooling de JS: htmx.min.js va vendoreado en static/ (la app es
localhost y debe funcionar sin salida a internet más allá de ARCA).
"""

from .routes import STATIC_DIR, router

__all__ = ["STATIC_DIR", "router"]

"""Arranque local: uv run python -m facturador

Bind fijo a 127.0.0.1 (spike.md §2.5): la app no escucha para nadie más;
la única conexión de red es saliente hacia ARCA. El host NO es configurable
por diseño.
"""

import logging
import os

import uvicorn

from .api import create_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    port = int(os.environ.get("FACTURADOR_PORT", "8399"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()

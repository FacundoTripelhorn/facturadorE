"""Arranque local: uv run python -m facturador (o vía Docker, ver Dockerfile).

Bind fijo a 127.0.0.1 (design.md §2.5): la app no escucha para nadie más;
la única conexión de red es saliente hacia ARCA. La única excepción es
dentro del contenedor: ahí el proceso debe escuchar en 0.0.0.0 para que el
port-forward de Docker lo alcance, y la restricción a localhost la impone
el publish de docker-compose ("127.0.0.1:PORT:PORT"). FACTURADOR_IN_DOCKER
lo setea únicamente el Dockerfile; el host NO es configurable por diseño.
"""

import logging
import logging.handlers
import os

import uvicorn

from .api import create_app
from .config import Config, load_config

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging(config: Config) -> None:
    """Logs a archivo local con rotación (design.md §2.5), además de stderr.

    La redacción de credenciales (token/sign del TA, CMS firmado) es
    responsabilidad de cada módulo al loguear (checklist §2.1.1 punto 9);
    acá solo se decide el destino.
    """
    log_dir = config.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "facturador.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(), file_handler],
    )


def main() -> None:
    config = load_config()  # ya cargó <home>/.env (FACTURADOR_PORT incluido)
    _setup_logging(config)
    port = int(os.environ.get("FACTURADOR_PORT", "8399"))
    host = "0.0.0.0" if os.environ.get("FACTURADOR_IN_DOCKER") else "127.0.0.1"
    uvicorn.run(create_app(config), host=host, port=port)


if __name__ == "__main__":
    main()

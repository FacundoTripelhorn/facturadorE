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
from .config import Config, load_config, resolve_boot_profile
from .constants import DEFAULT_PORT

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging(config: Config) -> None:
    """Logs a archivo local con rotación (design.md §2.5), además de stderr.

    La redacción de credenciales (token/sign del TA, CMS firmado) es
    responsabilidad de cada módulo al loguear (checklist §2.1.1 punto 9);
    acá solo se decide el destino: el logs/ del perfil (FAC-25).
    """
    config.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.paths.log_file,
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
    # ADR 0001 / FAC-24: el ambiente se resuelve UNA vez acá y viaja
    # inyectado; ningún otro módulo vuelve a leer ARCA_ENV.
    profile = resolve_boot_profile()
    config = load_config(profile)
    _setup_logging(config)
    # El repr del perfil redacta la raíz física: identifica el ambiente sin
    # exponer paths internos ni secretos en el log de arranque.
    logging.getLogger(__name__).info(
        "Arranque en %s (%r)", profile.display_name, profile
    )
    port = int(os.environ.get("FACTURADOR_PORT", str(DEFAULT_PORT)))
    host = "0.0.0.0" if os.environ.get("FACTURADOR_IN_DOCKER") else "127.0.0.1"
    uvicorn.run(create_app(profile, config=config), host=host, port=port)


if __name__ == "__main__":
    main()

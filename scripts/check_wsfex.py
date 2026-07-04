"""Fase 2: FEXDummy + descarga de tablas de parámetros a arca_params.

Uso:  uv run python scripts/check_wsfex.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facturador import db, repo
from facturador.arca.wsfex import PARAM_METHODS, WsfexClient
from facturador.config import load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    config = load_config()
    client = WsfexClient(config)
    conn = db.connect(config.data_dir / "facturador.db")

    print(f"Ambiente: {config.env}  |  URL: {config.wsfex_url}")

    estado = client.dummy()
    print(f"FEXDummy: {estado}")
    if any(v.upper() != "OK" for v in estado.values()):
        print("ARCA reporta servidores fuera de línea; abortando.")
        sys.exit(1)

    for kind in PARAM_METHODS:
        registros = client.get_param(kind)
        n = repo.replace_params(conn, kind, [r.as_dict() for r in registros])
        muestra = ", ".join(f"{r.code}={r.description}" for r in registros[:3])
        print(f"{kind:<10} {n:>3} registros  [{muestra}{', ...' if n > 3 else ''}]")

    print("\nCache arca_params actualizado en", config.data_dir / "facturador.db")


if __name__ == "__main__":
    main()

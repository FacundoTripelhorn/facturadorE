"""Fase 1: obtiene (o reutiliza) un Ticket de Acceso del WSAA.

Uso:  uv run python scripts/get_ta.py

Imprime ambiente, vigencia y origen (cache/nuevo). Nunca imprime token/sign.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facturador.config import load_config
from facturador.wsaa import SERVICE, WsaaClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    config = load_config()
    client = WsaaClient(config)

    cached = client.cache.load(config.env)
    ticket = client.get_ticket()
    origen = "cache" if cached is not None else "WSAA (nuevo)"

    print()
    print(f"Ambiente     : {config.env}")
    print(f"URL WSAA     : {config.wsaa_url}")
    print(f"Servicio     : {SERVICE}")
    print(f"Origen       : {origen}")
    print(f"Generado     : {ticket.generation.isoformat()}")
    print(f"Expira       : {ticket.expiration.isoformat()}")
    print("Token / Sign : [redactado]")


if __name__ == "__main__":
    main()

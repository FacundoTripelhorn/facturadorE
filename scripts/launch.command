#!/bin/sh
# Launcher de doble click para macOS (design.md §2.5): levanta el contenedor
# si el servidor no responde y abre el browser. Darle permiso de ejecución
# una vez (chmod +x) y anclarlo al Dock si se quiere.
set -eu
cd "$(dirname "$0")/.."

PORT="${FACTURADOR_PORT:-8399}"
url="http://localhost:$PORT"

vivo() { curl -fsS -o /dev/null "$url/health" 2>/dev/null; }

if ! vivo; then
    echo "Levantando facturador (docker compose up -d)..."
    docker compose up -d --build
    intentos=0
    until vivo; do
        intentos=$((intentos + 1))
        if [ "$intentos" -ge 60 ]; then
            echo "ERROR: el servidor no respondió en 60 s." >&2
            echo "Revisar: docker compose logs facturador" >&2
            exit 1
        fi
        sleep 1
    done
fi

open "$url/"

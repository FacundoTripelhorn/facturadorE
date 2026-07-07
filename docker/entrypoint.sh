#!/bin/sh
# Normaliza el bind mount del host (montado en /host) al layout que espera la
# app en FACTURADOR_HOME (design.md §2.5).
#
# Por qué no se usa /host directo como FACTURADOR_HOME: los bind mounts de
# Docker Desktop no tienen semántica POSIX confiable (Windows expone todo
# como 0777 y no acepta chmod), así que el chequeo de permisos de la clave
# privada fallaría siempre. Los secretos se COPIAN a un directorio interno
# del contenedor con chmod 400 (ahí el chequeo aplica de verdad y la copia
# muere con el contenedor); data/, backups/ y .env quedan como symlinks al
# mount para que todo persista en el host. Si el host viene vacío (primer
# arranque), la app crea la estructura y el .env de bootstrap a través de
# esos symlinks.
set -eu

HOME_DIR="${FACTURADOR_HOME:-/facturador}"

mkdir -p /host/secrets /host/data /host/backups "$HOME_DIR/secrets"
if [ -n "$(ls -A /host/secrets 2>/dev/null)" ]; then
    cp /host/secrets/* "$HOME_DIR/secrets/"
    chmod 400 "$HOME_DIR/secrets/"*
fi
ln -sfn /host/data "$HOME_DIR/data"
ln -sfn /host/backups "$HOME_DIR/backups"
# La app lee el .env SOLO de <home>/.env; el symlink apunta al del host (y
# si no existe, la app lo crea con el bootstrap a través del symlink).
# FACTURADOR_HOME y FACTURADOR_PORT ya están en el entorno del contenedor y
# python-dotenv NO pisa variables existentes: un valor en el .env del host
# no puede mover el layout ni el puerto interno.
ln -sfn /host/.env "$HOME_DIR/.env"

exec python -m facturador

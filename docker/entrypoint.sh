#!/bin/sh
# Normaliza el bind mount del host (montado en /host) al layout de perfiles
# aislados que espera la app (ADR 0001).
#
# Por qué no se apuntan los perfiles directo a /host: los bind mounts de
# Docker Desktop no tienen semántica POSIX confiable (Windows expone todo
# como 0777 y no acepta chmod), así que el chequeo de permisos de la clave
# privada fallaría siempre. Los secretos del perfil se COPIAN a un
# directorio interno del contenedor con chmod 400 (ahí el chequeo aplica de
# verdad y la copia muere con el contenedor); data/ y backups/ del perfil
# quedan como symlinks al mount para que todo persista en el host, bajo
# /host/profiles/<env>/. El .env de bootstrap sigue en <FACTURADOR_HOME>/.env
# (symlink a /host/.env). Si el host viene vacío (primer arranque), la app
# crea la estructura y el .env de bootstrap a través de esos symlinks.
set -eu

HOME_DIR="${FACTURADOR_HOME:-/facturador}"

mkdir -p /host "$HOME_DIR"
# La app lee el .env SOLO de <home>/.env; el symlink apunta al del host (y
# si no existe, la app lo crea con el bootstrap a través del symlink).
# FACTURADOR_HOME y FACTURADOR_PORT ya están en el entorno del contenedor y
# python-dotenv NO pisa variables existentes: un valor en el .env del host
# no puede mover el layout ni el puerto interno.
ln -sfn /host/.env "$HOME_DIR/.env"

# El ambiente decide QUÉ perfil se normaliza. Misma precedencia que la app:
# ARCA_ENV del entorno gana; si no, el del .env del host. Si no hay ambiente
# elegido, no se arma ningún perfil y la app falla con su mensaje claro.
ENV_NAME="${ARCA_ENV:-}"
if [ -z "$ENV_NAME" ] && [ -f /host/.env ]; then
    ENV_NAME="$(sed -n 's/^ARCA_ENV=[[:space:]]*//p' /host/.env | tail -n 1)"
fi

if [ -n "$ENV_NAME" ]; then
    PROFILE_ROOT="$XDG_DATA_HOME/facturadorE/$ENV_NAME"
    HOST_PROFILE="/host/profiles/$ENV_NAME"
    mkdir -p "$HOST_PROFILE/secrets" "$HOST_PROFILE/data" "$HOST_PROFILE/backups" \
             "$PROFILE_ROOT/secrets"
    # La copia interna es un espejo del mount, no un cache: en un restart
    # del contenedor podría sobrevivir una copia de secretos que ya no están
    # en el host, y la app arrancaría contra ARCA con certificados viejos.
    rm -f "$PROFILE_ROOT/secrets/"*
    if [ -n "$(ls -A "$HOST_PROFILE/secrets" 2>/dev/null)" ]; then
        cp "$HOST_PROFILE/secrets/"* "$PROFILE_ROOT/secrets/"
        chmod 400 "$PROFILE_ROOT/secrets/"*
    fi
    ln -sfn "$HOST_PROFILE/data" "$PROFILE_ROOT/data"
    ln -sfn "$HOST_PROFILE/backups" "$PROFILE_ROOT/backups"
fi

exec python -m facturador

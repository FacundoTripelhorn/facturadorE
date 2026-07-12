# Runtime único para Windows y macOS (design.md §2.5): la misma imagen corre
# en ambos hosts vía Docker Desktop; los permisos POSIX de la clave privada y
# el chequeo de arranque se resuelven una sola vez acá adentro (Linux).
FROM python:3.12-slim

# Libs nativas de weasyprint (Pango/HarfBuzz) + fuentes para el PDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz-subset0 \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY facturador/ facturador/
RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# El proceso corre como root DENTRO del contenedor (la VM de Docker Desktop,
# no el host): los bind mounts de Windows llegan con modo 0777 fijo y los de
# macOS con el uid del host, así que un usuario propio no podría ni leer la
# key ni pasar el chequeo de permisos. El entrypoint copia los secretos a un
# directorio interno con chmod 400, que es donde el chequeo sí aplica.
# FACTURADOR_PORT queda fijado acá para que un valor en el .env montado no
# pueda moverlo (python-dotenv no pisa variables ya presentes): el publish
# de compose y el HEALTHCHECK asumen 8399 adentro. El puerto del lado del
# host sí es configurable, vía FACTURADOR_PORT en el shell del host; compose
# lo propaga como FACTURADOR_PUBLIC_PORT para la allowlist Host/Origin
# (FAC-41) cuando el mapping no es 8399:8399.
# XDG_DATA_HOME fija dónde resuelve la app las raíces de perfil (ADR 0001 /
# FAC-25) para que el entrypoint y la app coincidan sin depender del $HOME
# del usuario del contenedor.
ENV FACTURADOR_HOME=/facturador \
    FACTURADOR_IN_DOCKER=1 \
    FACTURADOR_PORT=8399 \
    XDG_DATA_HOME=/appdata \
    PYTHONUNBUFFERED=1

EXPOSE 8399

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8399/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]

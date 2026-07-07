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
ENV FACTURADOR_HOME=/facturador \
    FACTURADOR_IN_DOCKER=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8399

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8399/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]

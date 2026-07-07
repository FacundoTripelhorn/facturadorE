# facturadorE

Emisión de **Factura E de exportación** (servicios) contra ARCA (ex AFIP)
desde una app local propia: API FastAPI + frontend Jinja/HTMX, con
autorización vía WSAA/WSFEX, PDF con QR (RG 4892) y la app como único
registro de comprobantes. Reemplaza el flujo manual de Comprobantes en
Línea por: abrir la app → monto + fecha de pago → CAE + PDF.

El diseño completo (dominio ARCA, arquitectura, decisiones y su porqué)
vive en [docs/design.md](docs/design.md).

## Cómo corre

La app corre **solo en localhost**, en la máquina del usuario, dentro de
Docker (misma imagen para Windows y macOS). No hay servidor público: la
única conexión de red es saliente hacia ARCA.

1. Crear el directorio de datos (default `~/facturador`) con este layout:

   ```
   ~/facturador/
     .env                  # copiar .env.example y completar
     secrets/
       homo.crt homo.key   # certificado de homologación (WSASS)
       prod.crt prod.key   # certificado de producción (cuando toque)
     data/                 # DB, PDFs y logs (los crea la app)
     backups/
   ```

2. Levantar: doble click en `scripts/launch.cmd` (Windows) o
   `scripts/launch.command` (macOS) — levanta el contenedor si hace falta y
   abre `http://localhost:8399`. Equivalente manual: `docker compose up -d`.

Si `FACTURADOR_HOME` no es `~/facturador`, definirlo en el entorno antes de
levantar compose.

## Backups

El estado completo (DB + PDFs + secretos + `.env`) se respalda cifrado del
lado del cliente con [age](https://age-encryption.org) y, opcionalmente, se
sube a un bucket S3 privado. La passphrase es del usuario y no vive en
ningún lado. Correr en el host después de emitir:

```sh
uv run python -m facturador.backup            # cifra a backups/ y sube si hay bucket
uv run python -m facturador.restore --latest  # máquina secundaria: baja y restaura
```

Requisitos en el host: `age` y (para S3) `aws` CLI. Nunca correr dos copias
emitiendo en paralelo: el chequeo de DB desactualizada contra ARCA bloquea
la emisión si el registro local quedó viejo, pero el orden primaria→backup→
restore→secundaria es responsabilidad del usuario.

## Desarrollo

```sh
uv sync
uv run pytest          # la suite no necesita red ni certificados
uv run ruff check .
uv run mypy
uv run python -m facturador   # servidor local sin Docker (sin PDF en Windows*)
```

\* El render de PDF (weasyprint) necesita Pango/GTK, garantizado solo dentro
de la imagen Docker; el resto de la app funciona sin eso.

Los scripts de `scripts/{get_ta,check_wsfex,authorize_homo}.py` son las
fases históricas del spike (WSAA → conectividad WSFEX → primer CAE) y
siguen sirviendo como smoke tests manuales contra homologación.

## Estado

- Fases 1–6 completas: CAEs reales emitidos en homologación (2026-07-03),
  flujo de dos pasos con revisión, PDF con QR, empaquetado Docker + backups.
- Pendiente fase 7 (producción): certificado prod, alta del servicio
  "Facturación Electrónica de Exportación", punto de venta RECE exclusivo,
  `ARCA_ENV=prod` y primera factura real chica (checklist en
  [docs/design.md §4](docs/design.md)).

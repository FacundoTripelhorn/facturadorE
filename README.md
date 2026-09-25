# facturador

Aplicación local para emitir **Factura E (exportación)** contra **ARCA** (ex AFIP)
desde tu propia computadora, como Responsable Inscripto. Reemplaza el flujo manual
de Comprobantes en Línea: el día del cobro abrís la app, ingresás el monto,
confirmás, y obtenés el CAE con el PDF descargable. La app además **es el
registro**: todos los comprobantes quedan persistidos con su estado, CAE y PDF.

Qué incluye:

- **Cliente ARCA propio**: autenticación WSAA (firma CMS, cache del ticket de
  acceso) y WSFEXv1 (autorización, numeración, tablas de parámetros, cotización).
- **API JSON** (FastAPI) con emisión idempotente, máquina de estados y
  reconciliación ante timeouts.
- **Frontend web** (Jinja2 + HTMX) servido por la misma app: form precargado con
  el cliente habitual, revisión antes de enviar, listado y detalle read-only.
- **PDF del comprobante** con QR según RG 4892 (fpdf2, Python puro, sin browser).
- **SQLite** como única fuente de verdad local; sin servicios externos.
- **Empaquetado Docker** (misma imagen para Windows y macOS) con el puerto
  publicado solo en `127.0.0.1`, y launchers de doble click.
- **Seed cifrado** del lado del cliente ([age](https://age-encryption.org)):
  solo la config que ARCA no puede reproducir; DB/PDFs se regeneran.

> El diseño completo, las decisiones tomadas y el mapeo campo por campo contra
> WSFEX están documentados en [`docs/design.md`](docs/design.md).

## Requisitos

- **Docker Desktop** (runtime recomendado), o bien **Python ≥ 3.12** +
  [uv](https://docs.astral.sh/uv/) para correr sin Docker. El PDF no necesita
  nada más que `uv sync`: se genera con fpdf2 en Python puro.
- **Certificado ARCA** autorizado al servicio `wsfex` (ver más abajo).
- **Reloj sincronizado** (NTP): el WSAA rechaza pedidos con clock skew. macOS y
  la mayoría de las distros Linux lo traen activo por defecto.
- Para backups: **[age](https://age-encryption.org)** (`winget install
  FiloSottile.age` / `brew install age`) y, si se sube a S3, **aws CLI**.

## Ambientes: una sola app

**FacturadorE** es una sola aplicación con dos ambientes de negocio:
**Homologación** (prueba, sin efecto fiscal) y **Producción** (comprobantes
reales). Al abrirla, el launcher te deja elegir cuál usar. Cada ambiente
tiene su propio estado aislado (base, certificados, PDFs, caches, backups);
no hace falta instalar dos copias ni administrar dos directorios a mano.

Cambiar de ambiente **reinicia** el backend con el otro perfil: no hay cambio
en caliente dentro del mismo proceso. El ambiente activo se ve siempre en la
app (badge). Contrato completo:
[`docs/adr/0001-perfiles-de-ambiente-aislados.md`](docs/adr/0001-perfiles-de-ambiente-aislados.md).

Guías paso a paso (trámites en ARCA, certificados y verificación):

- **[Homologación](docs/setup-homologacion.md)** — empezar acá: certificado vía
  WSASS, autorización al servicio `wsfex`, smoke tests y primera emisión de
  prueba.
- **[Producción](docs/setup-produccion.md)** — certificado productivo,
  asociación a "Facturación Electrónica de Exportación", punto de venta RECE,
  smoke test y primera factura real, más cuidados operativos.

## Uso con Docker (recomendado)

El directorio de datos del host (`FACTURADOR_HOME`, default `~/facturador`)
vive fuera del repo y del contenedor. **La app crea sola** el bootstrap y el
layout interno por ambiente; lo único que se coloca a mano es el par
certificado/clave del ambiente que vas a usar (`cert.crt` / `cert.key`, key en
modo `400`).

Un contenedor corre **un solo ambiente** (el elegido al arrancar). Homologación
y Producción no comparten base ni certificados. Los scripts de doble click
**no** muestran el chooser del launcher nativo: hay que fijar el ambiente en
`<FACTURADOR_HOME>/.env` **antes** del primer `compose up` (compose no pasa
`ARCA_ENV` del shell del host al contenedor; el entrypoint solo lee ese
archivo montado):

```dotenv
# ~/facturador/.env  (obligatorio para Docker)
ARCA_ENV=homo
```

Sin `ARCA_ENV` en ese archivo el contenedor no arranca. Para pasar a
Producción: parar el contenedor, poner `ARCA_ENV=prod` (y el par `cert.*`
bajo `profiles/prod/secrets/`), y volver a levantar — un reinicio, no un
cambio en caliente.

Levantar: doble click en `scripts/launch.cmd` (Windows) o
`scripts/launch.command` (macOS) — levanta el contenedor si hace falta y abre
`http://localhost:8399`. Equivalente manual: `docker compose up -d`. Si
`FACTURADOR_HOME` no es `~/facturador`, exportarlo en el **shell del host**
antes de levantar compose (compose interpola esa variable; no se lee del
`.env` montado). El puerto publicado en el host se cambia igual:
`export FACTURADOR_PORT=8400` antes de `compose up` / `launch.*` — no pongas
`FACTURADOR_PORT` en `~/facturador/.env` (adentro del contenedor el puerto
queda fijo en 8399). Compose propaga ese valor como `FACTURADOR_PUBLIC_PORT`
para que la política Host/Origin acepte `http://localhost:8400`.

El puerto se publica **solo en `127.0.0.1`**: la app no es accesible desde la
red. Dentro del contenedor, el entrypoint copia los secretos a un directorio
interno con `chmod 400` (los bind mounts de Docker Desktop no tienen semántica
POSIX confiable) y deja datos/backups persistiendo en el host bajo
`profiles/<env>/`.

## Uso sin Docker

```bash
git clone <url-del-repo> facturador && cd facturador
uv sync
uv run python -m facturador.launcher   # elige Homologación o Producción
```

El launcher nativo arranca el backend en `http://127.0.0.1:8399` y abre una
**ventana dedicada** (pywebview: WebView2 en Windows, WebKit en macOS) con el
título `FacturadorE — Homologación` o `FacturadorE — Producción`. Cerrar esa
ventana detiene el backend. Si el webview no está disponible, cae al navegador
del sistema. Para automatización / agents: `--no-browser`.

El PDF se ve y se descarga sin salir de la ventana: "Ver PDF" lo abre en un
modal con el visor del webview y "Descargar" abre el diálogo de guardado del
sistema. Si el webview no tiene visor de PDF embebido, el modal ofrece solo
la descarga.

La app escucha **solo en localhost** por diseño (el host no es configurable):
la única conexión de red es saliente hacia ARCA.

**Requisitos del webview**

- **Windows:** [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)
  (suele venir con Windows 11 / Edge; en máquinas mínimas hay que instalarlo).
- **macOS:** WebKit vía el runtime de pywebview (no hace falta un browser aparte).
- **Linux (dev):** pywebview usa GTK/Qt según lo disponible; el fallback al
  browser cubre entornos headless o sin toolkit.

Para desarrollo o scripts sin el chooser:
`uv run python -m facturador.launcher --env homo` (o `prod`), o
`ARCA_ENV=homo uv run python -m facturador` (un ambiente explícito por proceso).

### Configuración

La app es dueña de su configuración: casi todo se edita desde la página
**Configuración** de la UI y se guarda en la DB del ambiente activo (datos del
emisor que van al PDF, punto de venta, bucket S3 de backups), así viaja
dentro del seed cifrado.

El ambiente lo elige el **launcher nativo** (Homologación / Producción) o, en
Docker, `ARCA_ENV` en `<FACTURADOR_HOME>/.env` al arrancar el contenedor.
Cada proceso backend arranca con exactamente un ambiente inmutable; las URLs
de ARCA y el par certificado/clave salen de ese perfil. El CUIT emisor se
extrae del certificado. Sin el par `cert.crt` / `cert.key` (key en
`400`/`600`), la app se niega a arrancar y muestra dónde colocarlos. Sin
datos de emisor, la UI dirige a Configuración antes de permitir emitir.

La clave privada va **sin passphrase**: la protegen los permisos `400`, el
perfil local y el cifrado del backup al salir de la máquina.

### Flujo en la interfaz web

1. **`/configuracion`** — primera vez: completar los datos del emisor (se
   imprimen en el PDF), punto de venta y, opcionalmente, el bucket de backups.
2. **`/`** — form de nueva factura, precargado con el cliente default. El caso
   habitual se reduce a monto + fecha de pago + descripción; la cotización de la
   moneda la trae ARCA automáticamente para la fecha.
3. **Revisar** — página que muestra exactamente qué se va a enviar a ARCA. Nada
   viaja sin pasar por acá.
4. **Confirmar** — ejecuta la autorización (WSFEX `FEXAuthorize`) y redirige al
   detalle con el CAE, su vencimiento y los botones para ver (en un modal) y
   descargar el PDF.
5. **`/comprobantes`** — listado con tabs por estado (borradores, autorizadas,
   que requieren atención) y **`/clientes`** para el ABM de clientes.

Estados posibles de una factura: `draft` → `submitting` → `authorized` /
`rejected` / `unknown` (timeout post-envío; se reconcilia automáticamente
contra ARCA con `FEXGetCMP` al volver a consultarla).

### API JSON

La misma app expone la API (documentación interactiva en `/docs`):

| Método y ruta | Descripción |
|---|---|
| `POST /invoices` | Crea un borrador (valida dominio) |
| `POST /invoices/{id}/authorize` | Autoriza contra ARCA (idempotente; reintentos reutilizan el mismo Id) |
| `GET /invoices/{id}` | Estado + CAE (reconcilia `unknown` al consultar) |
| `GET /invoices/{id}/pdf` | PDF con QR RG 4892 (solo facturas autorizadas) |
| `GET /invoices` | Listado paginado (`limit`, `offset`) |
| `GET /clients` / `POST /clients` / `PUT /clients/{id}` | ABM de clientes |
| `GET /params/{kind}` | Tablas de parámetros de ARCA cacheadas: `moneda`, `pais`, `cuit_pais`, `cbte_tipo`, `umed`, `incoterms`, `idioma`, `tipo_expo` |
| `GET /params/currency/{id}/rate?date=AAAAMMDD` | Cotización de ARCA para esa fecha |
| `GET /health` | Liveness local (sin tocar ARCA; la usan Docker y los launchers) |
| `GET /health/arca` | Estado de los servidores de ARCA (`FEXDummy`) |

### Scripts de diagnóstico

Útiles para verificar la conectividad con ARCA paso a paso (requieren
`ARCA_ENV` explícito). Con el launcher nativo alcanza eso; si los datos están
en el layout Docker, sumá `FACTURADOR_APP_DATA=~/facturador/profiles`:

```bash
ARCA_ENV=homo uv run python scripts/get_ta.py         # ticket WSAA
ARCA_ENV=homo uv run python scripts/check_wsfex.py    # FEXDummy + params
ARCA_ENV=homo uv run python scripts/authorize_homo.py # emisión de prueba
```

## Backups (seed)

**ARCA es el ledger autoritativo.** La DB local y los PDFs son regenerables;
el backup que sale de la máquina es un **seed** de configuración (emisores,
CUIT, ambiente, PV/tipos, cliente default / UI, versión de esquema +
manifiesto), cifrado con [age](https://age-encryption.org) a las claves
públicas listadas en `backups/recipients.txt` del perfil (una por máquina).
No incluye DB, certificados, claves privadas ni datos por comprobante.

```bash
# Una clave pública age por línea (comentar con #). Ver el runbook de provisioning por máquina para el
# aprovisionamiento por máquina.
echo "age1..." >> "$(perfil)/backups/recipients.txt"

# Launcher nativo: --env resuelve el perfil en el app-data del SO
uv run python -m facturador.backup --env homo

# Docker: apuntar a la raíz montada en el host
uv run python -m facturador.backup --root ~/facturador/profiles/homo
```

El CLI escribe `backups/seed.age` (nombre fijo, overwrite) y **no sube nada a
S3**: es solo local. El upload lo hace la app: se dispara solo después de cada
cambio de configuración del perfil (y se reintenta al arrancar si quedó
pendiente), o a pedido con `POST /backup/seed`. Correr el CLI sobre un perfil
sin cambios no actualiza el seed del bucket. La clave lógica en el bucket del
usuario es `{prefix}/{cuit}/{env}/seed.age` (versioning del bucket como red de
seguridad). El registro de comprobantes se reconstruye desde ARCA, no desde S3.

Nunca emitir desde dos máquinas en paralelo: el chequeo de registro desactualizado bloquea si el
registro local quedó detrás de ARCA.

## Desarrollo

```bash
uv sync                      # instala también el grupo dev
uv run pytest                # tests (usan un ARCA falso; no requieren red ni certificado)
uv run ruff check .          # lint
uv run mypy                  # type check
```

Estructura del código:

```
facturador/
  arca/         # clientes WSAA (auth CMS) y WSFEX (SOAP)
  api/          # routers FastAPI de la API JSON
  web/          # frontend Jinja2 + HTMX
  pdf/          # render del comprobante + QR RG 4892
  repo/         # acceso a datos (SQLite)
  launcher/     # chooser Homologación/Producción + supervisor de proceso
  profile.py    # perfiles aislados (paths de runtime por ambiente)
  service.py    # lógica de dominio: numeración, idempotencia, estados
  config.py     # arranque: ambiente inyectado, certificados del perfil
  settings.py   # configuración de dominio (vive en la DB, se edita en la UI)
  backup.py     # CLI del seed cifrado; seed_backup.py arma/cifra
  s3_seed.py    # adapter S3 del seed (seed.age + recipients.txt)
  restore.py    # seed import + rebuild desde ARCA
  reconstruct.py # loop FEXGetCMP full/catch-up
  seed_import.py # identidad cert→env→CUIT→schema→integrity
  schema.sql    # baseline del esquema (migración v1)
  migrations/   # runner versionado + schema_migrations
docker/         # entrypoint del contenedor (ver Dockerfile y docker-compose.yml)
scripts/        # diagnóstico, flujo de homologación y launchers Docker
tests/          # pytest (incluye ARCA falso en tests/arca_fake.py)
docs/
  design.md             # diseño, decisiones y contexto de dominio
  adr/                  # decisiones de arquitectura (perfiles de ambiente)
  setup-homologacion.md # guía de setup del ambiente de prueba
  setup-produccion.md   # guía de pasaje a producción
```

## Seguridad

- La clave privada equivale a la firma fiscal: **nunca** va al repo (el
  `.gitignore` excluye `secrets/`, `*.key`, `*.crt`, `.env`) y la app exige
  permisos `400`/`600` sobre ella.
- El token/sign del WSAA y el CMS firmado nunca se loguean.
- Homologación y Producción no se cruzan: cada proceso backend usa un solo
  ambiente inmutable; URLs y certificados salen del mismo perfil.
- El seed se cifra del lado del cliente con `age` (recipients por máquina)
  **antes** de salir; DB, PDFs y la clave fiscal **no** viajan en el bundle.

## Licencia

[MIT](LICENSE).

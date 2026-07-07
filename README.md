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
- **PDF del comprobante** con QR según RG 4892 (WeasyPrint).
- **SQLite** como única fuente de verdad local; sin servicios externos.

> El diseño completo, las decisiones tomadas y el mapeo campo por campo contra
> WSFEX están documentados en [`docs/spike.md`](docs/spike.md).

## Requisitos

- **Python ≥ 3.12** y [uv](https://docs.astral.sh/uv/) (o `pip` si preferís).
- **Dependencias de sistema de WeasyPrint** (Pango, Cairo, GDK-PixBuf). En
  Debian/Ubuntu: `sudo apt install libpango-1.0-0 libpangocairo-1.0-0
  libgdk-pixbuf-2.0-0`. Ver la [guía de instalación de WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html).
- **Certificado ARCA** autorizado al servicio `wsfex` (ver más abajo).
- **Reloj sincronizado** (NTP): el WSAA rechaza pedidos con clock skew. macOS y
  la mayoría de las distros Linux lo traen activo por defecto.

## Setup por ambiente

Cada ambiente tiene su guía paso a paso (trámites en ARCA, certificados,
configuración local y verificación):

- **[Homologación](docs/setup-homologacion.md)** — ambiente de prueba, sin
  efectos fiscales. Por acá se empieza: certificado vía WSASS, autorización al
  servicio `wsfex`, verificación de conectividad con los scripts y primera
  emisión de prueba.
- **[Producción](docs/setup-produccion.md)** — certificado productivo,
  asociación al servicio "Facturación Electrónica de Exportación", punto de
  venta RECE exclusivo de exportación, smoke test y primera factura real, más
  los cuidados operativos (backups, numeración, multi-máquina).

## Instalación y configuración

```bash
git clone <url-del-repo> facturador && cd facturador
uv sync
```

La app lee toda su configuración de variables de entorno (y de un `.env` si
existe). El `.env` se carga del **directorio de trabajo** desde donde se
ejecuta la app — típicamente la raíz del repo, donde ya está en el
`.gitignore` —, no de `FACTURADOR_HOME`; alternativamente se pueden exportar
las mismas variables en el shell. Layout de datos esperado bajo
`FACTURADOR_HOME` (por defecto, el directorio actual):

```
$FACTURADOR_HOME/
  secrets/            # chmod 700
    homo.key          # chmod 400 — la app se niega a arrancar con permisos laxos
    homo.crt
    prod.key          # solo al pasar a producción
    prod.crt
  data/               # la crea la app
    facturador.db     # SQLite
    pdfs/             # comprobantes emitidos
```

Variables disponibles (ejemplo de `.env`):

```dotenv
# Ambiente: "homo" o "prod". De este ÚNICO flag se derivan las URLs de
# WSAA/WSFEX y los paths de certificado (secrets/<env>.key / <env>.crt);
# es imposible por construcción mezclar cert de homologación con producción.
ARCA_ENV=homo

# Raíz de datos (default: directorio actual)
FACTURADOR_HOME=~/facturador

# CUIT emisor, 11 dígitos sin guiones. Opcional: si falta, se extrae del
# certificado en runtime.
ARCA_CUIT=20123456789

# Punto de venta (default 1; en homo es libre, en prod el PV RECE exclusivo)
ARCA_PUNTO_VTA=1

# Passphrase de la clave privada, solo si la key está protegida
# ARCA_KEY_PASSPHRASE=...

# Datos del emisor que van al PDF (no viajan a ARCA)
EMISOR_RAZON_SOCIAL=Mi Empresa S.A.
EMISOR_DOMICILIO=Calle Falsa 123, CABA
EMISOR_IIBB=            # vacío => se imprime el CUIT
EMISOR_INICIO_ACTIVIDADES=01/2020

# Puerto local (default 8399)
# FACTURADOR_PORT=8399
```

En el arranque la app valida que existan `secrets/<env>.crt` y
`secrets/<env>.key` y que la key tenga permisos `400`/`600`; si no, se niega a
arrancar con un mensaje explicativo.

## Uso

```bash
uv run python -m facturador
```

Abre `http://127.0.0.1:8399`. La app escucha **solo en localhost** por diseño
(el host no es configurable): la única conexión de red es saliente hacia ARCA.

### Flujo en la interfaz web

1. **`/`** — form de nueva factura, precargado con el cliente default. El caso
   habitual se reduce a monto + fecha de pago + descripción; la cotización de la
   moneda la trae ARCA automáticamente para la fecha.
2. **Revisar** — página que muestra exactamente qué se va a enviar a ARCA. Nada
   viaja sin pasar por acá.
3. **Confirmar** — ejecuta la autorización (WSFEX `FEXAuthorize`) y redirige al
   detalle con el CAE, su vencimiento y el botón de descarga del PDF.
4. **`/comprobantes`** — listado con tabs por estado (borradores, autorizadas,
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
| `GET /health/arca` | Estado de los servidores de ARCA (`FEXDummy`) |

### Scripts de diagnóstico

Útiles para verificar la conectividad con ARCA paso a paso:

```bash
uv run python scripts/get_ta.py         # obtiene (o reutiliza) el ticket WSAA
uv run python scripts/check_wsfex.py    # FEXDummy + descarga de tablas de parámetros
uv run python scripts/authorize_homo.py # flujo completo de emisión en homologación
```

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
  service.py    # lógica de dominio: numeración, idempotencia, estados
  config.py     # carga y validación de configuración
  schema.sql    # esquema de la base
scripts/        # diagnóstico y flujo de homologación
tests/          # pytest (incluye ARCA falso en tests/arca_fake.py)
docs/
  spike.md              # diseño, decisiones y contexto de dominio
  setup-homologacion.md # guía de setup del ambiente de prueba
  setup-produccion.md   # guía de pasaje a producción
```

## Seguridad

- La clave privada equivale a la firma fiscal: **nunca** va al repo (el
  `.gitignore` excluye `secrets/`, `*.key`, `*.crt`, `.env`) y la app exige
  permisos `400`/`600` sobre ella.
- El token/sign del WSAA y el CMS firmado nunca se loguean.
- Los certificados de homologación y producción no pueden cruzarse: URLs y
  paths se derivan del único flag `ARCA_ENV`.

## Licencia

[MIT](LICENSE).

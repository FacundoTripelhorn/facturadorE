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
- **Empaquetado Docker** (misma imagen para Windows y macOS) con el puerto
  publicado solo en `127.0.0.1`, y launchers de doble click.
- **Backups cifrados** del lado del cliente ([age](https://age-encryption.org))
  con upload opcional a S3.

> El diseño completo, las decisiones tomadas y el mapeo campo por campo contra
> WSFEX están documentados en [`docs/design.md`](docs/design.md).

## Requisitos

- **Docker Desktop** (runtime recomendado; trae todo lo demás), o bien
  **Python ≥ 3.12** + [uv](https://docs.astral.sh/uv/) para correr sin Docker —
  en ese caso el render de PDF necesita además las **dependencias de sistema de
  WeasyPrint** (Pango/HarfBuzz; en Debian/Ubuntu: `sudo apt install
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`). Sin ellas el resto de
  la app funciona igual; solo la descarga del PDF falla.
- **Certificado ARCA** autorizado al servicio `wsfex` (ver más abajo).
- **Reloj sincronizado** (NTP): el WSAA rechaza pedidos con clock skew. macOS y
  la mayoría de las distros Linux lo traen activo por defecto.
- Para backups: **[age](https://age-encryption.org)** (`winget install
  FiloSottile.age` / `brew install age`) y, si se sube a S3, **aws CLI**.

## Setup por ambiente

> **Nota (ADR 0001):** la selección de ambiente vía `ARCA_ENV` en `.env` y el
> home compartido descriptos abajo siguen siendo el comportamiento
> implementado, pero fueron **superados por decisión de diseño**: la dirección
> aprobada es un launcher que elige entre Homologación y Producción, cada uno
> con un perfil interno aislado, y cambio de ambiente solo por reinicio del
> backend (sin hot switching). Ver
> [`docs/adr/0001-perfiles-de-ambiente-aislados.md`](docs/adr/0001-perfiles-de-ambiente-aislados.md);
> esta documentación se actualizará al implementarse (FAC-34).

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

## Uso con Docker (recomendado)

El directorio de datos (`FACTURADOR_HOME`, default `~/facturador`) vive fuera
del repo y del contenedor. **La app lo crea sola en el primer arranque**,
incluido el `.env` de bootstrap; lo único que se coloca a mano son los
certificados:

```
~/facturador/
  .env                # lo crea la app: ARCA_ENV (+ puerto opcional)
  secrets/            # colocar acá el par del ambiente activo
    homo.key          # chmod 400 — la app se niega a arrancar con permisos laxos
    homo.crt
    prod.key          # solo al pasar a producción
    prod.crt
  data/               # la crea la app: facturador.db, pdfs/, logs/
  backups/            # .tar.gz.age generados por facturador.backup
```

Levantar: doble click en `scripts/launch.cmd` (Windows) o
`scripts/launch.command` (macOS) — levanta el contenedor si hace falta y abre
`http://localhost:8399`. Equivalente manual: `docker compose up -d`. Si
`FACTURADOR_HOME` no es `~/facturador`, exportarlo antes de levantar compose.

El puerto se publica **solo en `127.0.0.1`**: la app no es accesible desde la
red. Dentro del contenedor, el entrypoint copia los secretos a un directorio
interno con `chmod 400` (los bind mounts de Docker Desktop no tienen semántica
POSIX confiable) y deja `data/` y `backups/` apuntando al host para que todo
persista.

## Uso sin Docker

```bash
git clone <url-del-repo> facturador && cd facturador
uv sync
uv run python -m facturador
```

Abre `http://127.0.0.1:8399`. La app escucha **solo en localhost** por diseño
(el host no es configurable): la única conexión de red es saliente hacia ARCA.

### Configuración

La app es dueña de su configuración: casi todo se edita desde la página
**Configuración** de la UI y se guarda en la DB (datos del emisor que van al
PDF, punto de venta, bucket S3 de backups), así viaja dentro del backup
cifrado como parte del estado.

Lo único que queda afuera es el **bootstrap**, en `<FACTURADOR_HOME>/.env`
(la app lo crea en el primer arranque; **nunca** se lee un `.env` del
directorio de trabajo):

```dotenv
# Ambiente: "homo" o "prod". De este ÚNICO flag se derivan las URLs de
# WSAA/WSFEX y los paths de certificado (secrets/<env>.key / <env>.crt);
# es imposible por construcción mezclar cert de homologación con producción.
ARCA_ENV=homo

# Puerto local (siempre en 127.0.0.1).
#FACTURADOR_PORT=8399
```

La clave privada va **sin passphrase**: la protegen los permisos `400`, el
home local y el cifrado del backup al salir de la máquina.

La raíz de datos se elige con la variable de entorno `FACTURADOR_HOME`
(default `~/facturador`). El CUIT emisor no se configura: se extrae del
certificado.

En el arranque la app crea el home y su estructura si no existen, y valida
que existan `secrets/<env>.crt` y `secrets/<env>.key` con la key en permisos
`400`/`600`; si no, se niega a arrancar con un mensaje explicativo. Sin datos
de emisor cargados, la UI dirige a Configuración antes de permitir emitir.

### Flujo en la interfaz web

1. **`/configuracion`** — primera vez: completar los datos del emisor (se
   imprimen en el PDF), punto de venta y, opcionalmente, el bucket de backups.
2. **`/`** — form de nueva factura, precargado con el cliente default. El caso
   habitual se reduce a monto + fecha de pago + descripción; la cotización de la
   moneda la trae ARCA automáticamente para la fecha.
3. **Revisar** — página que muestra exactamente qué se va a enviar a ARCA. Nada
   viaja sin pasar por acá.
4. **Confirmar** — ejecuta la autorización (WSFEX `FEXAuthorize`) y redirige al
   detalle con el CAE, su vencimiento y el botón de descarga del PDF.
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

Útiles para verificar la conectividad con ARCA paso a paso:

```bash
uv run python scripts/get_ta.py         # obtiene (o reutiliza) el ticket WSAA
uv run python scripts/check_wsfex.py    # FEXDummy + descarga de tablas de parámetros
uv run python scripts/authorize_homo.py # flujo completo de emisión en homologación
```

## Backups

El estado completo (DB — configuración incluida — + PDFs + secretos + `.env`)
se respalda cifrado del lado del cliente con age y, opcionalmente, se sube a
un bucket S3 privado (se configura en la página Configuración de la app). La
passphrase es del usuario y no vive en ningún lado. Correr en el host después
de emitir:

```bash
uv run python -m facturador.backup                                # cifra a backups/ y sube si hay bucket
uv run python -m facturador.restore --latest --bucket mi-bucket   # máquina secundaria: baja y restaura
```

El home se resuelve igual que en la app: `--home`, `FACTURADOR_HOME` o el
default `~/facturador` — nunca el directorio de trabajo. El bucket del backup
sale de la DB (dentro del propio backup); el restore con `--latest` lo recibe
por `--bucket` porque en una máquina nueva todavía no hay DB.

Nunca correr dos copias emitiendo en paralelo: el chequeo de DB desactualizada
contra ARCA bloquea la emisión si el registro local quedó viejo, pero el orden
primaria → backup → restore → secundaria es responsabilidad del usuario.

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
  config.py     # bootstrap de arranque: home, .env mínimo, certificados
  settings.py   # configuración de dominio (vive en la DB, se edita en la UI)
  backup.py     # backup cifrado (age → S3); restore.py es el inverso
  schema.sql    # esquema de la base
docker/         # entrypoint del contenedor (ver Dockerfile y docker-compose.yml)
scripts/        # diagnóstico, flujo de homologación y launchers
tests/          # pytest (incluye ARCA falso en tests/arca_fake.py)
docs/
  design.md             # diseño, decisiones y contexto de dominio
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
- Los backups se cifran del lado del cliente **antes** de salir de la máquina;
  el tarball con la clave fiscal nunca toca el disco ni S3 en claro.

## Licencia

[MIT](LICENSE).

# Setup: homologación (ambiente de prueba)

Guía paso a paso para dejar FacturadorE funcionando contra el ambiente de
**Homologación** de ARCA. Acá se puede probar el flujo completo (WSAA → WSFEX →
CAE → PDF) sin ningún efecto fiscal: los comprobantes autorizados en
homologación no valen nada.

FacturadorE es **una sola app**: Homologación y Producción son dos opciones del
launcher, cada una con estado aislado. No hace falta una segunda instalación.
Para el pasaje a producción, ver [`setup-produccion.md`](setup-produccion.md).
Contrato de ambientes: [`adr/0001-perfiles-de-ambiente-aislados.md`](adr/0001-perfiles-de-ambiente-aislados.md).

## Requisitos previos

- Clave fiscal de ARCA (nivel suficiente para operar servicios con clave fiscal).
- Python ≥ 3.12 y [uv](https://docs.astral.sh/uv/), **o** Docker Desktop.
- Chromium de Playwright si corrés sin Docker (FAC-82). Tras `uv sync`:
  `uv run playwright install chromium` (una vez por máquina / venv). En
  Linux, si faltan librerías del sistema del browser:
  `uv run playwright install --with-deps chromium`.
- **Reloj sincronizado por NTP.** El clock skew es la causa número 1 de errores
  del WSAA; macOS y la mayoría de las distros Linux lo traen activo por defecto.

## 1. Generar clave privada y CSR

En una terminal (reemplazar el CUIT por el del emisor):

```bash
openssl genrsa -out cert.key 2048
openssl req -new -key cert.key \
  -subj "/C=AR/O=MiEmpresa/CN=facturador/serialNumber=CUIT 20XXXXXXXXX" \
  -out pedido.csr
```

La clave privada `cert.key` **nunca** se sube a ningún lado (ni a ARCA ni al
repo); solo viaja el CSR.

## 2. Obtener el certificado en WSASS

1. Con clave fiscal, entrar a **WSASS** ("Autoservicio de Acceso a APIs de
   Homologación"). Si no aparece entre los servicios habilitados, agregarlo
   desde el "Administrador de Relaciones de Clave Fiscal".
2. En *Nuevo certificado*, pegar el contenido de `pedido.csr` y crear el
   certificado.
3. Descargar el certificado emitido y guardarlo como `cert.crt`.
4. En *Autorizar servicio*, **autorizar ese certificado al servicio `wsfex`**
   (Facturación Electrónica de Exportación). Sin este paso el WSAA emite el
   ticket pero WSFEX rechaza todas las llamadas.
5. Para probar la constatación in-app (FAC-84), autorizar también el servicio
   **`wscdc`** (Constatación de Comprobantes). Emisión y constatación usan
   Tickets de Acceso distintos; el menú «Constatación de CAE» no reutiliza el
   TA de `wsfex`.

Manual oficial de WSASS: `arca.gob.ar/ws/WSASS/WSASS_manual.pdf`.

## 3. Colocar el certificado del perfil Homologación

Al elegir **Homologación**, FacturadorE usa un perfil interno aislado (el
usuario no administra dos directorios de instalación). El par debe llamarse
exactamente `cert.crt` y `cert.key`; la key en modo `400`/`600`.

**Sin Docker (launcher nativo):**

```bash
uv sync
uv run playwright install chromium
# Linux (si faltan deps del sistema): uv run playwright install --with-deps chromium
uv run python -m facturador.launcher --env homo
```

Si faltan los certificados, el mensaje de arranque indica la carpeta
`secrets/` del perfil Homologación. Copiá ahí `cert.crt` y `cert.key`, luego
`chmod 400` sobre la key, y volvé a lanzar.

**Con Docker:** el estado del ambiente queda bajo
`<FACTURADOR_HOME>/profiles/homo/` (default `~/facturador/profiles/homo/`):

```bash
mkdir -p ~/facturador/profiles/homo/secrets
cp cert.key cert.crt ~/facturador/profiles/homo/secrets/
chmod 700 ~/facturador/profiles/homo/secrets
chmod 400 ~/facturador/profiles/homo/secrets/cert.key

# Los scripts launch.* no muestran el chooser: fijar el ambiente antes
cat >> ~/facturador/.env <<'EOF'
ARCA_ENV=homo
EOF
```

Sin `ARCA_ENV` en `~/facturador/.env`, el contenedor no arranca
(compose no inyecta el `ARCA_ENV` del shell del host). Después: `docker compose up -d` (o doble click en
`scripts/launch.command` / `launch.cmd`).

La app **se niega a arrancar** si falta el par o si la key tiene permisos más
laxos que `400`/`600`.

## 4. Ambiente y configuración de dominio

Con el **launcher nativo**, el ambiente lo elige el chooser (Homologación).
Con **Docker**, el ambiente se fija escribiendo `ARCA_ENV` en
`FACTURADOR_HOME/.env` **antes** de levantar el contenedor — los scripts
`launch.*` no muestran el chooser y compose no pasa variables del shell al
contenedor. Un proceso backend = un ambiente inmutable; no se cambia
editando el flag en caliente (hay que reiniciar).

El resto de la configuración (datos del emisor que van al PDF, punto de
venta, backups) **vive en la app**: se completa en la página **Configuración**
una vez levantada (paso 6). El emisor no elige ambiente: queda sellado al
perfil activo.

Para scripts/diagnóstico sin chooser (launcher nativo):
`ARCA_ENV=homo uv run python …`. Si los certificados están en el layout
Docker (`~/facturador/profiles/…`), sumá
`FACTURADOR_APP_DATA=~/facturador/profiles` para apuntar al mismo perfil.

## 5. Verificar la conectividad, pasos a paso

Todos los comandos se corren desde la raíz del repo, con Homologación
seleccionada. El launcher nativo resuelve el perfil en el app-data del SO;
si seguiste el camino Docker, hay que apuntar `FACTURADOR_APP_DATA` a
`~/facturador/profiles` (sin eso los scripts buscan otro `secrets/` y fallan
aunque el contenedor esté bien configurado):

```bash
uv sync

# Launcher nativo
ARCA_ENV=homo uv run python scripts/get_ta.py
ARCA_ENV=homo uv run python scripts/check_wsfex.py
ARCA_ENV=homo uv run python scripts/authorize_homo.py

# Docker (mismo layout que profiles/homo/)
export FACTURADOR_APP_DATA=~/facturador/profiles
ARCA_ENV=homo uv run python scripts/get_ta.py
ARCA_ENV=homo uv run python scripts/check_wsfex.py
ARCA_ENV=homo uv run python scripts/authorize_homo.py
```

Notas:

- `get_ta.py` imprime ambiente, vigencia y origen del ticket (cache o nuevo),
  nunca el token/sign. El ticket dura ~12 h y queda cacheado en el perfil
  (`data/ta-wsfex.json`); las corridas siguientes lo reutilizan.
- `authorize_homo.py` lee los datos de la factura de `data/invoice_input.json`
  bajo el perfil. Si no existe, lo crea con placeholders de homologación y
  continúa; se puede editar después para acercarlo al caso real.

## 6. Levantar la app y emitir de prueba

```bash
uv run python -m facturador.launcher --env homo
# o: elegir Homologación en el chooser sin --env
```

Abrir `http://127.0.0.1:8399` y:

1. Ir a `/configuracion` y completar los datos del emisor (razón social,
   domicilio, IIBB, inicio de actividades): se imprimen en el PDF y la app
   no permite emitir sin ellos. El punto de venta en homologación es libre
   (1 está bien).
2. Ir a `/clientes` y dar de alta un cliente (marcarlo como default para que el
   form lo precargue).
3. En `/`, completar monto, fecha de pago y descripción → **Revisar**.
4. Confirmar en la página de revisión → detalle con CAE, vencimiento y PDF.

El badge de la UI debe mostrar **Homologación**. Para pasar a Producción más
adelante, usá **Cambiar ambiente** (el launcher reinicia el backend); no
edites configuración en caliente para "mezclar" ambientes.

## Problemas frecuentes en homologación

| Síntoma | Causa y solución |
|---|---|
| WSAA rechaza el ticket ("certificado no válido", errores de firma) | El certificado no está autorizado al servicio `wsfex` en WSASS (paso 2.4), o se está usando la key equivocada. |
| Error de WSAA por fechas / "generationTime" | Clock skew: sincronizar el reloj por NTP (referencia: `time.afip.gov.ar`). |
| "El CEE ya posee un TA válido" | ARCA ya emitió un ticket vigente pero el cache local se perdió. Esperar a que venza (máx. ~12 h) o restaurar el `ta-wsfex.json` del perfil Homologación. |
| La app no arranca: falta `cert.key` / permisos laxos | Colocar `cert.crt` y `cert.key` en el `secrets/` del perfil Homologación; `chmod 400` sobre la key. El mensaje de arranque indica la ruta exacta. |
| Al autorizar: "Registro local desactualizado: ARCA reporta último comprobante N…" | Normal en homologación: el punto de venta es compartido con otros usuarios del ambiente de prueba, así que ARCA conoce comprobantes que tu DB local no tiene. La página de revisión ofrece forzar la emisión (equivale a `force_desync=true` en la API). **En producción este error nunca se fuerza a la ligera** (ver la guía de producción). |
| `FEXDummy` reporta servidores caídos | Mantenimiento de ARCA; reintentar más tarde. `GET /health/arca` muestra lo mismo desde la app. |

## Criterio de "homologación completa"

Antes de encarar producción conviene tener:

- [ ] Un CAE obtenido de punta a punta desde la interfaz web (no solo por script).
- [ ] El PDF descargado con QR, verificando que los datos del emisor y del
      cliente se vean correctos.
- [ ] Un reintento probado: cortar la app durante un authorize o repetir la
      confirmación, y verificar que ARCA devuelve el mismo CAE (reproceso) en
      lugar de duplicar el comprobante.

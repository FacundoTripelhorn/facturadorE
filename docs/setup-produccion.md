# Setup: producción

Guía paso a paso para pasar FacturadorE a **Producción**. A partir de acá los
comprobantes autorizados son reales, con validez fiscal y numeración que no se
puede deshacer: cada CAE emitido queda registrado en ARCA a nombre del emisor.

**Prerrequisito:** haber completado el circuito de homologación de punta a
punta ([`setup-homologacion.md`](setup-homologacion.md)). El código no cambia
entre ambientes; producción es un trámite de certificado + elegir **Producción**
en el launcher (perfil aislado, sin segunda instalación). Contrato:
[`adr/0001-perfiles-de-ambiente-aislados.md`](adr/0001-perfiles-de-ambiente-aislados.md).

## 1. Certificado de producción

1. Generar una clave privada y un CSR **nuevos** (no reutilizar los de
   homologación):

   ```bash
   openssl genrsa -out cert.key 2048
   openssl req -new -key cert.key \
     -subj "/C=AR/O=MiEmpresa/CN=facturador/serialNumber=CUIT 20XXXXXXXXX" \
     -out pedido-prod.csr
   ```

2. En el portal de ARCA, entrar al servicio **"Administración de Certificados
   Digitales"**, crear un alias, subir el CSR y descargar el certificado
   emitido (`cert.crt`).

## 2. Asociar el servicio de facturación de exportación

En **"Administrador de Relaciones de Clave Fiscal"**, asociar el certificado
(computador fiscal) al servicio **"Facturación Electrónica de Exportación"**.

> Atención: es un servicio **distinto** al de facturación electrónica nacional.
> Si solo se asocia el nacional, el WSAA emite el ticket igual pero WSFEX
> rechaza todas las llamadas.

## 3. Punto de venta exclusivo para exportación webservice

En **"Administración de Puntos de Venta y Domicilios"**, crear un punto de
venta **nuevo** con sistema **"RECE para aplicativos y web services"** (opción
para Responsable Inscripto).

- ARCA exige un punto de venta dedicado a exportación por webservice; **no se
  puede reusar** el de Comprobantes en Línea.
- La numeración del punto de venta nuevo arranca en `0001-00000001`. Las
  facturas históricas emitidas por Comprobantes en Línea viven en otro punto de
  venta: tener dos series es normal y esperado, no se migran ni se mezclan.

Si corresponde, completar además el empadronamiento en **"Regímenes de
facturación y registración (REAR/RECE/RFI)"**.

## 4. Configuración local (perfil Producción)

Producción usa su **propio perfil aislado** (DB, certificados, caches y
backups separados de Homologación). No se mezclan pares en un `secrets/`
compartido ni se "cambia el flag" dentro del mismo proceso.

**Sin Docker:**

```bash
uv run python -m facturador.launcher --env prod
```

Si faltan certificados, el mensaje indica el `secrets/` del perfil Producción.
Colocá ahí `cert.crt` / `cert.key` (key en `400`) y relanzá.

**Con Docker** (estado bajo `<FACTURADOR_HOME>/profiles/prod/`):

```bash
mkdir -p ~/facturador/profiles/prod/secrets
cp cert.key cert.crt ~/facturador/profiles/prod/secrets/
chmod 400 ~/facturador/profiles/prod/secrets/cert.key

# Fijar Producción y reiniciar el contenedor (no hay hot-switch)
printf 'ARCA_ENV=prod\n' > ~/facturador/.env
docker compose down && docker compose up -d
```

En la página **Configuración** de la sesión **Producción**, cargá el
**punto de venta** del PV RECE del paso 3 y los datos del emisor (la DB de
producción es distinta a la de homologación: no viajan solos al cambiar de
ambiente).

Cada comprobante queda registrado con su columna `environment` (`homo`/`prod`)
como auditoría; el historial de pruebas no vive en la misma base que el real.

## 5. Smoke test (sin emitir nada)

```bash
# Launcher nativo
ARCA_ENV=prod uv run python scripts/get_ta.py
ARCA_ENV=prod uv run python scripts/check_wsfex.py

# Docker (certs bajo ~/facturador/profiles/prod/)
export FACTURADOR_APP_DATA=~/facturador/profiles
ARCA_ENV=prod uv run python scripts/get_ta.py
ARCA_ENV=prod uv run python scripts/check_wsfex.py
```

Ambos deben mostrar `Ambiente: prod` y las URLs productivas. Si `get_ta.py`
falla acá, revisar los pasos 1 y 2; si falla `check_wsfex.py` con el ticket ya
emitido, casi siempre falta la asociación al servicio de exportación (paso 2).
Sin `FACTURADOR_APP_DATA` apuntando al layout Docker, el script busca el
perfil nativo y no ve los `cert.*` que copiaste bajo `profiles/prod/`.

## 6. Primera factura real

1. Levantar con Producción
   (`uv run python -m facturador.launcher --env prod`, o **Cambiar ambiente**
   desde Homologación — el launcher reinicia el backend) y verificar el badge
   **Producción**. Comprobar que la cotización de la moneda llega bien
   (`FEXGetPARAM_Ctz` para la fecha).
2. Emitir una **primera factura de monto chico** por el flujo normal
   (form → revisar → confirmar).
3. Verificar el CAE en el portal de ARCA con **"Constatación de Comprobantes"**
   (la verificación automatizada vía WSCDC es opcional a futuro). La app ya
   hace su propia verificación post-emisión con `FEXGetCMP`, pero para la
   primera real conviene constatar también del lado del portal.
4. Descargar el PDF y controlar datos del emisor, del cliente, importes, QR.

## Cuidados operativos en producción

- **Numeración / registro desactualizado.** Antes de emitir, la app compara el
  último comprobante que conoce ARCA (`FEXGetLast_CMP`) contra el máximo de la
  DB local. Si ARCA conoce comprobantes que la DB no tiene, bloquea la emisión
  con "Registro local desactualizado". En homologación eso se fuerza sin
  problema (punto de venta compartido); **en producción significa que la DB
  local no es la última** (por ejemplo, se emitió desde otra máquina): restaurar
  el último backup del perfil Producción antes de emitir. Forzar
  (`force_desync`) solo si se entiende exactamente por qué difiere.
- **Backups.** El estado de un perfil es `data/` + `secrets/`. Hacer backup
  después de cada emisión: nativo
  `uv run python -m facturador.backup --env prod`; Docker
  `uv run python -m facturador.backup --root ~/facturador/profiles/prod`.
  Para snapshots de la DB usar `sqlite3 .backup`, nunca copiar el archivo en
  caliente. Si el backup sale de la máquina (p. ej. S3), **cifrar del lado
  del cliente** antes de subir: el tarball contiene la clave fiscal.
- **Una sola máquina emite.** Si se usan varias computadoras, una es la
  primaria; en la secundaria se restaura el backup antes de emitir. Nunca
  emitir desde dos copias en paralelo (el chequeo de registro desactualizado
  ataja los olvidos, pero no es excusa para intentarlo).
- **La clave privada es la firma fiscal.** Permisos `400`, nunca en el repo ni
  en backups sin cifrar. Si se sospecha filtración, revocar el certificado en
  "Administración de Certificados Digitales" y emitir uno nuevo.
- **Eventos de ARCA.** Las respuestas de WSFEX pueden traer bloques `Events`
  (mantenimientos, cambios normativos). La app los loguea como warnings:
  mirarlos de vez en cuando, ahí avisan los cambios de esquema con anticipación.
- **No exponer la app.** Escucha solo en `127.0.0.1` por diseño. Si algún día
  hace falta acceso remoto, el camino es una red privada (Tailscale/WireGuard)
  hacia una máquina propia; nunca abrir el puerto a internet.
- **Cambio de ambiente.** Usá **Cambiar ambiente** / el launcher: reinicia el
  backend. No hay hot switching de certificados, DB o clientes ARCA en el
  mismo proceso.

## Checklist final

- [ ] Certificado productivo emitido y descargado (paso 1).
- [ ] Certificado asociado a "Facturación Electrónica de Exportación" (paso 2).
- [ ] Punto de venta RECE exclusivo de exportación creado y anotado (paso 3).
- [ ] `cert.key` (chmod 400) y `cert.crt` en el `secrets/` del perfil
      Producción (paso 4).
- [ ] Sesión Producción levantada; PV RECE cargado en Configuración (paso 4).
- [ ] `get_ta.py` y `check_wsfex.py` OK contra producción (paso 5).
- [ ] Primera factura de monto chico emitida y CAE constatado en el portal (paso 6).
- [ ] Backup post-emisión del perfil Producción funcionando.

# Setup: homologación (ambiente de prueba)

Guía paso a paso para dejar el facturador funcionando contra el ambiente de
**homologación** de ARCA. Acá se puede probar el flujo completo (WSAA → WSFEX →
CAE → PDF) sin ningún efecto fiscal: los comprobantes autorizados en
homologación no valen nada.

Para el pasaje a producción, ver [`setup-produccion.md`](setup-produccion.md).

## Requisitos previos

- Clave fiscal de ARCA (nivel suficiente para operar servicios con clave fiscal).
- Python ≥ 3.12 y [uv](https://docs.astral.sh/uv/).
- Dependencias de sistema de WeasyPrint (Pango, Cairo, GDK-PixBuf). En
  Debian/Ubuntu: `sudo apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0`.
- **Reloj sincronizado por NTP.** El clock skew es la causa número 1 de errores
  del WSAA; macOS y la mayoría de las distros Linux lo traen activo por defecto.

## 1. Generar clave privada y CSR

En una terminal (reemplazar el CUIT por el del emisor):

```bash
openssl genrsa -out homo.key 2048
openssl req -new -key homo.key \
  -subj "/C=AR/O=MiEmpresa/CN=facturador/serialNumber=CUIT 20XXXXXXXXX" \
  -out pedido.csr
```

La clave privada `homo.key` **nunca** se sube a ningún lado (ni a ARCA ni al
repo); solo viaja el CSR.

## 2. Obtener el certificado en WSASS

1. Con clave fiscal, entrar a **WSASS** ("Autoservicio de Acceso a APIs de
   Homologación"). Si no aparece entre los servicios habilitados, agregarlo
   desde el "Administrador de Relaciones de Clave Fiscal".
2. En *Nuevo certificado*, pegar el contenido de `pedido.csr` y crear el
   certificado.
3. Descargar el certificado emitido y guardarlo como `homo.crt`.
4. En *Autorizar servicio*, **autorizar ese certificado al servicio `wsfex`**
   (Facturación Electrónica de Exportación). Sin este paso el WSAA emite el
   ticket pero WSFEX rechaza todas las llamadas.

Manual oficial de WSASS: `arca.gob.ar/ws/WSASS/WSASS_manual.pdf`.

## 3. Armar el layout local

Elegir un directorio raíz de datos (recomendado: `~/facturador`) y copiar los
archivos generados:

```bash
mkdir -p ~/facturador/secrets
cp homo.key homo.crt ~/facturador/secrets/
chmod 700 ~/facturador/secrets
chmod 400 ~/facturador/secrets/homo.key
```

Los nombres son fijos por convención: `secrets/<ambiente>.key` y
`secrets/<ambiente>.crt`. La app **se niega a arrancar** si falta alguno de los
dos o si la key tiene permisos más laxos que `400`/`600`.

## 4. El `.env` de bootstrap

La app lee su `.env` **solo de `FACTURADOR_HOME`** (default `~/facturador`),
nunca del directorio de trabajo. En el primer arranque lo crea sola con
`ARCA_ENV=homo`, así que para homologación no hay nada que editar. Su
contenido completo posible:

```dotenv
# Ambiente: homo | prod. Único flag: deriva URLs y par de certificados.
ARCA_ENV=homo
# Passphrase de la clave privada, solo si la key la tiene.
#ARCA_KEY_PASSPHRASE=
# Puerto local (siempre en 127.0.0.1).
#FACTURADOR_PORT=8399
```

Del flag `ARCA_ENV=homo` se derivan automáticamente las URLs de homologación
(`wsaahomo.afip.gov.ar` y `wswhomo.afip.gov.ar`) y los paths
`secrets/homo.key` / `secrets/homo.crt`. No existen overrides por URL: es
imposible por construcción mezclar ambientes. El CUIT emisor no se configura:
se extrae del certificado.

El resto de la configuración (datos del emisor que van al PDF, punto de
venta, backups) **vive en la app**: se completa en la página
**Configuración** una vez levantada (paso 6).

> Si el home no es `~/facturador`, exportar `FACTURADOR_HOME` en el shell
> antes de correr cualquier comando.

## 5. Verificar la conectividad, paso a paso

Todos los comandos se corren desde la raíz del repo:

```bash
uv sync

# 5.1 — WSAA: obtiene el Ticket de Acceso y lo cachea
uv run python scripts/get_ta.py

# 5.2 — WSFEX: FEXDummy + descarga de tablas de parámetros al cache local
uv run python scripts/check_wsfex.py

# 5.3 — Flujo completo de emisión: cotización, numeración, FEXAuthorize,
#        verificación post-emisión con FEXGetCMP
uv run python scripts/authorize_homo.py
```

Notas:

- `get_ta.py` imprime ambiente, vigencia y origen del ticket (cache o nuevo),
  nunca el token/sign. El ticket dura ~12 h y queda cacheado en
  `data/ta-wsfex-homo.json`; las corridas siguientes lo reutilizan.
- `authorize_homo.py` lee los datos de la factura de `data/invoice_input.json`.
  Si no existe, lo crea con placeholders de homologación y continúa; se puede
  editar después para acercarlo al caso real.

## 6. Levantar la app y emitir de prueba

```bash
uv run python -m facturador
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

## Problemas frecuentes en homologación

| Síntoma | Causa y solución |
|---|---|
| WSAA rechaza el ticket ("certificado no válido", errores de firma) | El certificado no está autorizado al servicio `wsfex` en WSASS (paso 2.4), o se está usando la key equivocada. |
| Error de WSAA por fechas / "generationTime" | Clock skew: sincronizar el reloj por NTP (referencia: `time.afip.gov.ar`). |
| "El CEE ya posee un TA válido" | ARCA ya emitió un ticket vigente pero el cache local se perdió. Esperar a que venza (máx. ~12 h) o restaurar `data/ta-wsfex-homo.json`. |
| La app no arranca: "Permisos laxos en secrets/homo.key" | `chmod 400 ~/facturador/secrets/homo.key`. |
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

# Diseño: API para emisión de Factura E (exportación) en ARCA

> Este documento nació como plan de un spike y quedó como **documento de diseño del producto**. El objetivo del spike se cumplió el 2026-07-03: CAE válido para una Factura E en homologación con el flujo completo automatizado (auth WSAA → autorización WSFEX → persistencia → PDF con QR). Las menciones a "spike" en el texto son históricas; las decisiones siguen vigentes.

**Objetivo:** autorizar comprobantes tipo E reales contra ARCA (ex AFIP) desde una API propia, como Responsable Inscripto, reemplazando la emisión manual por Comprobantes en Línea. Producción es un cambio de configuración + trámite de certificado, no de código.

---

## 0. Flujo real del usuario (el problema a resolver)

Flujo actual, manual, ~1 vez por semana:

1. Genera un invoice en **Deel**.
2. Lo registra a mano en una planilla/registro personal.
3. Al cobrar (la semana siguiente), entra a **Comprobantes en Línea** de ARCA y genera la Factura E de exportación ese mismo día.
4. Descarga el PDF del comprobante y lo registra a mano en el mismo registro.

Lo que la app reemplaza: **pasos 3 y 4 completos**. Flujo objetivo: el día del cobro, abrir la app → form precargado con el cliente default → ingresar monto (y poco más) → un click → CAE + PDF descargable + registro persistido.

Implicancias de diseño que salen del flujo real:

- **La fecha de emisión es la fecha de cobro.** La cotización de la moneda debe ser la de ARCA para ese día (`FEXGetPARAM_Ctz`), traída automáticamente por el form, no ingresada a mano.
- **Campo `Fecha_pago`:** obligatorio para exportación de servicios y confirmado en el comprobante real, donde **difiere de la fecha de emisión** (cobro al día siguiente). Default del form = fecha_cbte, siempre editable.
- **El registro manual (pasos 2 y 4) desaparece:** la app ES el registro. El listado con estado, CAE y PDF descargable reemplaza la anotación a mano.
- **Deel (pasos 1–2) queda fuera de alcance:** el monto se ingresa a mano. El PDF de Deel es parseable a futuro, pero no justifica complejidad ahora.
- **Numeración al pasar a producción:** el punto de venta webservice nuevo arranca en 0001-00000001. Las facturas históricas de Comprobantes en Línea viven en otro punto de venta; tener dos series es normal y esperado, no se migran ni se mezclan.

**Paridad con Comprobantes en Línea:** se analizó un comprobante real del usuario (Factura E autorizada, exportación de servicios a Uruguay). El mapeo campo por campo y el layout objetivo del PDF salen de ahí (ver §0.1). Los datos reales (CUIT emisor, domicilios, cliente) NO van en este documento ni en el repo: viven en la configuración local / seed de la DB.

### 0.1 Mapeo del comprobante real → FEXAuthorize

Estructura observada en el comprobante de ejemplo (valores anonimizados):

| Campo del comprobante | Campo WSFEX | Observación para la app |
|---|---|---|
| Tipo E, Cód. 19 | `Cbte_Tipo = 19` | Fijo para factura; 21 NC / 20 ND a futuro |
| Compr. Nro `PPPPP-NNNNNNNN` | `Punto_vta` + `Cbte_nro` | El PV actual es de Comprobantes en Línea; el del WS será uno nuevo (serie nueva desde 1) |
| Fecha de Emisión | `Fecha_cbte` (AAAAMMDD) | Default: hoy (día del cobro) |
| Fecha de Pago | `Fecha_pago` | **Confirmado presente y puede diferir de la emisión** (en el ejemplo es el día siguiente). Default = fecha_cbte, editable |
| Cliente (razón social) | `Cliente` | De la entidad `clients` |
| Domicilio cliente | `Domicilio_cliente` | Snapshot al autorizar |
| CUIT País (p.ej. Uruguay persona jurídica) | `Cuit_pais_cliente` | De tabla `FEXGetPARAM_DST_CUIT` (genérico por país+tipo de persona, no es dato sensible) |
| ID Impositivo | `Id_impositivo` | Tax ID del cliente en su país (RUT en el ejemplo) |
| Destino del comprobante | `Dst_cmp` | Código ARCA de país (`FEXGetPARAM_DST_pais`) |
| Divisa USD | `Moneda_Id = "DOL"` | |
| Tipo de Cambio | `Moneda_ctz` | Cotización ARCA del día; **se informa y se imprime aunque el total esté en USD** |
| Forma de Pago ("WIRE TRANSFER") | `Forma_pago` | Texto libre; default del cliente |
| Incoterms | `Incoterms` | **Vacío en servicios** — el form lo oculta para tipo_expo=2 |
| Ítem único: código `0001`, descripción del servicio, cant. `1,000000`, U.Medida "unidades", precio unit. = total | `Items[]`: `Pro_codigo`, `Pro_ds`, `Pro_qty=1`, `Pro_umed=7` (unidades), `Pro_precio_uni`, `Pro_total_item` | Patrón real: 1 línea, qty 1, precio = importe total. El form puede reducirse a descripción (default del cliente) + monto |
| Importe Total | `Imp_total` | = suma de ítems; validar en dominio |
| Leyenda "IVA EXENTO OPERACIÓN DE EXPORTACIÓN", IIBB, inicio de actividades | — (no viajan a ARCA) | Datos del emisor para el PDF: snapshot inmutable en la factura al crear el borrador (FAC-10); la página Configuración edita la entidad `emisores` viva, no el historial |
| CAE + Fecha Vto. CAE + QR | respuesta de `FEXAuthorize` | Al PDF junto con QR RG 4892 |

Consecuencia para el frontend: el caso feliz semanal se reduce a **3 campos: monto, fecha de pago (default hoy) y descripción (default precargado)**. Todo lo demás sale del cliente default + cotización automática.

**Layout impreso del comprobante real** (replicado en `facturador/pdf/render.py`, renderer v1 con fpdf2; posiciones verificadas contra el PDF de Comprobantes en Línea):

- **Cabecera** partida al medio por la caja `E / COD. 19`. Izquierda: razón social en grande y los rótulos `Razón Social:`, `Domicilio Comercial:`, `Condición frente al IVA:`. Derecha: `FACTURA DE EXPORTACIÓN`, `Compr. Nro:` (con "r", número completo `PPPPP-NNNNNNNN`), `Fecha de Emisión:`, `CUIT:`, `Ingresos Brutos:` (texto literal de la condición, p.ej. "Exento" — nunca el CUIT como reemplazo), `Fecha de Inicio de Actividades:` (DD/MM/AAAA) y la leyenda `IVA EXENTO OPERACIÓN DE EXPORTACIÓN` cerrando la columna. La leyenda NO es una banda centrada después de los ítems.
- **Receptor**: `Señor(es):` y `Domicilio:` comparten fila; `CUIT País:` (con la descripción del cache de params entre paréntesis) e `ID Impositivo:` a línea completa debajo.
- **Operación**: `Divisa:` (código display + descripción) con `Destino del Comprobante:` apilado debajo; después una fila de tres columnas `Forma de Pago: | Fecha de Pago: | Incoterms:` (Incoterms se imprime aunque esté vacío). La cotización NO va en este bloque.
- **Ítems**: columnas `Ítem | Descripción | Cantidad | Precio Unit. (USD) | Total por ítem (USD)` — U. Medida no es columna: va como segunda línea bajo la cantidad (`U. Medida: unidades`). La columna Ítem lleva el ordinal de 4 dígitos y la descripción va con el código adelante (`0001 - …`). Recuadro solo en la fila de encabezado; el comprobante casi no tiene marcos (tampoco marco exterior de página).
- **Totales anclados al pie de la página** (la zona de ítems se estira): `Tipo de Cambio:` (el único número con punto decimal, 6 decimales) junto a `Divisa:` repetida, y debajo `Importe Total: USD n,nn`. Resto de números: coma decimal, 6 decimales en cantidades/precios unitarios y 2 en importes.
- **Pie**: QR RG 4892 abajo a la izquierda, logo de ARCA + `Comprobante Autorizado` al lado (el logo está pendiente: FAC-7), `CAE N°:` y `Fecha de Vto. de CAE:` a la derecha, y el descargo estándar en letra chica.
- El PDF real sale en **tirada de tres copias** (ORIGINAL / DUPLICADO / COPIA, rótulo en caja arriba al centro); nuestro render de una sola copia queda en FAC-6.

---

## 1. Contexto de dominio (leer antes de codear)

### 1.1 Qué web service corresponde

- La Factura E (exportación) **NO** se emite por WSFEv1 (el servicio "común" de facturas A/B/C). Se emite por **WSFEX / WSFEXv1**, el web service específico de exportación.
- Tipos de comprobante relevantes (tabla dinámica de ARCA, verificar con `FEXGetPARAM_Cbte_Tipo`):
  - `19` = Factura E
  - `20` = Nota de Débito E
  - `21` = Nota de Crédito E
- WSFEX exige más datos que WSFEv1: ítems obligatorios, moneda + cotización, país de destino, CUIT país destino, Incoterms, forma de pago, tipo de exportación (1=bienes, 2=servicios, 4=otros), idioma del comprobante, permisos de embarque (si aplica a bienes).

### 1.2 Autenticación: WSAA

Todos los WS de negocio de ARCA requieren primero un **Ticket de Acceso (TA)** emitido por el WSAA:

1. Generar un `LoginTicketRequest.xml` (TRA) con `service=wsfex`, `generationTime`, `expirationTime`, `uniqueId`.
2. Firmarlo como **CMS/PKCS#7** con el certificado X.509 + clave privada del contribuyente.
3. Codificar en Base64 y enviarlo al método SOAP `LoginCms` del WSAA.
4. La respuesta trae `token` + `sign`, válidos ~12 horas. **Cachear el TA** y renovar solo al vencer; ARCA rechaza pedidos de TA nuevos si ya hay uno vigente ("El CEE ya posee un TA válido").
5. El reloj del servidor debe estar sincronizado (NTP contra `time.afip.gov.ar`); el clock skew es la causa #1 de errores de WSAA.

Cada llamada a WSFEX lleva un bloque `Auth { Token, Sign, Cuit }`.

### 1.3 Endpoints

| Servicio | Homologación | Producción |
|---|---|---|
| WSAA | `https://wsaahomo.afip.gov.ar/ws/services/LoginCms` | `https://wsaa.afip.gov.ar/ws/services/LoginCms` |
| WSFEXv1 | `https://wswhomo.afip.gov.ar/wsfexv1/service.asmx` (WSDL: `?WSDL`) | `https://servicios1.afip.gov.ar/wsfexv1/service.asmx` |

Notas:
- Son servicios ASMX (.NET legacy), SOAP 1.1/1.2. El WSDL a veces cambia; no vale la pena generar clientes estáticos, conviene un cliente SOAP dinámico o requests XML armados a mano.
- Certificados de homologación NO deberían funcionar en producción y viceversa, pero hubo casos históricos de cruce: **validar por config que el par (cert, URL) sea consistente** y loguear el ambiente en cada CAE emitido.

### 1.4 Trámites previos (bloqueantes, hacer en paralelo al código)

**Para homologación:**
1. Con clave fiscal, entrar a **WSASS** ("Autoservicio de Acceso a APIs de Homologación").
2. Generar clave privada + CSR con OpenSSL:
   ```
   openssl genrsa -out privada.key 2048
   openssl req -new -key privada.key -subj "/C=AR/O=MiEmpresa/CN=facturador/serialNumber=CUIT 20XXXXXXXXX" -out pedido.csr
   ```
3. Subir el CSR en WSASS, descargar el certificado, y **autorizar el certificado al servicio `wsfex`** dentro de WSASS.

**Para producción (después del spike):**
1. Servicio "Administración de Certificados Digitales" en el portal ARCA: crear alias, subir CSR, descargar certificado.
2. En "Administrador de Relaciones de Clave Fiscal": asociar el computador fiscal al servicio **"Facturación Electrónica de Exportación"** (es un servicio distinto al de facturación nacional — si solo se asocia el nacional, WSFEX rechaza).
3. **Punto de venta exclusivo para exportación**: en "Administración de Puntos de Venta y Domicilios", crear un punto de venta nuevo con sistema **"RECE para aplicativos y web services"** (opción para Responsable Inscripto). ARCA exige un punto de venta dedicado a exportación webservice; no se puede reusar el de comprobantes en línea.
4. Empadronamiento en "Regímenes de facturación y registración (REAR/RECE/RFI)" → RECE si corresponde.

En homologación el punto de venta es libre (usar p.ej. `1`), pero la numeración es secuencial igual: siempre consultar `FEXGetLast_CMP` antes de autorizar.

### 1.5 Particularidades de WSFEX que condicionan el diseño

- **Tablas dinámicas de parámetros:** monedas (`FEXGetPARAM_MON`), países (`FEXGetPARAM_DST_pais`), CUITs de países (`FEXGetPARAM_DST_CUIT`), unidades de medida (`FEXGetPARAM_UMed`), idiomas, Incoterms, tipos de exportación, tipos de comprobante. Tienen vigencia desde/hasta y pueden cambiar → cachearlas localmente con TTL (p.ej. 24 h) y exponerlas por la API.
- **Cotización de moneda:** para facturas en moneda extranjera hay que informar `Moneda_ctz`. Existe `FEXGetPARAM_Ctz` (cotización del día) y `FEXGetPARAM_MonConCotizacion` (cotización ADUANA por fecha). Regla general: usar la cotización que devuelve ARCA para la fecha, no una propia.
- **Id secuencial + reproceso:** `FEXAuthorize` recibe un `Id` (int64) provisto por el cliente. `FEXGetLast_ID` devuelve el último usado. Si un request con el mismo `Id` y los mismos datos se reintenta, ARCA lo trata como reproceso (devuelve el mismo CAE) → esto es la base de la **idempotencia**; persistir el `Id` antes de llamar.
- **Recuperación ante timeout:** si la conexión se corta después de enviar, consultar `FEXGetCMP` (por tipo, punto de venta y número) para saber si se autorizó, antes de reintentar.
- **Eventos:** las respuestas pueden incluir bloques `Events` (mantenimientos, avisos normativos). Loguearlos siempre y propagarlos como warnings, no como errores.
- **RG 5616 (condición IVA del receptor):** obligatoria en WSFEv1 desde 2025. Para WSFEX el receptor es del exterior y el campo relevante es el `Id_impositivo` + CUIT país destino, pero al implementar validar contra el manual vigente y los eventos que devuelva homologación — ARCA comunica cambios de campos por esta vía.
- **QR (RG 4892):** el PDF del comprobante debe incluir el código QR con el JSON base64 estándar (ver, cuit, ptoVta, tipoCmp, nroCmp, importe, moneda, ctz, tipoCodAut=E ... apunta a `https://www.afip.gob.ar/fe/qr/`).

---

## 2. Arquitectura propuesta

```
┌────────────┐     HTTPS/JSON      ┌──────────────────────────────┐
│  Frontend   │ ──────────────────▶ │  API (Python, FastAPI)       │
│ (Jinja+HTMX)│                     │                              │
└────────────┘                     │  ┌────────────────────────┐  │
                                   │  │ InvoiceService         │  │
                                   │  │  - validación dominio  │  │
                                   │  │  - numeración/idempot. │  │
                                   │  └───────┬────────────────┘  │
                                   │  ┌───────▼────────────────┐  │
                                   │  │ ArcaClient             │  │
                                   │  │  - WsaaAuth (TA cache) │  │
                                   │  │  - WsfexClient (SOAP)  │  │
                                   │  │  - ParamCache          │  │
                                   │  └───────┬────────────────┘  │
                                   └──────────┼───────────────────┘
                                              │ SOAP/HTTPS
                              ┌───────────────▼───────────────┐
                              │  ARCA: WSAA + WSFEXv1         │
                              └───────────────────────────────┘
                                   │
                              ┌────▼─────┐   ┌──────────────┐
                              │ SQLite   │   │ PDF renderer │
                              │ (Postgres│   │ (fpdf2 +     │
                              │ si crece)│   │  QR RG4892)  │
                              │          │   │              │
                              └──────────┘   └──────────────┘
```

### 2.1 Stack (DECIDIDO: Python de punta a punta)

- **API:** FastAPI + Pydantic (validación de dominio) + SQLite. Un solo proceso, sin colas ni workers: el volumen es ~1 factura/semana.
- **Cliente ARCA:** implementación propia (`ArcaClient`) con `httpx` y XML SOAP armado con templates (los requests de WSFEX son pocos y estables; no generar clientes desde el WSDL). Alternativa aceptable si se traba: `zeep` como cliente SOAP dinámico.
- **Firma CMS (WSAA):** librería `cryptography` → `pkcs7.PKCS7SignatureBuilder` (firma nativa, sin subprocesos de openssl).
- **PDF:** `fpdf2` (Python puro, sin browser ni subprocesos; FAC-88 reemplaza a
  Playwright Chromium de FAC-82, que a su vez reemplazó a WeasyPrint) + `qrcode`
  para el QR RG 4892. El layout v1 se dibuja en código (`facturador/pdf/render.py`)
  con Liberation Sans embebida (SIL OFL, UTF-8). Una sola página A4: si el
  contenido no entra, `PdfLayoutError` (409) en lugar de recortar.
- **Frontend:** Jinja2 + HTMX servido por la misma app FastAPI. Cero build tooling de JS; si a futuro se quiere SPA, la API JSON ya existe.
- **DB:** SQLite alcanza incluso más allá del spike dado el volumen; migrar a Postgres solo si aparece multiusuario real. La app es el único registro y fuente de verdad local; no se sincroniza con herramientas externas. El esquema (siempre bajo un perfil) se versiona con migraciones transaccionales (`facturador.migrations`, FAC-43): al conectar se aplica lo pendiente, un fallo hace rollback y bloquea el arranque, y cada conexión habilita `PRAGMA foreign_keys=ON`. La baseline es el esquema de perfiles aislados.
- **Secretos:** cert + key nunca en el repo. Variables de entorno o archivo montado con permisos 400; la key privada es equivalente a la firma fiscal de la empresa.

### 2.1.1 pyafipws como referencia: checklist de paridad de seguridad

Estrategia: **no depender de `pyafipws`** (codebase legacy, GPL v3) pero usarla como oráculo de comportamiento. Al escribir el cliente propio, verificar explícitamente que NO se pierda ninguna de estas protecciones que la librería ya resuelve. Esto es parte del "definition of done" del `ArcaClient`:

1. **Consistencia ambiente/certificado:** pyafipws advierte que hubo casos reales de CAE de homologación emitidos contra certificados cruzados. Replicar su mitigación: cada proceso backend arranca con **exactamente un ambiente inmutable** (perfil aislado); URLs y paths de cert salen del mismo perfil — imposible mezclar por construcción. Además, registrar el ambiente en cada comprobante y verificar el CAE emitido (ver punto 6).
2. **Validación del TA recibido:** parsear y validar `expirationTime` del ticket antes de usarlo/cachearlo; no asumir las 12 h. Rechazar TAs con `destination`/`service` que no sean `wsfex`.
3. **Reproceso seguro:** al reintentar un `FEXAuthorize` con el mismo `Id`, pyafipws compara que la respuesta corresponda a los mismos datos enviados (monto, tipo, punto de venta) antes de aceptar el CAE como propio. Replicar: nunca aceptar ciegamente un CAE de reproceso sin verificar contra el request persistido.
4. **XML injection/escaping:** todos los campos de texto libre (razón social, descripción de ítems, observaciones, domicilio) deben escaparse al armar el XML. Con templates a mano este es EL riesgo nuevo que pyafipws no tenía (usa serialización). Usar siempre el serializador de la lib XML, jamás f-strings/concatenación para valores.
5. **TLS:** verificación de certificados del servidor SIEMPRE activa en `httpx`
   (nunca `verify=False` — es el vector exacto para robar el token fiscal).
   **Excepción documentada (FAC-81):** el WSFEX de producción
   (`servicios1.afip.gov.ar`) negocia DHE con DH de 1024 bits, que OpenSSL 3
   rechaza con `DH_KEY_TOO_SMALL`. Solo en **Producción** y solo para el
   cliente WSFEX se usa un `SSLContext` derivado de
   `ssl.create_default_context()` con SECLEVEL=1 (misma política de cifrados
   del default de Python; no se cambia a `DEFAULT`), manteniendo la
   verificación del certificado. Homologación y WSAA (ambos ambientes)
   siguen en el default de httpx/OpenSSL — no hace falta acomodarlos.
6. **Verificación post-emisión:** tras autorizar, `FEXGetCMP` compara CAE +
   importe + número (idempotencia / recovery — no es constatación del portal).
   **WSCDC (FAC-84):** menú propio «Constatación de CAE» + botón **Constatar**
   (nunca auto tras `FEXAuthorize` / `FEXGetCMP`). Request con CUIT emisor,
   CAE, fecha, tipo 19, PV, nro, importe en moneda original, receptor tipo
   **80** + `cuit_pais_cliente` (CUIT país, no el tax ID extranjero). El
   selector cubre dos modos (grill #4): comprobante del registro local
   (prefill desde el snapshot) u **«Otro comprobante (externo)…»**, donde el
   operador tipea los mismos campos del portal para una Factura E no emitida
   por la app (p.ej. Comprobantes en Línea u otro PV; el CUIT emisor sale del
   certificado del perfil). TA WSAA
   con `service=wscdc` y cache aparte de `wsfex`. Portal ARCA sigue como
   respaldo si el servicio no está asociado o no responde.
7. **WSDL/cache desactualizado:** pyafipws documenta fallos por WSDL cacheado viejo (campos nuevos rechazados, ej. RG 5616). Al usar templates propios esto se transforma en: versionar los templates y tener contract tests contra homologación en CI que fallen ruidosamente si ARCA cambió el esquema.
8. **Clock sync:** generación del TRA con ventana amplia (gen -10 min / exp +10 min) y NTP contra `time.afip.gov.ar`, como documenta el manual WSAA.
9. **Manejo de la clave privada:** pyafipws soporta passphrase en la key; acá se DECIDIÓ no usarla (la app es local y sin operador que la tipee: la protegen los permisos 400 y el home local). La clave **no** viaja en el seed de backup (FAC-44); cada máquina tiene su propio par (FAC-64). Nunca loguear ni el CMS firmado ni el token/sign del TA (tratarlos como credenciales en los logs — redactar).

### 2.2 Modelo de datos (mínimo)

```
clients
  id (uuid, pk)
  razon_social (text)
  domicilio (text)
  pais_dst (int)                  -- código ARCA de país
  cuit_pais (bigint)              -- de FEXGetPARAM_DST_CUIT
  id_impositivo (text)            -- tax id del cliente en su país
  moneda_default (text), incoterms_default (text), idioma_default (int)
  is_default (bool)               -- cliente habitual, precargado en el form
  created_at, updated_at

invoices
  id (uuid, pk)
  emisor_id (fk → emisores, nullable)  -- trazabilidad; NO es fuente del PDF
  client_id (fk → clients, nullable)   -- referencia; los datos se snapshotean abajo
  arca_id (bigint, unique)        -- Id secuencial enviado a FEXAuthorize
  cbte_tipo (int)                 -- 19/20/21
  punto_venta (int)
  cbte_nro (bigint, null hasta autorizar)
  status (draft | submitting | authorized | rejected | unknown)
  source ('wsfex' | 'imported')  -- solo wsfex es autoritativo para numeración/emisión (FAC-48)
  fecha_cbte (date)
  tipo_expo (int)                 -- 1 bienes / 2 servicios / 4 otros
  permiso_existente ('S'|'N'|'')
  dst_cmp (int)                   -- país destino
  dst_cmp_ds (text)               -- descripción país al crear (FAC-52; PDF no relee arca_params)
  cliente (text), cuit_pais_cliente (bigint), cuit_pais_cliente_ds (text)
  domicilio_cliente (text)
  id_impositivo (text)
  moneda_id (text), moneda_ds (text), moneda_ctz (numeric)
  incoterms (text), incoterms_ds (text)
  forma_pago (text)
  idioma_cbte (int)
  imp_total (numeric)
  obs (text)
  cae (text, null), cae_fch_vto (date, null)
  raw_request (jsonb), raw_response (jsonb)   -- auditoría completa
  cuit_emisor (text)              -- CUIT fiscal del perfil al crear (FAC-39)
  emisor_razon_social, emisor_domicilio, emisor_condicion_iva,
  emisor_iibb, emisor_inicio_actividades  -- snapshot del emisor al crear el borrador (FAC-10)
  pdf_render_version (int)        -- contrato de render PDF (FAC-52; FAC-53 despacha)
  environment ('homo'|'prod')
  created_at, updated_at

invoice_items
  id, invoice_id (fk)
  pro_codigo, pro_ds, pro_qty, pro_umed, pro_umed_ds, pro_precio_uni, pro_total_item

arca_params (cache de tablas dinámicas)
  kind (moneda|pais|umed|incoterms|cbte_tipo|idioma|cuit_pais|tipo_expo)
  code, description, valid_from, valid_to, fetched_at
```

`status = unknown` es clave: se setea cuando hubo timeout post-envío y todavía no reconciliamos con `FEXGetCMP`.

### 2.3 API REST (contrato mínimo)

```
POST   /invoices                  -- crea draft, valida dominio
POST   /invoices/:id/authorize    -- ejecuta FEXAuthorize (idempotente)
GET    /invoices/arca             -- peek de solo lectura del registro ARCA
                                   -- (FEXGetLast_CMP + FEXGetCMP; no escribe local)
GET    /invoices/:id              -- estado + CAE
GET    /invoices/:id/pdf          -- PDF con QR (fase 2)
GET    /invoices                  -- listado paginado (registro local)
GET    /clients                   -- listado (incluye flag de cliente default)
POST   /clients                   -- alta
PUT    /clients/:id               -- edición (no afecta facturas ya autorizadas)
GET    /params/:kind              -- monedas, países, incoterms, etc. (del cache)
GET    /params/currency/:id/rate?date=YYYYMMDD  -- cotización ARCA
GET    /health/arca               -- FEXDummy (estado app/db/auth de ARCA)
```

Reglas de `authorize`:
1. Tomar lock sobre la invoice (evitar doble submit).
2. `FEXGetLast_ID` → asignar `arca_id = last + 1` si no tiene; `FEXGetLast_CMP(pto_vta, cbte_tipo)` → `cbte_nro = last + 1`.
3. Persistir `status=submitting` + request completo **antes** de llamar.
4. Llamar `FEXAuthorize`. Éxito → guardar CAE, vencimiento, `status=authorized`. Rechazo → `status=rejected` + errores/observaciones. Timeout → `status=unknown` y job de reconciliación con `FEXGetCMP`.
5. Reintento sobre `unknown`/`submitting` reutiliza el mismo `arca_id` y datos idénticos (reproceso ARCA devuelve el mismo CAE).

### 2.4 Frontend (deliberadamente mínimo)

Jinja2 + HTMX servido por la misma app FastAPI: formulario de factura con selects poblados desde `/params/*` y el cliente default precargado, listado de comprobantes con estado/CAE, botón de descarga de PDF. Nada más en el spike.

### 2.5 Infraestructura y despliegue

**Decisión: la app corre en la máquina local del usuario, accesible solo por `localhost`. No hay servidor público.**

Fundamento: el uso es 1 emisión/semana, un solo usuario, desde su propia computadora. Publicar la app en internet agregaría todo lo que NO queremos: gestión de TLS, capa de autenticación, superficie de ataque sobre un servicio que custodia la clave fiscal, y costo/operación de un VPS (un servicio externo más). Con `localhost` la única conexión de red es **saliente** hacia ARCA — la app no escucha para nadie más. Esto elimina de raíz clases enteras de riesgo.

**Ambientes (DECIDIDO — [ADR 0001](adr/0001-perfiles-de-ambiente-aislados.md)):** una sola app FacturadorE con dos opciones de negocio (**Homologación** / **Producción**). El launcher elige el ambiente; cada uno mapea a un **perfil interno oculto** (app-data del SO) con su propia SQLite, certificados, PDFs, caches WSAA/params, logs, onboarding y backups. Un proceso backend = un ambiente inmutable + un perfil. Cambiar de ambiente **detiene** el backend y arranca otro; **no hay hot switching**. **Un CUIT fiscal por perfil** (FAC-39): se deriva del certificado validado, se sella en `settings.fiscal_cuit`, se usa en toda llamada ARCA, se snapshotéa en `invoices.cuit_emisor`, y un cert o config de emisor con otro CUIT se rechaza (cambiar de contribuyente = perfil nuevo o reset; sin migración automática). Las rutas de perfil no se exponen en la UI normal.

Componentes:

- **Launcher (experiencia de app de escritorio):** `python -m facturador.launcher` muestra Homologación/Producción, resuelve el perfil oculto, supervisa el proceso hijo (`python -m facturador`), espera `GET /health` y abre la UI en una **ventana nativa** vía [pywebview](https://pywebview.flowrl.com/) (FAC-83: WebView2 en Windows, WebKit en macOS) apuntando a `http://127.0.0.1:<port>/`. El título incluye el producto y el ambiente (`FacturadorE — Homologación` / `Producción`). Cerrar la ventana detiene el backend supervisado; `--no-browser` salta la UI (automatización / agents). Si el webview no puede arrancar (p.ej. WebView2 ausente en Windows), se cae al navegador del sistema con un mensaje en el log. "Cambiar ambiente" en la UI pide un reinicio orquestado por el launcher (se interrumpe la ventana para mostrar el chooser). Se evaluó y descartó una desktop app real (Electron/Tauri/Qt); pywebview es solo el shell — la UI sigue siendo Jinja/HTMX. Doble click Docker: `scripts/launch.cmd` / `scripts/launch.command` (sigue abriendo el browser del host contra el puerto publicado).
- **Runtime (DECIDIDO): Docker Desktop**, porque el usuario alterna entre Windows y macOS. La misma imagen corre en ambos; los permisos POSIX de la key y el chequeo de arranque se implementan una sola vez dentro del contenedor (Linux). Bind mount de `FACTURADOR_HOME` al contenedor; bind explícito del puerto a `127.0.0.1` (nunca `0.0.0.0`). Un contenedor = un ambiente.
- **Estado en múltiples máquinas:** **ARCA es el ledger autoritativo.** La DB local es una copia de conveniencia por máquina, sincronizada **desde ARCA** (catch-up FAC-48 / rebuild FAC-65), nunca a través de S3. S3 solo transporta el **seed** de configuración. Cada máquina tiene sus propios certificados e identidad `age` (FAC-64); los secretos no viajan entre máquinas. Nunca emitir desde dos copias en paralelo.
- **Layout de datos (fuera del repo, detalle de implementación):** el bootstrap mínimo (`FACTURADOR_HOME`, default `~/facturador`; en Docker `/facturador`) guarda solo el `.env` opcional de puerto/ambiente para entrypoints sin launcher. **Todo el runtime** (DB, `secrets/cert.{crt,key}`, PDFs, TA cache, logs, backups) vive bajo el perfil del ambiente vía `ProfilePaths` / `EnvironmentProfile` (`facturador/profile.py`) — sin sufijos `<env>` en nombres de archivo dentro del perfil. La app crea el layout en el primer arranque; el usuario no administra dos instalaciones.
- **Configuración: la app es dueña de su configuración.** El ambiente lo inyecta el launcher (o un `ARCA_ENV` explícito en el proceso) **una sola vez** antes de construir FastAPI/SQLite/clientes ARCA (checklist §2.1.1 punto 1). El CUIT emisor se extrae del certificado y es la identidad fiscal inmutable del perfil (FAC-39); la configuración de emisor (razón social, domicilio, etc.) no puede sobrescribirlo. Todo lo demás — datos del emisor, punto de venta, config de backups — vive en la DB del perfil y se edita desde Configuración. El emisor es **local al perfil** (sin selector de ambiente en el form/API). **Setup por perfil (FAC-35):** cada perfil persiste su paso en `data/onboarding.json` (`uninitialized` → `certificate_required` → `emisor_required` → `point_of_sale_required` → `ready`). El backend puede arrancar sin el par cert/key (queda en `certificate_required`); si la clave existe debe ser modo `400`/`600`. Hasta `ready`, la guardia bloquea creación/autorización de facturas y operaciones ARCA: las rutas HTML de la UI redirigen a `/setup` (FAC-37/38); las APIs JSON responden 503 con `setup_state`. `GET /health`, `GET /setup` y la configuración de emisor siguen disponibles.
- **Backups (DECIDIDO — FAC-44): seed cifrado de configuración, no snapshot de DB.** El bundle es kilobyte-scale y casi estático: CUIT fiscal, ambiente, emisores (campos de PDF), PV + set de tipos de comprobante, cliente default / UI, versión de esquema, más un manifiesto interno (schema version, device-id, timestamp, checksum). **Excluido:** DB, PDFs (regenerables FAC-53), certificados, claves privadas, identidades `age`, datos por comprobante (todo re-deriva vía `FEXGetCMP`, FAC-63). Cifrado del lado del cliente con `age` a **todas** las claves de `recipients.txt` (una por máquina). Layout lógico en el bucket privado del usuario: `{prefix}/{cuit}/{env}/seed.age` (overwrite; versioning del bucket como red de seguridad) y `recipients.txt` en claro al lado. Upload: FAC-45. Rebuild del registro: FAC-65. La emisión nunca depende de S3. La posición de secuencia (`last_CMP`) la responde siempre ARCA en vivo — el manifiesto no la usa como criterio de sync.
- **Seed S3 adapter (FAC-45):** put/get del seed ya cifrado y get/put/append de `recipients.txt` en las claves fijas anteriores. Implementación: `facturador/s3_seed.py` (`S3SeedAdapter`). Bucket/prefix salen de la config del perfil; credenciales vía la cadena estándar de AWS con scope mínimo `s3:GetObject`/`s3:PutObject` sobre el prefijo. Sin lógica de listado ni versiones en la app — **habilitar versioning del bucket** como red de seguridad ante overwrites. El adapter nunca toca plaintext del seed; un solo `PutObject` del blob completo (sin multipart parcial).
- **Seed backup trigger (FAC-47):** un upload fresco se dispara cuando cambia la **configuración del perfil** (onboarding → ready, emisor alta/edición, PV, cliente default / UI S3, recipients age), no tras cada autorización — el seed no cambia con la emisión. Varias ediciones en la misma sesión se coalescen (debounce). Si S3 está caído, el cambio de config se persiste igual; el estado `pending`/`failed` queda en settings (`seed_backup_*`, queryable para FAC-49) y se reintenta en el próximo arranque o con `POST /backup/seed`. Implementación: `facturador/seed_backup_sync.py`.
- **Detección de DB desactualizada (obligatorio dado el esquema multi-máquina, FAC-48):** antes de asignar número / enviar `FEXAuthorize`, comparar `FEXGetLast_CMP` contra el máximo `cbte_nro` local **autorizado con `source=wsfex`** (los importados históricos no cuentan). Si ARCA conoce comprobantes que el registro wsfex local no tiene → bloqueo: "registro local desactualizado — sincronizar desde ARCA (catch-up FAC-65) antes de emitir". Si el local wsfex está por delante de ARCA → bloqueo de inconsistencia (no forzable). Estado alineado (`last_cmp == local_max`) permite emitir. Remediación: `POST /registry/catch-up` (append `N_local+1..N_arca`); restore completo desde el launcher con backend detenido (`python -m facturador.launcher --restore` / `facturador.restore`). Ver también [`docs/wsfex-gap-vs-error.md`](wsfex-gap-vs-error.md).
- **Reloj:** requisito de NTP activo en la máquina (macOS/Linux lo traen por defecto; documentar la verificación). Sin reloj sincronizado, WSAA falla.
- **Logs:** archivo local del perfil con rotación. Token/sign del TA y CMS firmado siempre redactados (checklist §2.1.1 punto 9).
- **Host / Origin (FAC-41):** middleware estricto en FastAPI. Solo acepta `Host` `127.0.0.1:<port>`, `localhost:<port>` o `[::1]:<port>`. En nativo el puerto es `FACTURADOR_PORT` / launcher. En Docker el bind interno queda en 8399 y el browser usa el puerto publicado del host: compose inyecta `FACTURADOR_PUBLIC_PORT` para que la allowlist acepte ambos. En POST/PUT/PATCH/DELETE, si viene `Origin` o `Referer`, debe ser `http://` + esa misma forma de loopback — sin `*`, sin HTTPS (no hay TLS local). Mitiga DNS rebinding y cross-origin inesperados.
- **CSRF en formularios (FAC-42 / FAC-62):** double-submit cookie (`facturador_csrf`, HttpOnly, SameSite=Strict) + campo oculto `csrf_token` (o header `X-CSRF-Token`) en todo POST bajo `/ui/`. Sin login ni API keys. La API JSON y los GET no exigen el token. Rechazo bajo `/ui/` → HTML de sesión expirada (recargar); fuera de `/ui/` → JSON genérico. El valor del token no se loguea ni se ecoa en errores.
- **Confirmación de Producción (FAC-40):** la primera vez que se abre el perfil `prod` hace falta un ack explícito sobre validez fiscal real. En el launcher nativo es un diálogo (cancelar vuelve al chooser). En Docker / `python -m facturador`, sin ack el arranque falla hasta reiniciar una vez con `FACTURADOR_ACK_PRODUCTION=1` (queda en `data/production_ack.json` del perfil Producción). Homologación no pide ni escribe el flag. La UI de Producción mantiene badge y avisos de seguridad en setup/autorización.
- **CI (contract tests contra homologación):** correr localmente con un comando (`make contract-tests`). NO subir certificados de homologación a GitHub Actions — un runner externo con la key es un riesgo innecesario; si a futuro se quiere CI remota, se genera un certificado de homologación dedicado y descartable para eso.

**Evolución futura (fuera de alcance, documentada para no re-decidir):** si algún día se quiere emitir desde el teléfono, el camino es mover la misma imagen Docker a una máquina siempre encendida propia (mini-PC/Raspberry) y acceder por una red privada tipo Tailscale/WireGuard — nunca exponiendo el puerto a internet. Un VPS es la última opción, porque implica custodiar la clave fiscal fuera de hardware propio.

---

## 3. Riesgos y trampas conocidas

| Riesgo | Mitigación |
|---|---|
| Clock skew → WSAA rechaza el TRA | NTP obligatorio; tolerancia generosa en generation/expiration del TRA (p.ej. -10 min / +10 min) |
| Pedir TA teniendo uno vigente → error WSAA | Cache persistente del TA (sobrevive reinicios del proceso) |
| Doble emisión por reintentos | `Id` secuencial persistido pre-llamada + reproceso ARCA + reconciliación `FEXGetCMP` |
| Numeración desincronizada | ARCA es la fuente de verdad: siempre `FEXGetLast_CMP` antes de autorizar, nunca contador local |
| Tablas dinámicas desactualizadas (moneda/país dado de baja) | Refresh diario del cache + revalidar código contra ARCA al autorizar |
| Certificado homo usado contra prod (o viceversa) | Un ambiente inmutable por proceso + perfil aislado: URLs y cert del mismo perfil; log del ambiente en cada CAE |
| WSDL/ASMX quirks (SOAPAction, encoding) | Tests de contrato contra homologación en CI (al menos `FEXDummy` + `FEXGetPARAM_MON`) |
| Cambios normativos (tipo RG 5616) llegan como "eventos" | Loguear y alertar sobre todo bloque `Events`/`Obs` de las respuestas |
| Clave privada filtrada | Fuera del repo, permisos 400, secret manager en prod, rotación documentada |

---

## 4. Plan de ejecución del spike (para Claude Code)

**Fase 0 — Trámites (humano, en paralelo):** certificado de homologación vía WSASS autorizado al servicio `wsfex`. Sin esto no hay fase 2.

**Fase 1 — WSAA client:**
- Script/módulo que arma el TRA, lo firma (CMS con node-forge u `openssl smime`), llama `LoginCms` en homo y persiste el TA (token, sign, expiration) en disco/DB.
- Test: obtener TA, reusarlo, verificar renovación al expirar.

**Fase 2 — Conectividad WSFEX:**
- `FEXDummy` (no requiere auth compleja, chequea appserver/dbserver/authserver).
- Con TA: `FEXGetPARAM_MON`, `FEXGetPARAM_DST_pais`, `FEXGetPARAM_Cbte_Tipo`, `FEXGetPARAM_UMed`, `FEXGetPARAM_Incoterms`. Persistir en `arca_params`.

**Fase 3 — Primer CAE en homologación (núcleo del spike):**
- `FEXGetLast_ID`, `FEXGetLast_CMP`, `FEXAuthorize` con una factura E de servicios (`tipo_expo=2`, sin permisos de embarque; moneda `DOL` con cotización de `FEXGetPARAM_Ctz`; país destino y CUIT país de `FEXGetPARAM_DST_CUIT`). Usar los datos del cliente real: este caso de prueba ES el caso de producción.
- Verificar con `FEXGetCMP` que el comprobante quedó registrado. **Acá termina el riesgo técnico.**

**Fase 4 — API:**
- FastAPI + SQLite + endpoints de §2.3 + máquina de estados + reconciliación de `unknown`.
- Tests: idempotencia de authorize, reproceso verificado (checklist §2.1.1 punto 3), escaping XML con datos hostiles, rechazo con errores legibles.

**Fase 5 — PDF + QR (RG 4892).**

**Fase 6 — Frontend mínimo:** form precargado con el cliente default (el flujo "1 click + monto" para la factura semanal), selector de cliente para los futuros, listado con estado/CAE, descarga de PDF. Incluye el empaquetado de §2.5: Docker Compose (o comando uvicorn), launcher con perfiles aislados, script de backup y chequeos de arranque (permisos de key, consistencia cert/ambiente, bind a localhost).

**Fase 7 — Checklist a producción:** certificado prod, asociación al servicio "Facturación Electrónica de Exportación" **y** al WSCDC (constatación), punto de venta RECE exclusivo de exportación, arranque en Producción (launcher o `--env prod`), smoke test con `FEXDummy`, primera factura real de monto chico y constatación del CAE desde el menú in-app (botón Constatar) o, si falla, el portal ARCA.

---

## 5. Referencias

- Manual desarrollador WSFEXv1 y WSDL: portal ARCA → `arca.gob.ar/ws/documentacion/ws-factura-electronica.asp` y `arca.gob.ar/fe/ayuda/webservice.asp`
- Especificación WSAA + manual: `afip.gob.ar/ws/documentacion/wsaa.asp`
- WSASS (certs de homologación): manual `arca.gob.ar/ws/WSASS/WSASS_manual.pdf`
- Referencias de implementación: `pyafipws` (Python, wiki WSFEX de reingart), `@ramiidv/arca-sdk` (TypeScript, WSFE + WSFEX), wiki SistemasAgiles `FacturaElectronicaExportacion`

## 6. Decisiones tomadas y preguntas restantes

**Resueltas:**

1. **Exportación de servicios** (`tipo_expo = 2`, `permiso_existente = ''`). No hay permisos de embarque ni complejidad aduanera. La fase 3 se simplifica: el caso de prueba canónico es exactamente el caso real de producción.
2. **Volumen: ~1 factura por semana, un solo CUIT emisor.** Implicancias de diseño:
   - No hay problema de escala ni de concurrencia real; SQLite alcanza incluso más allá del spike.
   - El TA del WSAA (12 h de vida) probablemente se pida fresco en cada emisión — el cache sigue siendo necesario para reintentos dentro de la misma sesión, pero no hace falta nada sofisticado.
   - El refresh del cache de parámetros puede ser lazy (al momento de emitir, si `fetched_at` > 24 h) en lugar de un job programado.
3. **Clientes: hoy 1, el modelo debe soportar N.** Se agrega entidad `clients` (ver §2.2). La factura referencia un cliente pero **snapshotea** sus datos al crear el borrador (razón social, domicilio, id impositivo, país, CUIT país): el comprobante queda inmutable aunque el cliente se edite después. El frontend precarga el cliente habitual como default. Lo mismo aplica al emisor (FAC-10): se guardan `emisor_id` (trazabilidad) y los campos de encabezado/CUIT en la fila de `invoices`; el PDF no relee la tabla `emisores`. FAC-52 completa el snapshot de render: descripciones resueltas de `arca_params` (país, CUIT país, moneda, U. Medida) y `pdf_render_version` viven en la factura/ítems; regenerar el PDF no consulta emisores, clients, settings ni el cache de params. Esos campos viven en el **baseline** (`schema.sql` / migración v1): no hay DBs desplegadas que requieran un ALTER/migración v2.
4. **Ambientes: perfiles aislados elegidos por launcher ([ADR 0001](adr/0001-perfiles-de-ambiente-aislados.md), implementado FAC-23 … FAC-34).** Un backend corre contra exactamente un ambiente inmutable y un perfil oculto (DB, certificados, caches, logs, onboarding y backups propios; un CUIT fiscal por perfil). Cambiar de ambiente reinicia el backend; el hot switching queda rechazado. Reemplaza la selección por `ARCA_ENV` en `.env` compartido y el ambiente por emisor.

**Siguen abiertas:**

5. ¿Nota de Crédito/Débito E desde el día 1? Con una factura semanal al mismo cliente, la NC E aparece recién el día que haya que anular/corregir una. Sugerencia: dejarla fuera del spike pero modelar `cbte_tipo` y comprobante asociado desde el inicio para que agregarla sea trivial.

**Cerrada:** stack Python de punta a punta (§2.1), cliente WSFEX propio, `pyafipws` solo como referencia con checklist de paridad de seguridad (§2.1.1) como definition of done del `ArcaClient`.

# Smoke test manual de Windows

Prueba del instalador de FacturadorE en una PC con Windows antes de usar una
versión nueva. CI (`.github/workflows/windows.yml`) ya cubre lo que no
necesita pantalla: build, arranque sin ventana de Homologación, `/health`,
segundo launch sin backend duplicado, `launcher.log`, `age` y Tcl/Tk dentro del
bundle, y el instalador en silencio (instalar, actualizar encima,
desinstalar sin tocar los datos). Esta lista cubre el resto: la descarga real
desde el navegador, chooser, ventana, cambio de ambiente, cierre y errores.

Tarda unos 15 minutos. Anotá el resultado en la tabla del final.

## Preparación: una cuenta de Windows limpia

Para probar "en limpio" sin tocar tus datos reales, usá una **cuenta local
nueva** de Windows (Configuración → Cuentas → Otros usuarios → Agregar
cuenta → "No tengo los datos de inicio de sesión de esta persona" →
"Agregar un usuario sin cuenta Microsoft"). Una cuenta nueva no tiene
`%LOCALAPPDATA%\FacturadorE`, así que la app arranca desde cero. Al terminar
podés borrar la cuenta.

En esa cuenta:

1. Descargar `FacturadorE-Setup-<versión>.exe` del Release **desde el
   navegador** (así Windows lo marca como venido de internet, igual que le
   va a pasar a cualquier usuario). Para probar un build sin Release, usar el
   artifact `FacturadorE-Setup-…` del job **Windows** de CI.
2. Abrir el instalador tal como se descargó, sin desbloquearlo. Si aparece
   SmartScreen: Más información → Ejecutar de todas formas. No tiene que
   pedir administrador. Destildar "Ejecutar FacturadorE" al final.
3. Verificar que **FacturadorE** aparece en el menú Inicio y en
   Configuración → Aplicaciones, y que se instaló en
   `%LOCALAPPDATA%\Programs\FacturadorE`.
4. Tener a mano el Administrador de tareas (Ctrl+Shift+Esc) en la pestaña
   **Detalles**, ordenado por nombre: ahí se cuentan los procesos
   `FacturadorE.exe`.

## Checklist

| # | Paso | Esperado |
|---|------|----------|
| 1 | Abrir **FacturadorE** desde el menú Inicio. | Aparece el selector con los colores de la app (ícono y wordmark arriba) y las tarjetas **Homologación** y **Producción**; la primera vez, Homologación preseleccionada. No se abre ninguna consola. |
| 2 | Elegir **Homologación**. | La misma ventana muestra "Iniciando Homologación" con la barra animada (nunca queda la pantalla sin ventana); después se abre la ventana **propia** "FacturadorE — Homologación" (no el navegador) en la configuración inicial (paso del certificado). Badge "Homologación — sin valor fiscal". Hay 2 procesos `FacturadorE.exe` (launcher + backend). |
| 3 | Con la app abierta, abrir FacturadorE otra vez desde el menú Inicio y elegir Homologación. | Se abre otra ventana de la **misma** sesión (mismo paso de setup). Hay 3 procesos (2 launchers + **1** backend). Cerrar esa segunda ventana: vuelven a ser 2. |
| 4 | En la primera ventana: **Cambiar ambiente** (header). | Aparece el selector con la app todavía abierta, con **Producción** preseleccionada y Homologación marcada "Actual". **Cancelar**: todo sigue igual. |
| 5 | **Cambiar ambiente** → **Producción**. | La primera vez pide confirmar la validez fiscal, con el foco en **Cancelar**. **Cancelar** (o Enter, o Esc): no abre Producción y vuelve la ventana de Homologación. |
| 6 | **Cambiar ambiente** → **Producción** → confirmar. | En la misma ventana, "Cambiando a Producción": primero "Cerrando Homologación…", después "✓ Homologación cerrada" e "Iniciando Producción…". Luego, ventana "FacturadorE — Producción" con badge de validez fiscal, en su propia configuración inicial (no comparte nada con Homologación). Siguen siendo 2 procesos. |
| 7 | Cerrar la ventana (X). | En unos segundos no queda **ningún** `FacturadorE.exe` en Detalles. |
| 8 | Abrir de nuevo, **Producción**. | El selector preselecciona **Producción** con "Último usado". No vuelve a pedir la confirmación de validez fiscal (quedó guardada). |
| 9 | Cerrar. Abrir, **Homologación**, cargar el certificado de homologación en el setup y seguir hasta donde se pueda; cerrar y volver a abrir. | Reabre en el mismo paso: el perfil persiste entre aperturas en `%LOCALAPPDATA%\FacturadorE`. |
| 10 | Menú **Diagnóstico** → **Verificar ARCA** (con el setup de homologación completo). | La conexión con ARCA y la numeración salen en verde; el resto, en verde o con avisos que explican qué hacer. |
| 11 | Error entendible: en PowerShell, ocupar el puerto con `$l = [System.Net.Sockets.TcpListener]::new([ipaddress]::Loopback, 8399); $l.Start()` y abrir FacturadorE → Homologación. | Una ventana de error con los colores de la app explica que el puerto 8399 lo usa otro programa (sin traceback). **Copiar detalle técnico** copia el mensaje, el ambiente y la versión. `$l.Stop()` y **Reintentar**: abre Homologación. |
| 12 | Revisar `%LOCALAPPDATA%\FacturadorE\launcher.log`. | Tiene las líneas "listo en http://127.0.0.1:8399/" de las aperturas. |
| 13 | Actualizar: con la app **abierta**, ejecutar el instalador de nuevo (el mismo o el de una versión más nueva). Después abrir FacturadorE. | El instalador cierra la app (no queda ningún `FacturadorE.exe` mientras instala) y termina sin errores. Al abrir, todo sigue como estaba: mismo paso de setup, mismo "Último usado". |
| 14 | Windows en tema oscuro (Configuración → Personalización → Colores → Oscuro) y abrir FacturadorE. | El selector, la confirmación de Producción, la ventana de error y el aviso del paso 17 salen en oscuro, con la barra de título oscura. |
| 15 | En el selector, solo con teclado: ↑/↓, Tab, Enter, Esc. | ↑/↓ cambian la tarjeta elegida; Tab recorre tarjetas, Cancelar y Abrir con el anillo de foco visible; Enter abre; Esc cancela. |
| 16 | Elegir **Homologación** y, mientras dice "Iniciando Homologación", **Cancelar**. | Dice "Cancelando…" y vuelve al selector. En Detalles queda un solo `FacturadorE.exe` (el del selector): el backend que se estaba levantando se cerró. |
| 17 | Aviso de navegador: con la app cerrada, renombrar `%LOCALAPPDATA%\Programs\FacturadorE\_internal\pythonnet` a `pythonnet.x` y abrir FacturadorE → Homologación. Al terminar, finalizar `FacturadorE.exe` desde el Administrador de tareas y devolverle el nombre a la carpeta. | En vez de la ventana de la app aparece un aviso amarillo con los colores de la app: "No se pudo abrir la ventana de FacturadorE". **Ver registro** abre `launcher.log`, **Copiar detalle técnico** copia el motivo, el ambiente y la versión, **Ver ayuda** abre el README; ninguno abre la app. **Abrir en el navegador** (o Enter, o Esc) cierra el aviso y abre la app en el navegador. Tab recorre los cuatro controles con el anillo de foco visible. |
| 18 | Desinstalar: cerrar la app y, en Configuración → Aplicaciones, desinstalar FacturadorE. | Desaparecen el acceso directo y `%LOCALAPPDATA%\Programs\FacturadorE`. `%LOCALAPPDATA%\FacturadorE` sigue con los perfiles y `launcher.log`. |

Si algún paso falla, anotá el mensaje del diálogo y adjuntá `launcher.log` y
el log del perfil (`%LOCALAPPDATA%\FacturadorE\<homo|prod>\data\logs\`)
al issue. Revisalos antes de compartirlos: no deberían tener secretos, pero
sí rutas de tu usuario.

## Resultados

| Fecha | Versión | Windows (build) | Resultado | Notas |
|-------|---------|-----------------|-----------|-------|
| | | | | |

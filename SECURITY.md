# Seguridad

FacturadorE corre solo en tu computadora y firma comprobantes fiscales con tu
certificado de ARCA. Un problema de seguridad puede terminar en un
comprobante emitido a tu nombre, así que los reportes se toman en serio.

## Cómo reportar una vulnerabilidad

**No abras un issue público.** Usá el reporte privado de GitHub: pestaña
**Security** del repo → **Report a vulnerability**. Solo el mantenedor lo ve.

Incluí, si podés:

- qué versión o commit usás y en qué sistema operativo;
- los pasos para reproducirlo y qué impacto tiene (p.ej. otra página o
  proceso local puede autorizar un comprobante, leer la clave, saltear la
  confirmación de Producción);
- una prueba de concepto **con datos de prueba**.

**Nunca mandes** certificados, claves privadas, identidades `age`, tokens del
WSAA, CUIT ni datos reales de comprobantes, ni en el reporte ni en adjuntos.
Si un problema solo se reproduce con datos reales, describilo y lo vemos.

Respuesta inicial dentro de 7 días. Es un proyecto personal: los plazos de
corrección dependen de la gravedad, y se avisa en el reporte.

## Alcance

Dentro del alcance: la app (`facturador/`), el launcher y el ejecutable de
Windows publicado en los Releases, los workflows de CI y el backup cifrado.

Fuera del alcance: los webservices de ARCA, la seguridad general de la
computadora donde corre la app, y versiones distintas del último Release.

## Versiones soportadas

Solo el último Release recibe correcciones de seguridad.

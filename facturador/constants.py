"""Constantes de dominio compartidas entre módulos.

Acá viven solo los valores que el spike fija por diseño (§0.1) y que se
usan desde más de un módulo. Los códigos de comprobante/expo/unidad son de
las tablas dinámicas de ARCA (consultables vía /params/:kind); si ARCA los
cambiara, el lugar de la verdad es la tabla, no este archivo.
"""

# SOAP 1.1 (compartido por WSAA y WSFEX)
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# Tipos de comprobante de exportación (FEXGetPARAM_Cbte_Tipo)
CBTE_TIPO_FACTURA_E = 19
CBTE_TIPO_NOTA_DEBITO_E = 20   # fuera del spike; modelado desde el día 1 (§6.4)
CBTE_TIPO_NOTA_CREDITO_E = 21

# Tipos de exportación (FEXGetPARAM_Tipo_Expo)
TIPO_EXPO_BIENES = 1
TIPO_EXPO_SERVICIOS = 2
TIPO_EXPO_OTROS = 4

# Unidad de medida del caso canónico: 7 = "unidades" (FEXGetPARAM_UMed)
UMED_UNIDADES = 7

# Moneda del caso canónico (FEXGetPARAM_MON)
MONEDA_DOL = "DOL"

# QR RG 4892
QR_BASE_URL = "https://www.afip.gob.ar/fe/qr/"
TIPO_DOC_CUIT = 80  # tabla de tipos de documento de ARCA

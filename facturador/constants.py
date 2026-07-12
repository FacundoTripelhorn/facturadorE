"""Constantes de dominio compartidas entre módulos.

Acá viven solo los valores que design.md fija por diseño (§0.1) y que se
usan desde más de un módulo. Los códigos de comprobante/expo/unidad son de
las tablas dinámicas de ARCA (consultables vía /params/:kind); si ARCA los
cambiara, el lugar de la verdad es la tabla, no este archivo.
"""

import enum


class ArcaEnvironment(enum.StrEnum):
    """Ambiente fiscal ARCA (ADR 0001).

    StrEnum: el valor TEXT entra sin conversión en ``invoices.environment``
    y en el JSON de la API. Un perfil aislado corresponde a exactamente uno.
    """

    HOMO = "homo"
    PROD = "prod"


class InvoiceStatus(enum.StrEnum):
    """Estados de la máquina de estados de facturas (design.md §2.2).

    Es un StrEnum: cada miembro ES su valor TEXT, así que entra sin
    conversión en la columna ``invoices.status`` y en el JSON de la API.
    El CHECK de schema.sql (baseline v1) enumera los mismos valores; un test los mantiene
    en sync.
    """

    DRAFT = "draft"
    SUBMITTING = "submitting"
    AUTHORIZED = "authorized"
    REJECTED = "rejected"
    UNKNOWN = "unknown"  # timeout post-envío, pendiente de reconciliar

# Endpoints de ARCA por ambiente (design.md §1.3). NO son configurables por
# separado: Config los deriva del único flag ARCA_ENV (checklist §2.1.1
# punto 1), imposible mezclar cert de homo con URL de prod por construcción.
WSAA_URLS = {
    "homo": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "prod": "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}

WSFEX_URLS = {
    "homo": "https://wswhomo.afip.gov.ar/wsfexv1/service.asmx",
    "prod": "https://servicios1.afip.gov.ar/wsfexv1/service.asmx",
}

# SOAP 1.1 (compartido por WSAA y WSFEX)
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# Tipos de comprobante de exportación (FEXGetPARAM_Cbte_Tipo)
CBTE_TIPO_FACTURA_E = 19
CBTE_TIPO_NOTA_DEBITO_E = 20   # fuera de alcance hoy; modelado desde el día 1 (§6.4)
CBTE_TIPO_NOTA_CREDITO_E = 21

# Tipos de exportación (FEXGetPARAM_Tipo_Expo)
TIPO_EXPO_BIENES = 1
TIPO_EXPO_SERVICIOS = 2
TIPO_EXPO_OTROS = 4

# Unidad de medida del caso canónico: 7 = "unidades" (FEXGetPARAM_UMed)
UMED_UNIDADES = 7

# Moneda del caso canónico (FEXGetPARAM_MON)
MONEDA_DOL = "DOL"

# Códigos de moneda de ARCA → código ISO para mostrar. Solo presentación:
# hacia ARCA siempre viaja el código de la tabla (DOL, PES, ...).
MONEDA_DISPLAY = {"DOL": "USD", "PES": "ARS"}

# QR RG 4892
QR_BASE_URL = "https://www.afip.gob.ar/fe/qr/"
TIPO_DOC_CUIT = 80  # tabla de tipos de documento de ARCA

# Puerto local por defecto (siempre bind 127.0.0.1 en host; design.md §2.5).
# Una sola fuente para backend, launcher CLI y plan de arranque.
DEFAULT_PORT = 8399

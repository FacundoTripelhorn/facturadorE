"""Esquemas Pydantic del contrato REST (design.md §2.3)."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .arca.amounts import (
    IMP_TOTAL,
    MONEDA_CTZ,
    PRO_PRECIO_UNI,
    PRO_QTY,
    validar_importe,
)
from .constants import MONEDA_DOL, UMED_UNIDADES
from .settings import BACKUP_PREFIX_DEFAULT, CONDICION_IVA_DEFAULT

_FECHA_RE = re.compile(r"^\d{8}$")


def _validar_fecha(value: str, campo: str) -> str:
    if not _FECHA_RE.match(value):
        raise ValueError(f"{campo} debe ser AAAAMMDD")
    dt.datetime.strptime(value, "%Y%m%d")  # fecha real, no 20261399
    return value


class EmisorCreateIn(BaseModel):
    """Alta de un emisor dentro del perfil activo."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    razon_social: str = Field(min_length=1, max_length=200)
    domicilio: str = Field(min_length=1, max_length=200)
    iibb: str = Field(min_length=1, max_length=50)
    inicio_actividades: str = Field(min_length=1, max_length=20)
    condicion_iva: str = CONDICION_IVA_DEFAULT
    puntos_venta: list[int] = Field(default=[1], min_length=1)

    @field_validator("puntos_venta")
    @classmethod
    def _puntos_venta_positivos(cls, valores: list[int]) -> list[int]:
        if any(pv < 1 for pv in valores):
            raise ValueError("los puntos de venta deben ser >= 1")
        return valores


class EmisorUpdateIn(BaseModel):
    """Edición de un emisor existente dentro del perfil activo."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    razon_social: str = Field(min_length=1, max_length=200)
    domicilio: str = Field(min_length=1, max_length=200)
    iibb: str = Field(min_length=1, max_length=50)
    inicio_actividades: str = Field(min_length=1, max_length=20)
    condicion_iva: str = CONDICION_IVA_DEFAULT
    puntos_venta: list[int] = Field(default=[1], min_length=1)

    @field_validator("puntos_venta")
    @classmethod
    def _puntos_venta_positivos(cls, valores: list[int]) -> list[int]:
        if any(pv < 1 for pv in valores):
            raise ValueError("los puntos de venta deben ser >= 1")
        return valores


class BackupSettingsIn(BaseModel):
    """Config global de backups (no depende del emisor)."""

    backup_s3_bucket: str = ""
    backup_s3_prefix: str = BACKUP_PREFIX_DEFAULT


class SeedBackupStateOut(BaseModel):
    """Estado queryable del seed backup."""

    status: str
    last_success_at: str | None = None
    last_attempt_at: str | None = None
    last_error: str | None = None
    pending_reason: str | None = None
    needs_retry: bool = False


class ClientIn(BaseModel):
    razon_social: str = Field(min_length=1, max_length=200)
    domicilio: str = ""
    pais_dst: int
    cuit_pais: int
    id_impositivo: str = ""
    moneda_default: str = MONEDA_DOL
    incoterms_default: str = ""
    idioma_default: int = 1
    forma_pago_default: str = "WIRE TRANSFER"
    descripcion_default: str = ""
    is_default: bool = False


class ClientOut(ClientIn):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: str
    updated_at: str


# Los importes aceptan NaN/Infinity en el parseo para que el rechazo salga
# del validador, con mensaje en castellano, y no del genérico de Pydantic.
class ItemIn(BaseModel):
    pro_ds: str = Field(min_length=1)
    pro_precio_uni: Decimal = Field(allow_inf_nan=True)
    pro_codigo: str = "0001"
    pro_qty: Decimal = Field(default=Decimal(1), allow_inf_nan=True)
    pro_umed: int = UMED_UNIDADES

    @field_validator("pro_precio_uni")
    @classmethod
    def _precio(cls, valor: Decimal) -> Decimal:
        return validar_importe(valor, PRO_PRECIO_UNI)

    @field_validator("pro_qty")
    @classmethod
    def _cantidad(cls, valor: Decimal) -> Decimal:
        return validar_importe(valor, PRO_QTY)


class InvoiceCreate(BaseModel):
    """Caso feliz semanal (§0.1): monto + fecha de pago + descripción.
    Todo lo demás sale del cliente (default si no se indica client_id)."""

    imp_total: Decimal = Field(allow_inf_nan=True)
    punto_venta: int | None = None   # obligatorio si el emisor tiene varios PV
    client_id: str | None = None
    descripcion: str | None = None      # default: descripcion_default del cliente
    fecha_cbte: str | None = None       # default: hoy (día del cobro)
    fecha_pago: str | None = None       # default: fecha_cbte, siempre editable
    moneda_id: str | None = None        # default: moneda_default del cliente
    # default: cotización ARCA del día
    moneda_ctz: Decimal | None = Field(default=None, allow_inf_nan=True)
    obs: str = ""
    items: list[ItemIn] | None = None   # default: 1 ítem qty=1, precio=imp_total

    @field_validator("fecha_cbte", "fecha_pago")
    @classmethod
    def _fechas(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validar_fecha(value, info.field_name)

    @field_validator("imp_total")
    @classmethod
    def _importe(cls, valor: Decimal) -> Decimal:
        return validar_importe(valor, IMP_TOTAL)

    @field_validator("moneda_ctz")
    @classmethod
    def _cotizacion(cls, valor: Decimal | None) -> Decimal | None:
        if valor is None:
            return None
        return validar_importe(valor, MONEDA_CTZ)

    @field_validator("punto_venta")
    @classmethod
    def _punto_venta_positivo(cls, valor: int | None) -> int | None:
        if valor is not None and valor < 1:
            raise ValueError("punto_venta debe ser >= 1")
        return valor


class ItemOut(BaseModel):
    pro_codigo: str
    pro_ds: str
    pro_qty: str
    pro_umed: int
    pro_umed_ds: str = ""
    pro_precio_uni: str
    pro_total_item: str


class InvoiceOut(BaseModel):
    id: str
    emisor_id: str | None
    client_id: str | None
    arca_id: int | None
    cbte_tipo: int
    punto_venta: int
    cbte_nro: int | None
    status: str
    source: str = "wsfex"
    fecha_cbte: str
    fecha_pago: str
    tipo_expo: int
    dst_cmp: int
    dst_cmp_ds: str = ""
    cliente: str
    cuit_pais_cliente: int
    cuit_pais_cliente_ds: str = ""
    domicilio_cliente: str
    id_impositivo: str
    moneda_id: str
    moneda_ds: str = ""
    moneda_ctz: str
    incoterms: str
    forma_pago: str
    idioma_cbte: int
    imp_total: str
    obs: str
    cae: str | None
    cae_fch_vto: str | None
    last_error: str | None
    cuit_emisor: str | None = None
    # Snapshot del emisor al crear el borrador; no se relee emisores.
    emisor_razon_social: str = ""
    emisor_domicilio: str = ""
    emisor_condicion_iva: str = ""
    emisor_iibb: str = ""
    emisor_inicio_actividades: str = ""
    # Contrato de render PDF; no se relee arca_params al regenerar.
    pdf_render_version: int = 1
    environment: str
    created_at: str
    updated_at: str
    items: list[ItemOut] = []


class ArcaInvoiceItemOut(BaseModel):
    """Ítem tal como lo devuelve FEXGetCMP (peek del registro ARCA)."""

    pro_codigo: str = ""
    pro_ds: str = ""
    pro_qty: str = ""
    pro_umed: str = ""
    pro_precio_uni: str = ""
    pro_total_item: str = ""
    pro_bonificacion: str = "0"


class ArcaInvoiceOut(BaseModel):
    """Comprobante leído en vivo desde ARCA (no es el registro local)."""

    cbte_tipo: int
    punto_venta: int
    cbte_nro: int
    arca_id: int | None = None
    cae: str | None = None
    cae_fch_vto: str | None = None
    fecha_cbte: str | None = None
    fecha_pago: str | None = None
    tipo_expo: int | None = None
    dst_cmp: int | None = None
    cliente: str | None = None
    cuit_pais_cliente: int | None = None
    domicilio_cliente: str | None = None
    id_impositivo: str | None = None
    moneda_id: str | None = None
    moneda_ctz: str | None = None
    imp_total: str | None = None
    forma_pago: str | None = None
    incoterms: str | None = None
    idioma_cbte: int | None = None
    obs: str | None = None
    items: list[ArcaInvoiceItemOut] = []


class ArcaInvoicesOut(BaseModel):
    """Peek del registro ARCA para un (PV, tipo)."""

    punto_venta: int
    cbte_tipo: int
    last_cmp: int
    invoices: list[ArcaInvoiceOut]
    gaps: list[int] = []


class ParamOut(BaseModel):
    code: str
    description: str | None
    valid_from: str | None
    valid_to: str | None


class RateOut(BaseModel):
    moneda_id: str
    fecha: str
    cotizacion: str


class HealthOut(BaseModel):
    environment: str
    appserver: str
    dbserver: str
    authserver: str

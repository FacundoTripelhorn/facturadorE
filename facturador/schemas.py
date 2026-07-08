"""Esquemas Pydantic del contrato REST (design.md §2.3)."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import MONEDA_DOL, UMED_UNIDADES
from .settings import BACKUP_PREFIX_DEFAULT, CONDICION_IVA_DEFAULT

_FECHA_RE = re.compile(r"^\d{8}$")


def _validar_fecha(value: str, campo: str) -> str:
    if not _FECHA_RE.match(value):
        raise ValueError(f"{campo} debe ser AAAAMMDD")
    dt.datetime.strptime(value, "%Y%m%d")  # fecha real, no 20261399
    return value


class SettingsIn(BaseModel):
    """Configuración de dominio editable desde la UI (vive en la DB).
    El bloque del emisor es el del emisor que factura contra el ambiente
    activo (el ambiente lo declara el emisor, no este payload)."""

    emisor_razon_social: str = Field(min_length=1, max_length=200)
    emisor_domicilio: str = Field(min_length=1, max_length=200)
    # Literal del comprobante, p.ej. "Exento" o el nro de inscripción.
    emisor_iibb: str = Field(min_length=1, max_length=50)
    # DD/MM/AAAA, como lo imprime el comprobante.
    emisor_inicio_actividades: str = Field(min_length=1, max_length=20)
    emisor_condicion_iva: str = CONDICION_IVA_DEFAULT
    # Puntos de venta habilitados del emisor; se emite con el primero.
    puntos_venta: list[int] = Field(default=[1], min_length=1)
    backup_s3_bucket: str = ""
    backup_s3_prefix: str = BACKUP_PREFIX_DEFAULT

    @field_validator("puntos_venta")
    @classmethod
    def _puntos_venta_positivos(cls, valores: list[int]) -> list[int]:
        if any(pv < 1 for pv in valores):
            raise ValueError("los puntos de venta deben ser >= 1")
        return valores


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


class ItemIn(BaseModel):
    pro_ds: str = Field(min_length=1)
    pro_precio_uni: Decimal = Field(gt=0)
    pro_codigo: str = "0001"
    pro_qty: Decimal = Field(default=Decimal(1), gt=0)
    pro_umed: int = UMED_UNIDADES


class InvoiceCreate(BaseModel):
    """Caso feliz semanal (§0.1): monto + fecha de pago + descripción.
    Todo lo demás sale del cliente (default si no se indica client_id)."""

    imp_total: Decimal = Field(gt=0)
    client_id: str | None = None
    descripcion: str | None = None      # default: descripcion_default del cliente
    fecha_cbte: str | None = None       # default: hoy (día del cobro)
    fecha_pago: str | None = None       # default: fecha_cbte, siempre editable
    moneda_id: str | None = None        # default: moneda_default del cliente
    moneda_ctz: Decimal | None = None   # default: cotización ARCA del día
    obs: str = ""
    items: list[ItemIn] | None = None   # default: 1 ítem qty=1, precio=imp_total

    @field_validator("fecha_cbte", "fecha_pago")
    @classmethod
    def _fechas(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validar_fecha(value, info.field_name)


class ItemOut(BaseModel):
    pro_codigo: str
    pro_ds: str
    pro_qty: str
    pro_umed: int
    pro_precio_uni: str
    pro_total_item: str


class InvoiceOut(BaseModel):
    id: str
    client_id: str | None
    arca_id: int | None
    cbte_tipo: int
    punto_venta: int
    cbte_nro: int | None
    status: str
    fecha_cbte: str
    fecha_pago: str
    tipo_expo: int
    dst_cmp: int
    cliente: str
    cuit_pais_cliente: int
    domicilio_cliente: str
    id_impositivo: str
    moneda_id: str
    moneda_ctz: str
    incoterms: str
    forma_pago: str
    idioma_cbte: int
    imp_total: str
    obs: str
    cae: str | None
    cae_fch_vto: str | None
    last_error: str | None
    environment: str
    created_at: str
    updated_at: str
    items: list[ItemOut] = []


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

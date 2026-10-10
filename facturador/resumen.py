"""Resumen de facturación del año: totales por mes, por cliente y por moneda.

Es una vista de control personal. Cuenta solo comprobantes autorizados y
suma cada importe en su moneda original: nunca se suman importes de monedas
distintas ni se convierte a pesos.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

MESES = (
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
)


class Comprobante(Protocol):
    """Fila con columnas por nombre: ``sqlite3.Row`` o un ``dict``."""

    def __getitem__(self, key: str, /) -> Any: ...


@dataclass
class Total:
    """Importe acumulado en una sola moneda y cuántos comprobantes lo forman."""

    importe: Decimal = Decimal(0)
    cantidad: int = 0

    def sumar(self, importe: Decimal) -> None:
        self.importe += importe
        self.cantidad += 1


@dataclass
class FilaCliente:
    cliente: str
    moneda: str
    total: Total = field(default_factory=Total)


@dataclass
class ResumenMoneda:
    """Totales de una moneda: los 12 meses (enero primero) y el año."""

    moneda: str
    por_mes: list[Total] = field(default_factory=lambda: [Total() for _ in MESES])
    anual: Total = field(default_factory=Total)


@dataclass
class Resumen:
    anio: int
    # Monedas en orden de columna: la más usada primero.
    monedas: list[ResumenMoneda]
    # Por total descendente dentro de cada moneda, en el orden de ``monedas``.
    por_cliente: list[FilaCliente]

    @property
    def vacio(self) -> bool:
        return not self.monedas


def resumir(anio: int, comprobantes: Iterable[Comprobante]) -> Resumen:
    """Agrupa comprobantes autorizados del año.

    Cada comprobante trae ``fecha_cbte`` (AAAAMMDD), ``moneda_id``,
    ``imp_total`` (texto decimal), ``cliente`` y ``client_id``. El llamador
    filtra estado, ambiente y año; acá solo se suma.
    """
    monedas: dict[str, ResumenMoneda] = {}
    clientes: dict[tuple[str, str], FilaCliente] = {}
    for cbte in comprobantes:
        moneda = cbte["moneda_id"]
        importe = Decimal(cbte["imp_total"])
        mes = int(cbte["fecha_cbte"][4:6])
        resumen = monedas.setdefault(moneda, ResumenMoneda(moneda))
        resumen.por_mes[mes - 1].sumar(importe)
        resumen.anual.sumar(importe)
        # Un cliente renombrado sigue siendo el mismo si tiene id; los
        # comprobantes sin id (reconstruidos desde ARCA) se agrupan por nombre.
        clave = (cbte["client_id"] or f"nombre:{cbte['cliente']}", moneda)
        fila = clientes.setdefault(clave, FilaCliente(cbte["cliente"], moneda))
        # Vienen ordenados por fecha: queda el nombre del más reciente.
        fila.cliente = cbte["cliente"]
        fila.total.sumar(importe)

    ordenadas = sorted(
        monedas.values(), key=lambda m: (-m.anual.cantidad, m.moneda)
    )
    posicion = {m.moneda: i for i, m in enumerate(ordenadas)}
    por_cliente = sorted(
        clientes.values(),
        key=lambda f: (posicion[f.moneda], -f.total.importe, f.cliente),
    )
    return Resumen(anio=anio, monedas=ordenadas, por_cliente=por_cliente)

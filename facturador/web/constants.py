"""Valores fijos de la UI web: clases de badge, estados que muestra la
pantalla de backups y claves de los avisos/errores que viajan en la URL.

Son StrEnum: cada miembro ES su texto, así que entra igual en un
f-string de URL, en una plantilla o como clave de un dict que después se
consulta con el string que llega en el query param.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class Badge(StrEnum):
    """Clase CSS de un ``.badge`` (ver ``static/app.css``)."""

    OK = "ok"
    ERROR = "error"
    AVISO = "aviso"
    NEUTRO = "neutro"


class EstadoBackupUI(Enum):
    """Cómo se muestra el estado del backup: badge y título."""

    AL_DIA = (Badge.OK, "Al día")
    ERROR = (Badge.ERROR, "Error")
    PENDIENTE = (Badge.AVISO, "Pendiente")
    SIN_BACKUP = (Badge.NEUTRO, "Sin backup")

    def __init__(self, badge: Badge, titulo: str) -> None:
        self.badge = badge
        self.titulo = titulo


class AvisoConfiguracion(StrEnum):
    """``/configuracion?aviso=<clave>`` después de una acción que salió bien."""

    GUARDADO = "guardado"
    BACKUP = "backup"
    ACTIVADO = "activado"
    IDENTIDAD = "identidad"
    MAQUINA = "maquina"
    MAQUINA_QUITADA = "maquina_quitada"
    BACKUP_HECHO = "backup_hecho"


class ErrorConfiguracion(StrEnum):
    """``/configuracion?error=<clave>`` cuando la acción falló después del
    redirect (el detalle queda en el log, no en la URL)."""

    BACKUP = "backup"


class TabComprobantes(StrEnum):
    """Solapas de ``/comprobantes`` (``?estado=<valor>``)."""

    TODAS = "todas"
    BORRADORES = "borradores"
    AUTORIZADAS = "autorizadas"
    ATENCION = "atencion"

    @classmethod
    def desde_query(cls, valor: str) -> TabComprobantes:
        """La solapa pedida; una desconocida (link viejo o tipeado) es TODAS."""
        try:
            return cls(valor)
        except ValueError:
            return cls.TODAS


class AvisoSetup(StrEnum):
    """``/setup?aviso=<valor>``: qué paso del onboarding se acaba de completar."""

    INSTALADO = "instalado"
    EMISOR = "emisor"
    PUNTO_VENTA = "punto_venta"
    LISTO = "listo"

    @classmethod
    def desde_query(cls, valor: str) -> AvisoSetup | None:
        """El aviso pedido, o None. Un texto cualquiera en la URL no se
        muestra: un link armado no puede poner mensajes en la app."""
        try:
            return cls(valor)
        except ValueError:
            return None

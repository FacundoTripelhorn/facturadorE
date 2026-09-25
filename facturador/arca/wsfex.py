"""Cliente WSFEXv1 (servicio ASMX de ARCA para Factura E de exportación).

Requests XML armados con ElementTree (nunca f-strings para valores —
checklist §2.1.1 punto 4). Cada llamada autenticada lleva Auth{Token, Sign,
Cuit} con el TA provisto por WsaaClient.

Manejo de respuestas (§1.5):
  - FEXErr con ErrCode != 0 => error de negocio, se levanta WsfexError.
  - FEXEvents => warnings de ARCA (mantenimientos, cambios normativos):
    se loguean SIEMPRE y se acumulan, nunca se tratan como error.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from ..config import Config
from ..constants import (
    CBTE_TIPO_FACTURA_E,
    MONEDA_DOL,
    SOAP_ENV_NS,
    TIPO_EXPO_SERVICIOS,
    UMED_UNIDADES,
)
from .tls import wsfex_verify
from .wsaa import WsaaClient, cuit_from_certificate

logger = logging.getLogger(__name__)

# Namespace verificado contra el WSDL real (2026-07): es minúscula, no FEXV1.
FEX_NS = "http://ar.gov.afip.dif.fexv1/"


class WsfexError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"WSFEX error {code}: {message}")
        self.code = code
        self.message = message


# docs/wsfex-gap-vs-error.md: FEXGetCMP ErrCode when the requested
# (tipo, PV, nro) is not on ARCA's ledger. Distinct from transport/SOAP faults
# and from other business ErrCodes (which abort a rebuild after retries).
CMP_NOT_FOUND_CODES = frozenset({"1521"})


class CmpNotFoundError(WsfexError):
    """ARCA confirmed the comprobante does not exist (known gap, not a fault)."""


@dataclass(frozen=True)
class CmpItem:
    """Línea de ítem devuelta por FEXGetCMP (rebuild)."""

    pro_codigo: str
    pro_ds: str
    pro_qty: str
    pro_umed: str
    pro_precio_uni: str
    pro_total_item: str
    pro_bonificacion: str = "0"


@dataclass(frozen=True)
class CmpRecord:
    """Respuesta estructurada de FEXGetCMP: campos planos + ítems anidados."""

    fields: dict[str, str]
    items: tuple[CmpItem, ...]


@dataclass(frozen=True)
class ParamRecord:
    code: str
    description: str | None
    valid_from: str | None
    valid_to: str | None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "description": self.description,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }


# kind -> método SOAP. El parseo de los ítems es genérico por sufijo de tag
# (_Id/_Codigo/_CUIT => código, _Ds => descripción, _vig_* => vigencia), lo
# que tolera diferencias menores de nombres entre tablas del WSDL.
PARAM_METHODS = {
    "moneda": "FEXGetPARAM_MON",
    "pais": "FEXGetPARAM_DST_pais",
    "cuit_pais": "FEXGetPARAM_DST_CUIT",
    "cbte_tipo": "FEXGetPARAM_Cbte_Tipo",
    "umed": "FEXGetPARAM_UMed",
    "incoterms": "FEXGetPARAM_Incoterms",
    "idioma": "FEXGetPARAM_Idiomas",
    "tipo_expo": "FEXGetPARAM_Tipo_Expo",
}

_CODE_SUFFIXES = ("_Id", "_Codigo", "_CUIT")
_DESC_SUFFIX = "_Ds"
_VIG_DESDE_SUFFIX = "_vig_desde"
_VIG_HASTA_SUFFIX = "_vig_hasta"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _findtext_local(root: ET.Element, name: str) -> str | None:
    for elem in root.iter():
        if _local(elem.tag) == name:
            return (elem.text or "").strip() or None
    return None


@dataclass(frozen=True)
class InvoiceItem:
    pro_ds: str
    pro_precio_uni: Decimal
    pro_codigo: str = "0001"
    pro_qty: Decimal = Decimal(1)
    pro_umed: int = UMED_UNIDADES
    pro_bonificacion: Decimal = Decimal(0)

    @property
    def pro_total_item(self) -> Decimal:
        return self.pro_qty * self.pro_precio_uni - self.pro_bonificacion


@dataclass(frozen=True)
class Invoice:
    """Factura E de exportación de servicios (caso canónico de design.md §0.1)."""

    arca_id: int              # Id idempotente de FEXAuthorize
    fecha_cbte: str           # AAAAMMDD
    punto_vta: int
    cbte_nro: int
    dst_cmp: int              # país destino (código ARCA)
    cliente: str
    cuit_pais_cliente: int
    domicilio_cliente: str
    id_impositivo: str
    moneda_ctz: Decimal
    imp_total: Decimal
    fecha_pago: str           # AAAAMMDD; puede diferir de fecha_cbte
    forma_pago: str
    items: list[InvoiceItem]
    cbte_tipo: int = CBTE_TIPO_FACTURA_E
    tipo_expo: int = TIPO_EXPO_SERVICIOS
    permiso_existente: str = ""   # vacío para servicios
    moneda_id: str = MONEDA_DOL
    incoterms: str = ""       # vacío en servicios (§0.1)
    idioma_cbte: int = 1      # español
    obs: str = ""

    def validate(self) -> None:
        total_items = sum(i.pro_total_item for i in self.items)
        if total_items != self.imp_total:
            raise ValueError(
                f"Imp_total {self.imp_total} != suma de items {total_items}"
            )
        if not self.items:
            raise ValueError("La factura necesita al menos un ítem")


@dataclass(frozen=True)
class AuthResult:
    cae: str
    cae_fch_vto: str
    arca_id: int
    cbte_nro: int
    cbte_tipo: int
    punto_vta: int
    fecha_cbte: str
    reproceso: str            # 'S' si ARCA devolvió un CAE ya emitido
    motivos_obs: str | None
    events: list[tuple[str, str]] = field(default_factory=list)


def _dec(value: Decimal) -> str:
    return format(value, "f")


def _add_child(parent: ET.Element, tag: str, value) -> None:
    ET.SubElement(parent, f"{{{FEX_NS}}}{tag}").text = str(value)


def build_cmp_element(invoice: Invoice) -> ET.Element:
    """Arma el <Cmp> de FEXAuthorize en el ORDEN del WSDL real (secuencia
    estricta del ASMX, verificada 2026-07). Solo serializador: los textos
    libres (cliente, domicilio, descripciones) quedan escapados por ET."""
    invoice.validate()
    cmp_el = ET.Element(f"{{{FEX_NS}}}Cmp")

    def add(tag: str, value) -> None:
        _add_child(cmp_el, tag, value)

    add("Id", invoice.arca_id)
    add("Fecha_cbte", invoice.fecha_cbte)
    add("Cbte_Tipo", invoice.cbte_tipo)
    add("Punto_vta", invoice.punto_vta)
    add("Cbte_nro", invoice.cbte_nro)
    add("Tipo_expo", invoice.tipo_expo)
    add("Permiso_existente", invoice.permiso_existente)
    add("Dst_cmp", invoice.dst_cmp)
    add("Cliente", invoice.cliente)
    add("Cuit_pais_cliente", invoice.cuit_pais_cliente)
    add("Domicilio_cliente", invoice.domicilio_cliente)
    add("Id_impositivo", invoice.id_impositivo)
    add("Moneda_Id", invoice.moneda_id)
    add("Moneda_ctz", _dec(invoice.moneda_ctz))
    add("Imp_total", _dec(invoice.imp_total))
    if invoice.obs:
        add("Obs", invoice.obs)
    add("Forma_pago", invoice.forma_pago)
    add("Incoterms", invoice.incoterms)
    add("Idioma_cbte", invoice.idioma_cbte)
    items_el = ET.SubElement(cmp_el, f"{{{FEX_NS}}}Items")
    for item in invoice.items:
        item_el = ET.SubElement(items_el, f"{{{FEX_NS}}}Item")
        _add_child(item_el, "Pro_codigo", item.pro_codigo)
        _add_child(item_el, "Pro_ds", item.pro_ds)
        _add_child(item_el, "Pro_qty", _dec(item.pro_qty))
        _add_child(item_el, "Pro_umed", item.pro_umed)
        _add_child(item_el, "Pro_precio_uni", _dec(item.pro_precio_uni))
        _add_child(item_el, "Pro_bonificacion", _dec(item.pro_bonificacion))
        _add_child(item_el, "Pro_total_item", _dec(item.pro_total_item))
    add("Fecha_pago", invoice.fecha_pago)
    return cmp_el


def parse_auth_result(result: ET.Element) -> AuthResult:
    cae = _findtext_local(result, "Cae")
    if not cae:
        raise WsfexError("?", "FEXAuthorize sin CAE en la respuesta")
    return AuthResult(
        cae=cae,
        cae_fch_vto=_findtext_local(result, "Fch_venc_Cae") or "",
        arca_id=int(_findtext_local(result, "Id") or 0),
        cbte_nro=int(_findtext_local(result, "Cbte_nro") or 0),
        cbte_tipo=int(_findtext_local(result, "Cbte_tipo")
                      or _findtext_local(result, "Cbte_Tipo") or 0),
        punto_vta=int(_findtext_local(result, "Punto_vta") or 0),
        fecha_cbte=_findtext_local(result, "Fecha_cbte") or "",
        reproceso=_findtext_local(result, "Reproceso") or "N",
        motivos_obs=_findtext_local(result, "Motivos_Obs"),
    )


def parse_cmp_record(result: ET.Element) -> CmpRecord:
    """Parsea ``FEXGetCMPResult`` en campos planos + ítems.

    Los tags bajo ``Items/Item`` no se aplastan en ``fields`` (evitarían
    colisiones entre líneas). El resto de hojas de ``FEXResultGet`` sí.
    """
    result_get: ET.Element | None = None
    for elem in result.iter():
        if _local(elem.tag) == "FEXResultGet":
            result_get = elem
            break
    if result_get is None:
        raise WsfexError("?", "FEXGetCMP sin FEXResultGet")

    fields: dict[str, str] = {}
    items: list[CmpItem] = []
    for child in list(result_get):
        name = _local(child.tag)
        if name == "Items":
            for item_el in child:
                if _local(item_el.tag) != "Item":
                    continue
                leaf: dict[str, str] = {}
                for leaf_el in item_el:
                    text = (leaf_el.text or "").strip()
                    if text:
                        leaf[_local(leaf_el.tag)] = text
                items.append(
                    CmpItem(
                        pro_codigo=leaf.get("Pro_codigo", "0001"),
                        pro_ds=leaf.get("Pro_ds", ""),
                        pro_qty=leaf.get("Pro_qty", "1"),
                        pro_umed=leaf.get("Pro_umed", str(UMED_UNIDADES)),
                        pro_precio_uni=leaf.get("Pro_precio_uni", "0"),
                        pro_total_item=leaf.get("Pro_total_item", "0"),
                        pro_bonificacion=leaf.get("Pro_bonificacion", "0"),
                    )
                )
            continue
        # Hojas directas y subárboles no-Items: tomar texto de hojas.
        if len(child) == 0:
            text = (child.text or "").strip()
            if text:
                fields[name] = text
            continue
        for leaf_el in child.iter():
            if leaf_el is child:
                continue
            if len(leaf_el) == 0:
                text = (leaf_el.text or "").strip()
                if text:
                    fields[_local(leaf_el.tag)] = text
    return CmpRecord(fields=fields, items=tuple(items))


def _match_request(auth_result: AuthResult, invoice: Invoice) -> list[str]:
    """Discrepancias respuesta vs request; nunca aceptar un CAE ajeno."""
    mismatches = []
    for campo, esperado, recibido in (
        ("Id", invoice.arca_id, auth_result.arca_id),
        ("Cbte_nro", invoice.cbte_nro, auth_result.cbte_nro),
        ("Punto_vta", invoice.punto_vta, auth_result.punto_vta),
        ("Cbte_Tipo", invoice.cbte_tipo, auth_result.cbte_tipo),
    ):
        if recibido and recibido != esperado:
            mismatches.append(f"{campo}: enviado {esperado}, recibido {recibido}")
    return mismatches


def parse_param_items(result: ET.Element) -> list[ParamRecord]:
    """Extrae los registros de un FEXResultGet de forma genérica."""
    records: list[ParamRecord] = []
    result_get = None
    for elem in result.iter():
        if _local(elem.tag) == "FEXResultGet":
            result_get = elem
            break
    if result_get is None:
        return records
    for item in list(result_get):
        code = desc = vig_desde = vig_hasta = None
        for child in item:
            tag = _local(child.tag)
            text = (child.text or "").strip() or None
            if tag.endswith(_VIG_DESDE_SUFFIX):
                vig_desde = text
            elif tag.endswith(_VIG_HASTA_SUFFIX):
                vig_hasta = text
            elif tag.endswith(_DESC_SUFFIX):
                desc = text
            elif tag.endswith(_CODE_SUFFIXES) and code is None:
                code = text
        if code is not None:
            records.append(
                ParamRecord(
                    code=code,
                    description=desc,
                    valid_from=vig_desde,
                    valid_to=vig_hasta,
                )
            )
    return records


class WsfexClient:
    def __init__(
        self,
        config: Config,
        wsaa: WsaaClient | None = None,
        http: httpx.Client | None = None,
    ):
        self.config = config
        self.wsaa = wsaa or WsaaClient(config)
        # TLS verify activo (design.md §2.1.1 punto 5). En prod WSFEX, OpenSSL
        # SECLEVEL=1 acepta el DH 1024 de servicios1.afip.gov.ar;
        # homologación y WSAA siguen en defaults.
        self.http = http or httpx.Client(
            timeout=60.0, verify=wsfex_verify(config.env)
        )
        self._cuit: int | None = None
        self.last_events: list[tuple[str, str]] = []

    @property
    def cuit(self) -> int:
        if self._cuit is None:
            # Nunca hardcodeado ni configurable: el certificado ya lo trae
            # en el subject (serialNumber=CUIT NNNNNNNNNNN).
            self._cuit = cuit_from_certificate(self.config.paths.cert.read_bytes())
        return self._cuit

    # --- infraestructura SOAP ---

    def _auth_element(self) -> ET.Element:
        ticket = self.wsaa.get_ticket()
        auth = ET.Element(f"{{{FEX_NS}}}Auth")
        ET.SubElement(auth, f"{{{FEX_NS}}}Token").text = ticket.token
        ET.SubElement(auth, f"{{{FEX_NS}}}Sign").text = ticket.sign
        ET.SubElement(auth, f"{{{FEX_NS}}}Cuit").text = str(self.cuit)
        return auth

    def call(self, method: str, *children: ET.Element) -> ET.Element:
        """Llama un método WSFEX y devuelve el elemento <method>Result."""
        envelope = ET.Element(f"{{{SOAP_ENV_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
        operation = ET.SubElement(body, f"{{{FEX_NS}}}{method}")
        for child in children:
            operation.append(child)
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

        response = self.http.post(
            self.config.wsfex_url,
            content=payload,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{FEX_NS}{method}"',
            },
        )
        root = ET.fromstring(response.text)
        if response.status_code >= 400:
            fault = root.find(".//{*}Fault")
            if fault is not None:
                raise WsfexError(
                    fault.findtext(".//faultcode", default="?"),
                    fault.findtext(".//faultstring", default=response.text[:500]),
                )
            raise WsfexError(str(response.status_code), f"HTTP {response.status_code}")

        result = root.find(f".//{{{FEX_NS}}}{method}Result")
        if result is None:
            result = root.find(f".//{{*}}{method}Result")
        if result is None:
            raise WsfexError("?", f"Respuesta sin {method}Result")

        self._collect_events(result, method)
        self._raise_on_error(result)
        return result

    def _collect_events(self, result: ET.Element, method: str) -> None:
        self.last_events = []
        for elem in result.iter():
            if _local(elem.tag) == "FEXEvents":
                code = elem.findtext(".//{*}EventCode") or elem.findtext("EventCode")
                msg = elem.findtext(".//{*}EventMsg") or elem.findtext("EventMsg")
                if code or msg:
                    self.last_events.append((code or "?", msg or ""))
                    logger.warning("Evento ARCA en %s [%s]: %s", method, code, msg)

    @staticmethod
    def _raise_on_error(result: ET.Element) -> None:
        for elem in result.iter():
            if _local(elem.tag) == "FEXErr":
                code = elem.findtext("{*}ErrCode") or elem.findtext("ErrCode") or "?"
                msg = elem.findtext("{*}ErrMsg") or elem.findtext("ErrMsg") or ""
                clean = code.strip()
                if clean not in ("0", "", "?"):
                    if clean in CMP_NOT_FOUND_CODES:
                        raise CmpNotFoundError(clean, msg)
                    raise WsfexError(clean, msg)
                return

    # --- métodos de negocio ---

    def dummy(self) -> dict[str, str]:
        """FEXDummy: estado de appserver/dbserver/authserver (sin TA)."""
        result = self.call("FEXDummy")
        return {
            _local(child.tag).lower(): (child.text or "").strip()
            for child in result
        }

    def get_param(self, kind: str) -> list[ParamRecord]:
        method = PARAM_METHODS[kind]
        result = self.call(method, self._auth_element())
        return parse_param_items(result)

    def get_ctz(self, mon_id: str, fch_cotiz: str | None = None) -> tuple[Decimal, str]:
        """Cotización ARCA de la moneda (Mon_ctz, Mon_fecha). §1.5: la
        cotización siempre sale de ARCA, nunca es propia."""
        mon = ET.Element(f"{{{FEX_NS}}}Mon_id")
        mon.text = mon_id
        children = [self._auth_element(), mon]
        if fch_cotiz:
            fch = ET.Element(f"{{{FEX_NS}}}FchCotiz")
            fch.text = fch_cotiz
            children.append(fch)
        result = self.call("FEXGetPARAM_Ctz", *children)
        ctz = _findtext_local(result, "Mon_ctz")
        fecha = _findtext_local(result, "Mon_fecha")
        if ctz is None:
            raise WsfexError("?", f"FEXGetPARAM_Ctz sin Mon_ctz para {mon_id}")
        return Decimal(ctz), fecha or ""

    def get_last_id(self) -> int:
        """Último Id (int64) usado en FEXAuthorize — base de la idempotencia."""
        result = self.call("FEXGetLast_ID", self._auth_element())
        last = _findtext_local(result, "Id")
        if last is None:
            raise WsfexError("?", "FEXGetLast_ID sin Id en la respuesta")
        return int(last)

    def get_last_cmp(self, punto_vta: int, cbte_tipo: int = CBTE_TIPO_FACTURA_E) -> int:
        """Último número autorizado para (pto_vta, tipo). ARCA es la fuente
        de verdad de la numeración (§3), nunca un contador local."""
        auth = self._auth_element()
        ET.SubElement(auth, f"{{{FEX_NS}}}Pto_venta").text = str(punto_vta)
        ET.SubElement(auth, f"{{{FEX_NS}}}Cbte_Tipo").text = str(cbte_tipo)
        result = self.call("FEXGetLast_CMP", auth)
        last = _findtext_local(result, "Cbte_nro")
        if last is None:
            raise WsfexError("?", "FEXGetLast_CMP sin Cbte_nro en la respuesta")
        return int(last)

    def authorize(self, invoice: Invoice) -> AuthResult:
        """FEXAuthorize. Valida que la respuesta corresponda al request
        enviado (Id, número, tipo, punto de venta) antes de aceptar el CAE —
        también en reprocesos (checklist §2.1.1 punto 3)."""
        result = self.call(
            "FEXAuthorize", self._auth_element(), build_cmp_element(invoice)
        )
        auth_result = parse_auth_result(result)
        mismatches = _match_request(auth_result, invoice)
        if mismatches:
            raise WsfexError(
                "?",
                "La respuesta de FEXAuthorize no corresponde al request "
                f"enviado ({'; '.join(mismatches)}). CAE NO aceptado.",
            )
        return auth_result

    def get_cmp(self, cbte_tipo: int, punto_vta: int, cbte_nro: int) -> dict[str, str]:
        """FEXGetCMP: campos planos tag→texto (sin ítems anidados).

        Base de la verificación post-emisión (checklist punto 6) y de la
        reconciliación tras timeouts (§1.5). Para rebuild con ítems usar
        :meth:`get_cmp_record`.
        """
        return dict(self.get_cmp_record(cbte_tipo, punto_vta, cbte_nro).fields)

    def get_cmp_record(
        self, cbte_tipo: int, punto_vta: int, cbte_nro: int
    ) -> CmpRecord:
        """FEXGetCMP estructurado: campos + ``Items/Item``.

        Raises:
            CmpNotFoundError: ARCA confirma que el número no existe (gap).
            WsfexError / httpx errors: fallo de negocio o transporte.
        """
        cmp_el = ET.Element(f"{{{FEX_NS}}}Cmp")
        ET.SubElement(cmp_el, f"{{{FEX_NS}}}Cbte_tipo").text = str(cbte_tipo)
        ET.SubElement(cmp_el, f"{{{FEX_NS}}}Punto_vta").text = str(punto_vta)
        ET.SubElement(cmp_el, f"{{{FEX_NS}}}Cbte_nro").text = str(cbte_nro)
        result = self.call("FEXGetCMP", self._auth_element(), cmp_el)
        return parse_cmp_record(result)

    def verify_issued(self, invoice: Invoice, auth_result: AuthResult) -> list[str]:
        """Constata contra FEXGetCMP que el CAE quedó registrado con los
        datos emitidos. Devuelve discrepancias (lista vacía = OK)."""
        registrado = self.get_cmp(
            invoice.cbte_tipo, invoice.punto_vta, invoice.cbte_nro
        )
        problemas: list[str] = []
        if registrado.get("Cae") != auth_result.cae:
            problemas.append(
                f"CAE registrado {registrado.get('Cae')!r} != {auth_result.cae!r}"
            )
        imp_registrado = registrado.get("Imp_total")
        if imp_registrado is None or Decimal(imp_registrado) != invoice.imp_total:
            problemas.append(
                f"Imp_total registrado {imp_registrado!r} != {invoice.imp_total}"
            )
        nro = registrado.get("Cbte_nro")
        if nro is None or int(nro) != invoice.cbte_nro:
            problemas.append(f"Cbte_nro registrado {nro!r} != {invoice.cbte_nro}")
        return problemas

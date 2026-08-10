"""Cliente WSCDC — Constatación de Comprobantes (FAC-84).

Independiente de ``FEXGetCMP``: esa reconciliación prueba que ARCA registró
*nuestro* authorize; WSCDC es la misma verificación del portal
"Constatación de Comprobantes". Solo se invoca por acción explícita del
operador (botón Constatar), nunca en el flujo de authorize.

Autenticación: TA WSAA con ``service=wscdc`` (cache propio; no reutiliza el
de ``wsfex``). Requests XML con ElementTree; Token/Sign nunca se loguean.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal

import httpx

from ..config import Config
from ..constants import (
    CBTE_MODO_CAE,
    CBTE_TIPO_FACTURA_E,
    SOAP_ENV_NS,
    TIPO_DOC_CUIT,
)
from .tls import arca_servicios1_verify
from .wsaa import SERVICE_WSCDC, WsaaClient, cuit_from_certificate

logger = logging.getLogger(__name__)

WSCDC_NS = "http://servicios1.afip.gob.ar/wscdc/"


class WscdcError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"WSCDC error {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ConstatacionRequest:
    """Campos mínimos del portal / CmpReq para Factura E."""

    cuit_emisor: int
    punto_venta: int
    cbte_tipo: int
    cbte_nro: int
    fecha_cbte: str  # AAAAMMDD
    imp_total: Decimal
    cae: str
    doc_tipo_receptor: str
    doc_nro_receptor: str
    cbte_modo: str = CBTE_MODO_CAE


@dataclass(frozen=True)
class ConstatacionResult:
    """Resultado de ``ComprobanteConstatar`` sin secretos."""

    resultado: str  # A | R | ?
    fch_proceso: str | None
    observations: tuple[tuple[str, str], ...]
    errors: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return self.resultado == "A"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _findtext_local(root: ET.Element, name: str) -> str | None:
    for elem in root.iter():
        if _local(elem.tag) == name:
            return (elem.text or "").strip() or None
    return None


def _collect_code_msg(
    root: ET.Element, item_tag: str
) -> tuple[tuple[str, str], ...]:
    """Recoge pares (Code, Msg) de elementos ``Err`` / ``Obs`` / ``Evt``."""
    out: list[tuple[str, str]] = []
    for elem in root.iter():
        if _local(elem.tag) != item_tag:
            continue
        code = None
        msg = None
        for child in elem:
            local = _local(child.tag)
            if local == "Code":
                code = (child.text or "").strip()
            elif local == "Msg":
                msg = (child.text or "").strip()
        if code or msg:
            out.append((code or "?", msg or ""))
    return tuple(out)


class WscdcClient:
    def __init__(
        self,
        config: Config,
        wsaa: WsaaClient | None = None,
        http: httpx.Client | None = None,
    ):
        self.config = config
        self.wsaa = wsaa or WsaaClient(config, service=SERVICE_WSCDC)
        # Prod WSCDC vive en servicios1 (mismo weak-DH que WSFEX, FAC-81).
        self.http = http or httpx.Client(
            timeout=60.0, verify=arca_servicios1_verify(config.env, label="WSCDC")
        )
        self._cuit: int | None = None

    @property
    def cuit(self) -> int:
        if self._cuit is None:
            self._cuit = cuit_from_certificate(self.config.paths.cert.read_bytes())
        return self._cuit

    def _auth_element(self) -> ET.Element:
        ticket = self.wsaa.get_ticket()
        auth = ET.Element(f"{{{WSCDC_NS}}}Auth")
        ET.SubElement(auth, f"{{{WSCDC_NS}}}Token").text = ticket.token
        ET.SubElement(auth, f"{{{WSCDC_NS}}}Sign").text = ticket.sign
        ET.SubElement(auth, f"{{{WSCDC_NS}}}Cuit").text = str(self.cuit)
        return auth

    def _cmp_req_element(self, req: ConstatacionRequest) -> ET.Element:
        cmp_req = ET.Element(f"{{{WSCDC_NS}}}CmpReq")
        fields = (
            ("CbteModo", req.cbte_modo),
            ("CuitEmisor", str(req.cuit_emisor)),
            ("PtoVta", str(req.punto_venta)),
            ("CbteTipo", str(req.cbte_tipo)),
            ("CbteNro", str(req.cbte_nro)),
            ("CbteFch", req.fecha_cbte),
            ("ImpTotal", format(req.imp_total, "f")),
            ("CodAutorizacion", req.cae),
            ("DocTipoReceptor", req.doc_tipo_receptor),
            ("DocNroReceptor", req.doc_nro_receptor),
        )
        for name, value in fields:
            ET.SubElement(cmp_req, f"{{{WSCDC_NS}}}{name}").text = value
        return cmp_req

    def call(self, method: str, *children: ET.Element) -> ET.Element:
        envelope = ET.Element(f"{{{SOAP_ENV_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
        operation = ET.SubElement(body, f"{{{WSCDC_NS}}}{method}")
        for child in children:
            operation.append(child)
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

        response = self.http.post(
            self.config.wscdc_url,
            content=payload,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{WSCDC_NS}{method}"',
            },
        )
        # Status first: 502/503 HTML outage pages are not XML. ParseError
        # must become WscdcError so the UI can show 503, not an unhandled 500.
        if response.status_code >= 400:
            try:
                root = ET.fromstring(response.text)
            except ET.ParseError as exc:
                raise WscdcError(
                    str(response.status_code),
                    f"HTTP {response.status_code}",
                ) from exc
            fault = root.find(".//{*}Fault")
            if fault is not None:
                raise WscdcError(
                    fault.findtext(".//faultcode", default="?") or "?",
                    fault.findtext(".//faultstring", default=response.text[:500])
                    or "",
                )
            raise WscdcError(str(response.status_code), f"HTTP {response.status_code}")

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise WscdcError("?", "Respuesta WSCDC no es XML válido") from exc

        result = root.find(f".//{{{WSCDC_NS}}}{method}Result")
        if result is None:
            result = root.find(f".//{{*}}{method}Result")
        if result is None:
            raise WscdcError("?", f"Respuesta sin {method}Result")
        return result

    def constatar(self, req: ConstatacionRequest) -> ConstatacionResult:
        """``ComprobanteConstatar`` — no loguea Token/Sign ni el SOAP crudo."""
        logger.info(
            "WSCDC ComprobanteConstatar: modo=%s tipo=%s pv=%s nro=%s "
            "fecha=%s importe=%s doc_rec=%s/%s",
            req.cbte_modo,
            req.cbte_tipo,
            req.punto_venta,
            req.cbte_nro,
            req.fecha_cbte,
            req.imp_total,
            req.doc_tipo_receptor,
            req.doc_nro_receptor,
        )
        result = self.call(
            "ComprobanteConstatar", self._auth_element(), self._cmp_req_element(req)
        )
        resultado = _findtext_local(result, "Resultado") or "?"
        fch = _findtext_local(result, "FchProceso")
        observations = _collect_code_msg(result, "Obs")
        errors = _collect_code_msg(result, "Err")
        # Events: loguear como warning (misma política que WSFEX), sin secretos.
        for code, msg in _collect_code_msg(result, "Evt"):
            logger.warning("Evento ARCA en ComprobanteConstatar [%s]: %s", code, msg)

        logger.info(
            "WSCDC ComprobanteConstatar resultado=%s fch=%s obs=%d err=%d",
            resultado,
            fch,
            len(observations),
            len(errors),
        )
        return ConstatacionResult(
            resultado=resultado,
            fch_proceso=fch,
            observations=observations,
            errors=errors,
        )

    @staticmethod
    def request_from_invoice_row(
        inv: object, *, cuit_emisor: int
    ) -> ConstatacionRequest:
        """Deriva CmpReq del snapshot local de una Factura E autorizada."""
        # sqlite3.Row / mapping
        status = inv["status"]  # type: ignore[index]
        cae = inv["cae"]  # type: ignore[index]
        cbte_nro = inv["cbte_nro"]  # type: ignore[index]
        if status != "authorized" or not cae or cbte_nro is None:
            raise WscdcError(
                "local",
                "Solo se puede constatar una Factura E autorizada con CAE y número",
            )
        cbte_tipo = int(inv["cbte_tipo"])  # type: ignore[index]
        if cbte_tipo != CBTE_TIPO_FACTURA_E:
            raise WscdcError(
                "local",
                f"Constatación in-app solo cubre Factura E (tipo 19); got {cbte_tipo}",
            )
        return ConstatacionRequest(
            cuit_emisor=cuit_emisor,
            punto_venta=int(inv["punto_venta"]),  # type: ignore[index]
            cbte_tipo=cbte_tipo,
            cbte_nro=int(cbte_nro),
            fecha_cbte=str(inv["fecha_cbte"]),  # type: ignore[index]
            imp_total=Decimal(str(inv["imp_total"])),  # type: ignore[index]
            cae=str(cae),
            doc_tipo_receptor=str(TIPO_DOC_CUIT),
            doc_nro_receptor=str(inv["cuit_pais_cliente"]),  # type: ignore[index]
        )

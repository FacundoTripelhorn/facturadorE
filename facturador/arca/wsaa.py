"""Cliente WSAA: obtención y cache del Ticket de Acceso (TA).

Flujo (spike.md §1.2):
  1. Armar LoginTicketRequest.xml (TRA) con service=wsfex y ventana amplia
     de tiempos (gen -10 min / exp +10 min, checklist §2.1.1 punto 8).
  2. Firmarlo como CMS/PKCS#7 con `cryptography` (sin subprocesos openssl).
  3. POST SOAP a LoginCms; la respuesta trae token + sign (~12 h de vida).
  4. Cachear el TA en disco (sobrevive reinicios: ARCA rechaza pedir un TA
     nuevo si ya hay uno vigente) y renovarlo solo al vencer.

Seguridad:
  - El TA recibido se valida antes de usarse: expirationTime parseado del
    ticket real y service == wsfex (checklist punto 2).
  - Token, sign y CMS firmado son credenciales: jamás se loguean ni entran
    en reprs/errores (checklist punto 9).
  - Todo XML se arma con ElementTree (serialización, nunca f-strings) y la
    verificación TLS de httpx queda en su default (activa) — puntos 4 y 5.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key, pkcs7
from cryptography.x509.oid import NameOID

from ..config import Config
from ..constants import SOAP_ENV_NS

logger = logging.getLogger(__name__)

SERVICE = "wsfex"

# Ventana del TRA: tolera clock skew moderado (checklist punto 8).
TRA_GENERATION_SLACK = dt.timedelta(minutes=10)
TRA_EXPIRATION_SLACK = dt.timedelta(minutes=10)

# Margen antes del vencimiento real del TA a partir del cual se renueva.
TA_EXPIRY_MARGIN = dt.timedelta(minutes=5)

WSAA_NS = "http://wsaa.view.sua.dvadac.desein.afip.gov"


class WsaaError(RuntimeError):
    """Error de WSAA (fault SOAP, respuesta inválida, TA inválido)."""


class TaAlreadyIssuedError(WsaaError):
    """ARCA ya emitió un TA vigente pero no lo tenemos cacheado.

    WSAA no re-entrega un TA vigente: hay que esperar a que venza (máx ~12 h)
    o recuperar el cache. Por eso el cache es persistente.
    """


@dataclass(frozen=True)
class Ticket:
    token: str = field(repr=False)
    sign: str = field(repr=False)
    generation: dt.datetime
    expiration: dt.datetime
    service: str
    environment: str

    def is_valid(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.UTC)
        return now < self.expiration - TA_EXPIRY_MARGIN

    def __str__(self) -> str:  # nunca exponer token/sign
        return (
            f"Ticket(service={self.service}, env={self.environment}, "
            f"expira={self.expiration.isoformat()}, token=[redactado])"
        )


def build_tra(service: str = SERVICE, now: dt.datetime | None = None) -> bytes:
    """Arma el LoginTicketRequest (TRA) como XML serializado."""
    now = (now or dt.datetime.now(dt.UTC)).astimezone()
    root = ET.Element("loginTicketRequest", version="1.0")
    header = ET.SubElement(root, "header")
    ET.SubElement(header, "uniqueId").text = str(int(now.timestamp()))
    ET.SubElement(header, "generationTime").text = (
        (now - TRA_GENERATION_SLACK).isoformat(timespec="seconds")
    )
    ET.SubElement(header, "expirationTime").text = (
        (now + TRA_EXPIRATION_SLACK).isoformat(timespec="seconds")
    )
    ET.SubElement(root, "service").text = service
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def sign_tra_cms(
    tra: bytes,
    cert_pem: bytes,
    key_pem: bytes,
    key_passphrase: bytes | None = None,
) -> bytes:
    """Firma el TRA como CMS/PKCS#7 (DER). Soporta key con passphrase."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = load_pem_private_key(key_pem, password=key_passphrase)
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise WsaaError("La clave privada debe ser RSA o EC para firmar el CMS")
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra)
        .add_signer(cert, key, hashes.SHA256())
        # Binary: firmar los bytes exactos del TRA, sin canonicalización
        # S/MIME de line endings.
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )


def cuit_from_certificate(cert_pem: bytes) -> int:
    """Extrae el CUIT del subject del certificado (serialNumber=CUIT NNN...).

    Evita hardcodear el CUIT en el repo: el certificado ya lo contiene.
    """
    cert = x509.load_pem_x509_certificate(cert_pem)
    attrs = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
    for attr in attrs:
        value = str(attr.value)
        match = re.search(r"(\d{11})", value)
        if match:
            return int(match.group(1))
    raise WsaaError(
        "No se pudo extraer el CUIT del certificado; definir ARCA_CUIT en .env"
    )


def build_login_request(cms_der: bytes) -> bytes:
    """Envelope SOAP 1.1 para LoginCms, armado con serializador."""
    ET.register_namespace("soapenv", SOAP_ENV_NS)
    ET.register_namespace("wsaa", WSAA_NS)
    envelope = ET.Element(f"{{{SOAP_ENV_NS}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP_ENV_NS}}}Body")
    login = ET.SubElement(body, f"{{{WSAA_NS}}}loginCms")
    ET.SubElement(login, f"{{{WSAA_NS}}}in0").text = base64.b64encode(cms_der).decode(
        "ascii"
    )
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _parse_datetime(raw: str, field_name: str) -> dt.datetime:
    try:
        value = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise WsaaError(f"{field_name} inválido en el TA: {raw!r}") from exc
    if value.tzinfo is None:
        # ARCA responde con offset; si faltara, asumir hora de Argentina.
        value = value.replace(tzinfo=dt.timezone(dt.timedelta(hours=-3)))
    return value


def parse_login_response(body: bytes | str, environment: str) -> Ticket:
    """Extrae y VALIDA el TA de la respuesta de LoginCms (checklist punto 2)."""
    root = ET.fromstring(body)
    ret = root.find(f".//{{{WSAA_NS}}}loginCmsReturn")
    if ret is None:
        ret = root.find(".//{*}loginCmsReturn")
    if ret is None or not ret.text:
        raise WsaaError("Respuesta de LoginCms sin loginCmsReturn")

    ta = ET.fromstring(ret.text)
    token = ta.findtext(".//credentials/token")
    sign = ta.findtext(".//credentials/sign")
    generation_raw = ta.findtext(".//header/generationTime")
    expiration_raw = ta.findtext(".//header/expirationTime")
    if not token or not sign or not expiration_raw:
        raise WsaaError("TA incompleto: falta token, sign o expirationTime")

    expiration = _parse_datetime(expiration_raw, "expirationTime")
    generation = (
        _parse_datetime(generation_raw, "generationTime")
        if generation_raw
        else dt.datetime.now(dt.UTC)
    )
    now = dt.datetime.now(dt.UTC)
    if expiration <= now:
        raise WsaaError(
            f"El TA recibido ya está vencido (expirationTime={expiration_raw}). "
            "Verificar sincronización de reloj (NTP contra time.afip.gov.ar)."
        )

    return Ticket(
        token=token,
        sign=sign,
        generation=generation,
        expiration=expiration,
        service=SERVICE,
        environment=environment,
    )


def _parse_fault(body: bytes | str) -> tuple[str, str] | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    fault = root.find(".//{*}Fault")
    if fault is None:
        return None
    code = fault.findtext("faultcode") or fault.findtext("{*}faultcode") or ""
    string = fault.findtext("faultstring") or fault.findtext("{*}faultstring") or ""
    return code, string


class TicketCache:
    """Cache persistente del TA (spike.md §3: sobrevive reinicios)."""

    def __init__(self, path: Path):
        self.path = path

    def load(self, environment: str, service: str = SERVICE) -> Ticket | None:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            ticket = Ticket(
                token=raw["token"],
                sign=raw["sign"],
                generation=dt.datetime.fromisoformat(raw["generation"]),
                expiration=dt.datetime.fromisoformat(raw["expiration"]),
                service=raw["service"],
                environment=raw["environment"],
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            logger.warning("Cache de TA corrupto en %s; se descarta", self.path)
            return None
        # Rechazar TAs de otro servicio u otro ambiente (checklist punto 2).
        if ticket.service != service or ticket.environment != environment:
            return None
        if not ticket.is_valid():
            return None
        return ticket

    def save(self, ticket: Ticket) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "token": ticket.token,
                    "sign": ticket.sign,
                    "generation": ticket.generation.isoformat(),
                    "expiration": ticket.expiration.isoformat(),
                    "service": ticket.service,
                    "environment": ticket.environment,
                }
            ),
            encoding="utf-8",
        )


class WsaaClient:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        # TLS verify queda en el default de httpx (activo) — checklist punto 5.
        self.http = http or httpx.Client(timeout=30.0)
        self.cache = TicketCache(config.data_dir / f"ta-{SERVICE}-{config.env}.json")

    def get_ticket(self) -> Ticket:
        """TA cacheado si sigue vigente; si no, pide uno nuevo y lo persiste."""
        cached = self.cache.load(self.config.env)
        if cached is not None:
            logger.info("TA vigente reutilizado: %s", cached)
            return cached
        ticket = self._request_new_ticket()
        self.cache.save(ticket)
        logger.info("TA nuevo obtenido: %s", ticket)
        return ticket

    def _request_new_ticket(self) -> Ticket:
        tra = build_tra()
        cms = sign_tra_cms(
            tra,
            self.config.cert_path.read_bytes(),
            self.config.key_path.read_bytes(),
            self.config.key_passphrase.encode() if self.config.key_passphrase else None,
        )
        request_body = build_login_request(cms)
        response = self.http.post(
            self.config.wsaa_url,
            content=request_body,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "",
            },
        )
        if response.status_code >= 400:
            fault = _parse_fault(response.text)
            if fault is not None:
                code, message = fault
                if "alreadyauthenticated" in (code + message).lower():
                    raise TaAlreadyIssuedError(
                        "ARCA reporta un TA vigente para este certificado que no "
                        "está en el cache local. Esperar a que venza (máx ~12 h) "
                        "o restaurar el archivo de cache."
                    )
                # faultstring de WSAA no contiene credenciales; es seguro mostrarlo.
                raise WsaaError(f"WSAA fault [{code}]: {message}")
            raise WsaaError(f"WSAA HTTP {response.status_code}")
        return parse_login_response(response.text, self.config.env)

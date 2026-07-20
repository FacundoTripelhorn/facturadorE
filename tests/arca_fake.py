"""Simulador de WSFEX para tests de la fase 4.

Mantiene estado (numeración, comprobantes emitidos) y permite simular los
modos de falla que definen la máquina de estados: rechazo de negocio,
timeout sin emisión y timeout post-envío con emisión (el caso 'unknown').
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections import Counter

import httpx

from facturador.arca.wsaa import Ticket
from facturador.arca.wsfex import FEX_NS


class FakeWsaa:
    """WSAA que siempre entrega un TA vigente de mentira."""

    def get_ticket(self) -> Ticket:
        return Ticket(
            token="tok==",
            sign="sig==",
            generation=dt.datetime.now(dt.UTC),
            expiration=dt.datetime.now(dt.UTC) + dt.timedelta(hours=12),
            service="wsfex",
            environment="homo",
        )


def soap_response(method: str, inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{method}Response xmlns="{FEX_NS}">'
        f"<{method}Result>{inner}</{method}Result>"
        f"</{method}Response></soap:Body></soap:Envelope>"
    )


def _findtext(root: ET.Element, name: str) -> str | None:
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] == name:
            return elem.text
    return None


class FakeArca:
    def __init__(self):
        self.last_id = 0
        self.last_cmp: dict[tuple[int, int], int] = {}
        self.issued: dict[tuple[int, int, int], dict] = {}
        self.authorize_mode = "ok"  # ok | reject | timeout | timeout_but_issued
        self.get_cmp_mode = "ok"  # ok | timeout | error (FAC-65)
        self.reject_code = "1068"
        self.reject_msg = "Campo Id_impositivo invalido"
        self.ctz = "1145.5690"
        self.calls: Counter[str] = Counter()
        self.last_authorize_cliente: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.headers.get("SOAPAction", "").strip('"').rsplit("/", 1)[-1]
        self.calls[method] += 1
        body = ET.fromstring(request.content)  # explota si el XML se rompió
        fn = getattr(self, f"_{method.lower()}", None)
        if fn is None:
            return httpx.Response(
                500, text=f"método no soportado por el fake: {method}"
            )
        return fn(body)

    # --- métodos simulados ---

    def _fexdummy(self, body):
        return httpx.Response(
            200,
            text=soap_response(
                "FEXDummy",
                "<AppServer>OK</AppServer><DbServer>OK</DbServer>"
                "<AuthServer>OK</AuthServer>",
            ),
        )

    def _fexgetparam_ctz(self, body):
        return httpx.Response(
            200,
            text=soap_response(
                "FEXGetPARAM_Ctz",
                f"<FEXResultGet><Mon_ctz>{self.ctz}</Mon_ctz>"
                "<Mon_fecha>20260703</Mon_fecha></FEXResultGet>",
            ),
        )

    def _fexgetlast_id(self, body):
        return httpx.Response(
            200,
            text=soap_response(
                "FEXGetLast_ID",
                f"<FEXResultGet><Id>{self.last_id}</Id></FEXResultGet>",
            ),
        )

    def _fexgetlast_cmp(self, body):
        pv = int(_findtext(body, "Pto_venta"))
        tipo = int(_findtext(body, "Cbte_Tipo"))
        nro = self.last_cmp.get((pv, tipo), 0)
        return httpx.Response(
            200,
            text=soap_response(
                "FEXGetLast_CMP",
                f"<FEXResult_LastCMP><Cbte_nro>{nro}</Cbte_nro></FEXResult_LastCMP>",
            ),
        )

    def _fexauthorize(self, body):
        arca_id = int(_findtext(body, "Id"))
        tipo = int(_findtext(body, "Cbte_Tipo"))
        pv = int(_findtext(body, "Punto_vta"))
        nro = int(_findtext(body, "Cbte_nro"))
        imp_total = _findtext(body, "Imp_total")
        self.last_authorize_cliente = _findtext(body, "Cliente")
        key = (tipo, pv, nro)

        if self.authorize_mode == "reject":
            return httpx.Response(
                200,
                text=soap_response(
                    "FEXAuthorize",
                    f"<FEXErr><ErrCode>{self.reject_code}</ErrCode>"
                    f"<ErrMsg>{self.reject_msg}</ErrMsg></FEXErr>",
                ),
            )
        if self.authorize_mode in ("timeout", "timeout_but_issued"):
            if self.authorize_mode == "timeout_but_issued":
                self._register(key, arca_id, imp_total)
            raise httpx.ConnectTimeout("timeout simulado post-envío")

        # Reproceso ARCA: mismo Id sobre un comprobante ya emitido devuelve
        # el mismo CAE con Reproceso=S.
        reproceso = "N"
        if key in self.issued:
            if self.issued[key]["arca_id"] == arca_id:
                reproceso = "S"
            else:
                return httpx.Response(
                    200,
                    text=soap_response(
                        "FEXAuthorize",
                        "<FEXErr><ErrCode>1462</ErrCode>"
                        "<ErrMsg>Nro de comprobante ya utilizado</ErrMsg></FEXErr>",
                    ),
                )
        else:
            self._register(key, arca_id, imp_total)
        emitido = self.issued[key]
        return httpx.Response(
            200,
            text=soap_response(
                "FEXAuthorize",
                "<FEXResultAuth>"
                f"<Id>{arca_id}</Id><Cbte_nro>{nro}</Cbte_nro>"
                f"<Cae>{emitido['cae']}</Cae><Fch_venc_Cae>20260713</Fch_venc_Cae>"
                f"<Fecha_cbte>20260703</Fecha_cbte><Cbte_tipo>{tipo}</Cbte_tipo>"
                f"<Punto_vta>{pv}</Punto_vta><Reproceso>{reproceso}</Reproceso>"
                "</FEXResultAuth>"
                "<FEXErr><ErrCode>0</ErrCode><ErrMsg>OK</ErrMsg></FEXErr>",
            ),
        )

    def _register(self, key, arca_id, imp_total, **extra):
        tipo, pv, nro = key
        row = {
            "arca_id": arca_id,
            "cae": f"761{nro:011d}",
            "imp_total": imp_total,
            "fecha_cbte": extra.get("fecha_cbte", "20260703"),
            "fecha_pago": extra.get("fecha_pago", "20260703"),
            "cliente": extra.get("cliente", "Cliente Fake"),
            "cuit_pais_cliente": extra.get("cuit_pais_cliente", "55000002002"),
            "domicilio_cliente": extra.get("domicilio_cliente", "Abroad 1"),
            "id_impositivo": extra.get("id_impositivo", "12-345"),
            "dst_cmp": extra.get("dst_cmp", "225"),
            "moneda_id": extra.get("moneda_id", "DOL"),
            "moneda_ctz": extra.get("moneda_ctz", "1000"),
            "forma_pago": extra.get("forma_pago", "WIRE"),
            "tipo_expo": extra.get("tipo_expo", "2"),
            "idioma_cbte": extra.get("idioma_cbte", "1"),
            "items": extra.get(
                "items",
                [
                    {
                        "pro_codigo": "0001",
                        "pro_ds": "Servicio",
                        "pro_qty": "1",
                        "pro_umed": "7",
                        "pro_precio_uni": str(imp_total),
                        "pro_total_item": str(imp_total),
                    }
                ],
            ),
        }
        self.issued[key] = row
        self.last_cmp[(pv, tipo)] = max(self.last_cmp.get((pv, tipo), 0), nro)
        self.last_id = max(self.last_id, arca_id)

    def seed_issued(
        self,
        *,
        tipo: int,
        pv: int,
        nro: int,
        arca_id: int | None = None,
        imp_total: str = "100.00",
        **extra,
    ) -> None:
        """Registra un comprobante como si ya estuviera en ARCA (rebuild tests)."""
        reserved = arca_id if arca_id is not None else (10_000 + nro)
        self._register((tipo, pv, nro), reserved, imp_total, **extra)

    def _fexgetcmp(self, body):
        tipo = int(_findtext(body, "Cbte_tipo"))
        pv = int(_findtext(body, "Punto_vta"))
        nro = int(_findtext(body, "Cbte_nro"))
        # Fallo de transporte configurable (FAC-65 retries / abort).
        if getattr(self, "get_cmp_mode", "ok") == "timeout":
            raise httpx.ConnectTimeout("timeout simulado FEXGetCMP")
        if getattr(self, "get_cmp_mode", "ok") == "error":
            return httpx.Response(
                200,
                text=soap_response(
                    "FEXGetCMP",
                    "<FEXErr><ErrCode>500</ErrCode>"
                    "<ErrMsg>Error interno simulado</ErrMsg></FEXErr>",
                ),
            )
        emitido = self.issued.get((tipo, pv, nro))
        if emitido is None:
            return httpx.Response(
                200,
                text=soap_response(
                    "FEXGetCMP",
                    "<FEXErr><ErrCode>1521</ErrCode>"
                    "<ErrMsg>No existen datos para el comprobante</ErrMsg></FEXErr>",
                ),
            )
        items_xml = "".join(
            "<Item>"
            f"<Pro_codigo>{it['pro_codigo']}</Pro_codigo>"
            f"<Pro_ds>{it['pro_ds']}</Pro_ds>"
            f"<Pro_qty>{it['pro_qty']}</Pro_qty>"
            f"<Pro_umed>{it['pro_umed']}</Pro_umed>"
            f"<Pro_precio_uni>{it['pro_precio_uni']}</Pro_precio_uni>"
            "<Pro_bonificacion>0</Pro_bonificacion>"
            f"<Pro_total_item>{it['pro_total_item']}</Pro_total_item>"
            "</Item>"
            for it in emitido["items"]
        )
        return httpx.Response(
            200,
            text=soap_response(
                "FEXGetCMP",
                "<FEXResultGet>"
                f"<Id>{emitido['arca_id']}</Id>"
                f"<Fecha_cbte>{emitido['fecha_cbte']}</Fecha_cbte>"
                f"<Cbte_tipo>{tipo}</Cbte_tipo><Punto_vta>{pv}</Punto_vta>"
                f"<Cbte_nro>{nro}</Cbte_nro>"
                f"<Tipo_expo>{emitido['tipo_expo']}</Tipo_expo>"
                "<Permiso_existente></Permiso_existente>"
                f"<Dst_cmp>{emitido['dst_cmp']}</Dst_cmp>"
                f"<Cliente>{emitido['cliente']}</Cliente>"
                f"<Cuit_pais_cliente>{emitido['cuit_pais_cliente']}</Cuit_pais_cliente>"
                f"<Domicilio_cliente>{emitido['domicilio_cliente']}</Domicilio_cliente>"
                f"<Id_impositivo>{emitido['id_impositivo']}</Id_impositivo>"
                f"<Moneda_Id>{emitido['moneda_id']}</Moneda_Id>"
                f"<Moneda_ctz>{emitido['moneda_ctz']}</Moneda_ctz>"
                f"<Imp_total>{emitido['imp_total']}</Imp_total>"
                f"<Forma_pago>{emitido['forma_pago']}</Forma_pago>"
                "<Incoterms></Incoterms>"
                f"<Idioma_cbte>{emitido['idioma_cbte']}</Idioma_cbte>"
                f"<Items>{items_xml}</Items>"
                f"<Fecha_pago>{emitido['fecha_pago']}</Fecha_pago>"
                f"<Cae>{emitido['cae']}</Cae>"
                "<Fch_venc_Cae>20260713</Fch_venc_Cae>"
                "</FEXResultGet>"
                "<FEXErr><ErrCode>0</ErrCode><ErrMsg>OK</ErrMsg></FEXErr>",
            ),
        )

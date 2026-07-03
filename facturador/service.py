"""InvoiceService: validación de dominio, numeración/idempotencia y máquina
de estados (spike.md §2.2/§2.3).

Estados: draft → submitting → authorized | rejected | unknown
  - 'unknown' = timeout u otra falla post-envío sin respuesta concluyente;
    se reconcilia con FEXGetCMP (lazy en GET y al reintentar authorize).
  - El reintento sobre submitting/unknown reusa el MISMO arca_id y los datos
    idénticos persistidos en raw_request (reproceso ARCA, checklist §2.1.1
    punto 3). raw_request se escribe SIEMPRE antes de llamar a FEXAuthorize:
    si raw_request es NULL, se garantiza que nunca se envió nada.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from decimal import Decimal

import httpx

from . import db, repo
from .config import Config
from .schemas import ClientIn, InvoiceCreate
from .wsfex import Invoice, InvoiceItem, WsfexClient, WsfexError

logger = logging.getLogger(__name__)

PARAMS_TTL = dt.timedelta(hours=24)


class ServiceError(RuntimeError):
    pass


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class DomainError(ServiceError):
    pass


class ArcaUnavailableError(ServiceError):
    pass


class InvoiceService:
    def __init__(self, config: Config, conn: sqlite3.Connection, wsfex: WsfexClient):
        self.config = config
        self.conn = conn
        self.wsfex = wsfex

    # ------------------------------------------------------------------
    # Parámetros (cache con refresh lazy de 24 h — spike.md §6.2)
    # ------------------------------------------------------------------

    def get_params(self, kind: str) -> list[sqlite3.Row]:
        rows = db.get_params(self.conn, kind)
        if rows:
            fetched = dt.datetime.fromisoformat(rows[0]["fetched_at"])
            if dt.datetime.now(dt.timezone.utc) - fetched < PARAMS_TTL:
                return rows
        try:
            records = self.wsfex.get_param(kind)
            db.replace_params(self.conn, kind, [r.as_dict() for r in records])
            return db.get_params(self.conn, kind)
        except (WsfexError, httpx.HTTPError) as exc:
            if rows:
                logger.warning(
                    "No se pudo refrescar %s (%s); se usa cache viejo", kind, exc
                )
                return rows
            raise ArcaUnavailableError(
                f"Sin cache de {kind} y ARCA no respondió: {exc}"
            ) from exc

    def _validate_code(self, kind: str, code: object, campo: str) -> None:
        codigos = {row["code"] for row in self.get_params(kind)}
        if str(code) not in codigos:
            raise DomainError(
                f"{campo}={code} no está en la tabla '{kind}' de ARCA"
            )

    # ------------------------------------------------------------------
    # Clientes
    # ------------------------------------------------------------------

    def create_client(self, payload: ClientIn) -> sqlite3.Row:
        self._validate_client_codes(payload)
        return repo.create_client(self.conn, payload.model_dump())

    def update_client(self, client_id: str, payload: ClientIn) -> sqlite3.Row:
        self._validate_client_codes(payload)
        row = repo.update_client(self.conn, client_id, payload.model_dump())
        if row is None:
            raise NotFoundError(f"Cliente {client_id} no existe")
        # No toca facturas: los datos viajan snapshoteados en cada invoice.
        return row

    def _validate_client_codes(self, payload: ClientIn) -> None:
        self._validate_code("pais", payload.pais_dst, "pais_dst")
        self._validate_code("cuit_pais", payload.cuit_pais, "cuit_pais")
        self._validate_code("moneda", payload.moneda_default, "moneda_default")
        self._validate_code("idioma", payload.idioma_default, "idioma_default")

    # ------------------------------------------------------------------
    # Facturas
    # ------------------------------------------------------------------

    def create_invoice(self, payload: InvoiceCreate) -> sqlite3.Row:
        if payload.client_id is not None:
            client = repo.get_client(self.conn, payload.client_id)
            if client is None:
                raise NotFoundError(f"Cliente {payload.client_id} no existe")
        else:
            client = repo.get_default_client(self.conn)
            if client is None:
                raise ConflictError(
                    "No hay cliente default; indicar client_id o marcar uno"
                )

        fecha_cbte = payload.fecha_cbte or dt.date.today().strftime("%Y%m%d")
        fecha_pago = payload.fecha_pago or fecha_cbte
        moneda_id = payload.moneda_id or client["moneda_default"]
        descripcion = payload.descripcion or client["descripcion_default"]

        self._validate_code("moneda", moneda_id, "moneda_id")
        self._validate_code("pais", client["pais_dst"], "pais_dst")
        self._validate_code("cuit_pais", client["cuit_pais"], "cuit_pais")

        if payload.moneda_ctz is not None:
            ctz = payload.moneda_ctz
        elif moneda_id == "PES":
            ctz = Decimal(1)
        else:
            # Cotización de ARCA para la fecha de emisión, nunca propia (§0).
            try:
                ctz, _ = self.wsfex.get_ctz(moneda_id)
            except (WsfexError, httpx.HTTPError) as exc:
                raise ArcaUnavailableError(
                    f"No se pudo obtener la cotización {moneda_id} de ARCA: {exc}"
                ) from exc

        if payload.items:
            items = [
                {
                    "pro_codigo": i.pro_codigo,
                    "pro_ds": i.pro_ds,
                    "pro_qty": _dec(i.pro_qty),
                    "pro_umed": i.pro_umed,
                    "pro_precio_uni": _dec(i.pro_precio_uni),
                    "pro_total_item": _dec(i.pro_qty * i.pro_precio_uni),
                }
                for i in payload.items
            ]
        else:
            # Patrón real §0.1: 1 línea, qty 1, precio = importe total.
            if not descripcion:
                raise DomainError(
                    "Falta descripción (ni en el request ni como default del cliente)"
                )
            items = [
                {
                    "pro_codigo": "0001",
                    "pro_ds": descripcion,
                    "pro_qty": "1",
                    "pro_umed": 7,
                    "pro_precio_uni": _dec(payload.imp_total),
                    "pro_total_item": _dec(payload.imp_total),
                }
            ]

        total_items = sum(Decimal(i["pro_total_item"]) for i in items)
        if total_items != payload.imp_total:
            raise DomainError(
                f"imp_total {payload.imp_total} != suma de items {total_items}"
            )

        data = {
            "client_id": client["id"],
            "cbte_tipo": 19,
            "punto_venta": self.config.punto_venta,
            "fecha_cbte": fecha_cbte,
            "fecha_pago": fecha_pago,
            "tipo_expo": 2,
            "permiso_existente": "",
            "dst_cmp": client["pais_dst"],
            # Snapshot del cliente: el comprobante queda inmutable aunque el
            # cliente se edite después (spike.md §6.3).
            "cliente": client["razon_social"],
            "cuit_pais_cliente": client["cuit_pais"],
            "domicilio_cliente": client["domicilio"],
            "id_impositivo": client["id_impositivo"],
            "moneda_id": moneda_id,
            "moneda_ctz": _dec(ctz),
            "incoterms": "",  # vacío en servicios (§0.1)
            "incoterms_ds": "",
            "forma_pago": client["forma_pago_default"],
            "idioma_cbte": client["idioma_default"],
            "imp_total": _dec(payload.imp_total),
            "obs": payload.obs,
            "environment": self.config.env,
        }
        return repo.create_invoice(self.conn, data, items)

    def get_invoice(self, invoice_id: str, reconcile: bool = True) -> sqlite3.Row:
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:
            raise NotFoundError(f"Factura {invoice_id} no existe")
        if reconcile and inv["status"] == "unknown" and inv["raw_request"]:
            try:
                resolved = self._try_reconcile(inv)
            except (WsfexError, httpx.HTTPError):
                resolved = None  # ARCA no disponible: sigue unknown
            if resolved is not None:
                return resolved
        return inv

    # ------------------------------------------------------------------
    # Authorize (reglas §2.3)
    # ------------------------------------------------------------------

    def authorize(self, invoice_id: str, force_desync: bool = False) -> sqlite3.Row:
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:
            raise NotFoundError(f"Factura {invoice_id} no existe")
        if inv["status"] == "authorized":
            return inv  # idempotente: mismo CAE, sin tocar ARCA
        if inv["status"] == "rejected":
            raise ConflictError(
                f"Factura rechazada por ARCA ({inv['last_error']}); "
                "corregir los datos creando una nueva."
            )

        # Lock anti doble-submit (regla 1).
        if not repo.try_transition_to_submitting(self.conn, invoice_id):
            raise ConflictError("Ya hay un authorize en curso para esta factura")

        inv = repo.get_invoice(self.conn, invoice_id)
        try:
            if inv["raw_request"] is None:
                return self._authorize_first_time(inv, force_desync)
            return self._authorize_retry(inv)
        except (ConflictError, ArcaUnavailableError):
            # No se llegó a enviar nada: volver a draft para no dejar el
            # lock tomado (raw_request sigue NULL).
            if inv["raw_request"] is None:
                repo.update_invoice(self.conn, invoice_id, status="draft")
            raise

    def _authorize_first_time(
        self, inv: sqlite3.Row, force_desync: bool
    ) -> sqlite3.Row:
        invoice_id = inv["id"]
        try:
            last_cmp = self.wsfex.get_last_cmp(inv["punto_venta"], inv["cbte_tipo"])
            arca_id = inv["arca_id"] or self.wsfex.get_last_id() + 1
        except (WsfexError, httpx.HTTPError) as exc:
            raise ArcaUnavailableError(f"ARCA no disponible: {exc}") from exc

        # Detección de DB desactualizada (§2.5): si ARCA conoce comprobantes
        # que la DB local no tiene, bloquear (esquema multi-máquina).
        local_max = repo.max_authorized_cbte_nro(
            self.conn, inv["punto_venta"], inv["cbte_tipo"]
        )
        if last_cmp > local_max and not force_desync:
            raise ConflictError(
                f"Registro local desactualizado: ARCA reporta último comprobante "
                f"{last_cmp} para PV {inv['punto_venta']} tipo {inv['cbte_tipo']} "
                f"pero la DB local llega a {local_max}. Restaurar el último "
                "backup antes de emitir (o forzar con force_desync=true si es "
                "intencional, p.ej. homologación)."
            )

        cbte_nro = last_cmp + 1
        wsfex_invoice = _row_to_wsfex_invoice(inv, arca_id, cbte_nro,
                                              repo.get_invoice_items(self.conn, invoice_id))
        # Regla 3: persistir el request completo ANTES de llamar.
        repo.update_invoice(
            self.conn,
            invoice_id,
            arca_id=arca_id,
            cbte_nro=cbte_nro,
            raw_request=json.dumps(_wsfex_invoice_to_raw(wsfex_invoice)),
        )
        return self._send(invoice_id, wsfex_invoice)

    def _authorize_retry(self, inv: sqlite3.Row) -> sqlite3.Row:
        # Primero reconciliar: la llamada anterior pudo haber sido autorizada.
        try:
            resolved = self._try_reconcile(inv)
        except (WsfexError, httpx.HTTPError):
            resolved = None
        if resolved is not None:
            return resolved
        # Regla 5: reproceso con el MISMO arca_id y datos idénticos, tomados
        # del request persistido (no de la fila, por si algo mutó).
        wsfex_invoice = _raw_to_wsfex_invoice(json.loads(inv["raw_request"]))
        return self._send(inv["id"], wsfex_invoice)

    def _send(self, invoice_id: str, wsfex_invoice: Invoice) -> sqlite3.Row:
        try:
            result = self.wsfex.authorize(wsfex_invoice)
        except WsfexError as exc:
            # Respuesta concluyente de ARCA: rechazo con error legible.
            repo.update_invoice(
                self.conn,
                invoice_id,
                status="rejected",
                last_error=f"ARCA {exc.code}: {exc.message}",
                raw_response=json.dumps({"error": exc.code, "message": exc.message}),
            )
            return repo.get_invoice(self.conn, invoice_id)
        except httpx.HTTPError as exc:
            # Sin respuesta concluyente (timeout post-envío, corte, etc.):
            # unknown hasta reconciliar con FEXGetCMP (§1.5).
            repo.update_invoice(
                self.conn,
                invoice_id,
                status="unknown",
                last_error=f"Sin respuesta de ARCA: {exc}",
            )
            return repo.get_invoice(self.conn, invoice_id)

        # Verificación post-emisión (checklist punto 6).
        problemas = self.wsfex.verify_issued(wsfex_invoice, result)
        if problemas:
            repo.update_invoice(
                self.conn,
                invoice_id,
                status="unknown",
                cae=result.cae,
                cae_fch_vto=result.cae_fch_vto,
                last_error="Verificación FEXGetCMP con discrepancias: "
                + "; ".join(problemas),
            )
            return repo.get_invoice(self.conn, invoice_id)

        repo.update_invoice(
            self.conn,
            invoice_id,
            status="authorized",
            cae=result.cae,
            cae_fch_vto=result.cae_fch_vto,
            last_error=None,
            raw_response=json.dumps(
                {
                    "cae": result.cae,
                    "cae_fch_vto": result.cae_fch_vto,
                    "reproceso": result.reproceso,
                    "motivos_obs": result.motivos_obs,
                    "events": self.wsfex.last_events,
                }
            ),
        )
        return repo.get_invoice(self.conn, invoice_id)

    def _try_reconcile(self, inv: sqlite3.Row) -> sqlite3.Row | None:
        """FEXGetCMP para una factura submitting/unknown. Devuelve la fila
        actualizada si se resolvió, None si ARCA no registra el comprobante
        (=> es seguro reintentar/reprocesar)."""
        try:
            registrado = self.wsfex.get_cmp(
                inv["cbte_tipo"], inv["punto_venta"], inv["cbte_nro"]
            )
        except WsfexError:
            return None  # típicamente "no existe comprobante": no se registró
        cae = registrado.get("Cae")
        if not cae:
            return None
        # Nunca aceptar un CAE sin verificar contra el request persistido
        # (checklist punto 3).
        raw = json.loads(inv["raw_request"])
        imp_registrado = registrado.get("Imp_total")
        if imp_registrado is None or Decimal(imp_registrado) != Decimal(raw["imp_total"]):
            repo.update_invoice(
                self.conn,
                inv["id"],
                status="unknown",
                last_error=(
                    f"ARCA registra el comprobante {inv['cbte_nro']} con importe "
                    f"{imp_registrado}, distinto del enviado {raw['imp_total']}. "
                    "Revisión manual requerida."
                ),
            )
            return repo.get_invoice(self.conn, inv["id"])
        repo.update_invoice(
            self.conn,
            inv["id"],
            status="authorized",
            cae=cae,
            cae_fch_vto=registrado.get("Fch_venc_Cae"),
            last_error=None,
            raw_response=json.dumps({"reconciled_from": "FEXGetCMP", **registrado}),
        )
        logger.info(
            "Factura %s reconciliada vía FEXGetCMP: CAE recuperado", inv["id"]
        )
        return repo.get_invoice(self.conn, inv["id"])


def _dec(value: Decimal) -> str:
    return format(value, "f")


def _row_to_wsfex_invoice(
    inv: sqlite3.Row, arca_id: int, cbte_nro: int, items: list[sqlite3.Row]
) -> Invoice:
    return Invoice(
        arca_id=arca_id,
        fecha_cbte=inv["fecha_cbte"],
        punto_vta=inv["punto_venta"],
        cbte_nro=cbte_nro,
        dst_cmp=inv["dst_cmp"],
        cliente=inv["cliente"],
        cuit_pais_cliente=inv["cuit_pais_cliente"],
        domicilio_cliente=inv["domicilio_cliente"],
        id_impositivo=inv["id_impositivo"],
        moneda_ctz=Decimal(inv["moneda_ctz"]),
        imp_total=Decimal(inv["imp_total"]),
        fecha_pago=inv["fecha_pago"],
        forma_pago=inv["forma_pago"],
        cbte_tipo=inv["cbte_tipo"],
        tipo_expo=inv["tipo_expo"],
        permiso_existente=inv["permiso_existente"],
        moneda_id=inv["moneda_id"],
        incoterms=inv["incoterms"],
        idioma_cbte=inv["idioma_cbte"],
        obs=inv["obs"],
        items=[
            InvoiceItem(
                pro_ds=i["pro_ds"],
                pro_precio_uni=Decimal(i["pro_precio_uni"]),
                pro_codigo=i["pro_codigo"],
                pro_qty=Decimal(i["pro_qty"]),
                pro_umed=i["pro_umed"],
            )
            for i in items
        ],
    )


def _wsfex_invoice_to_raw(invoice: Invoice) -> dict:
    raw = {
        k: (format(v, "f") if isinstance(v, Decimal) else v)
        for k, v in vars(invoice).items()
        if k != "items"
    }
    raw["items"] = [
        {
            "pro_ds": i.pro_ds,
            "pro_precio_uni": format(i.pro_precio_uni, "f"),
            "pro_codigo": i.pro_codigo,
            "pro_qty": format(i.pro_qty, "f"),
            "pro_umed": i.pro_umed,
        }
        for i in invoice.items
    ]
    return raw


def _raw_to_wsfex_invoice(raw: dict) -> Invoice:
    datos = dict(raw)
    items = [
        InvoiceItem(
            pro_ds=i["pro_ds"],
            pro_precio_uni=Decimal(i["pro_precio_uni"]),
            pro_codigo=i["pro_codigo"],
            pro_qty=Decimal(i["pro_qty"]),
            pro_umed=i["pro_umed"],
        )
        for i in datos.pop("items")
    ]
    for campo in ("moneda_ctz", "imp_total"):
        datos[campo] = Decimal(datos[campo])
    return Invoice(items=items, **datos)

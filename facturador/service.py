"""InvoiceService: validación de dominio, numeración/idempotencia y máquina
de estados (design.md §2.2/§2.3).

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
import threading
from decimal import Decimal
from typing import Any

import httpx

from . import repo
from .arca.wsfex import Invoice, WsfexClient, WsfexError
from .config import Config
from .constants import (
    CBTE_TIPO_FACTURA_E,
    TIPO_EXPO_SERVICIOS,
    UMED_UNIDADES,
    InvoiceStatus,
)
from .mappers import (
    dec,
    raw_to_wsfex_invoice,
    row_to_wsfex_invoice,
    wsfex_invoice_to_raw,
)
from .schemas import ClientIn, InvoiceCreate, SettingsIn
from .settings import Emisor, Settings, load_settings, save_settings

logger = logging.getLogger(__name__)

PARAMS_TTL = dt.timedelta(hours=24)

# Serializa authorize() de punta a punta entre llamadas concurrentes. Dos
# razones: (1) ARCA exige numeración estrictamente secuencial (solo se puede
# autorizar last_cmp+1), así que dos emisiones NO pueden pipelinearse — la
# segunda tiene que ver el CAE de la primera antes de tomar su número (el
# arca_id además es global por CUIT, no por serie); (2) la conexión SQLite es
# única y compartida entre threads (§2.5, FastAPI atiende en threadpool), y no
# es segura para transacciones concurrentes. Serializar todo el authorize
# resuelve ambas. Volumen ~1 factura/semana: el costo de serializar es nulo.
_AUTHORIZE_LOCK = threading.Lock()


class ServiceError(RuntimeError):
    pass


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class StaleRegistryError(ConflictError):
    """DB local desactualizada frente a ARCA (§2.5): la única variante de
    conflicto que es forzable a conciencia con force_desync. Es un tipo
    propio para que la UI no tenga que mirar el texto del mensaje."""


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
    # Configuración de dominio (vive en la DB, se edita desde la UI)
    # ------------------------------------------------------------------

    def get_settings(self) -> Settings:
        # El emisor es el que factura contra el ambiente activo.
        return load_settings(self.conn, self.config.env)

    def update_settings(self, payload: SettingsIn) -> Settings:
        # SettingsIn ya llega stripeado (str_strip_whitespace): acá no se
        # vuelve a limpiar, solo se mapea.
        save_settings(
            self.conn,
            Settings(
                emisor=Emisor(
                    razon_social=payload.emisor_razon_social,
                    domicilio=payload.emisor_domicilio,
                    iibb=payload.emisor_iibb,
                    inicio_actividades=payload.emisor_inicio_actividades,
                    condicion_iva=payload.emisor_condicion_iva,
                    # La UI edita el emisor del ambiente activo; el alta de
                    # emisores con ambiente propio llega con su feature.
                    ambiente=self.config.env,
                    puntos_venta=tuple(payload.puntos_venta),
                ),
                backup_s3_bucket=payload.backup_s3_bucket,
                backup_s3_prefix=payload.backup_s3_prefix,
            ),
        )
        return self.get_settings()

    # ------------------------------------------------------------------
    # Parámetros (cache con refresh lazy de 24 h — design.md §6.2)
    # ------------------------------------------------------------------

    def get_params(self, kind: str) -> list[sqlite3.Row]:
        rows = repo.get_params(self.conn, kind)
        if rows:
            fetched = dt.datetime.fromisoformat(rows[0]["fetched_at"])
            if dt.datetime.now(dt.UTC) - fetched < PARAMS_TTL:
                return rows
        try:
            records = self.wsfex.get_param(kind)
            repo.replace_params(self.conn, kind, [r.as_dict() for r in records])
            return repo.get_params(self.conn, kind)
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
        # Sin datos de emisor no se emite: el PDF del comprobante los
        # necesita y la UI dirige a Configuración a completarlos.
        settings = self.get_settings()
        if not settings.emisor.completo:
            raise ConflictError(
                "Faltan los datos del emisor (razón social y domicilio): "
                "completarlos en Configuración antes de emitir."
            )
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

        items: list[dict[str, Any]]
        if payload.items:
            items = [
                {
                    "pro_codigo": i.pro_codigo,
                    "pro_ds": i.pro_ds,
                    "pro_qty": dec(i.pro_qty),
                    "pro_umed": i.pro_umed,
                    "pro_precio_uni": dec(i.pro_precio_uni),
                    "pro_total_item": dec(i.pro_qty * i.pro_precio_uni),
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
                    "pro_umed": UMED_UNIDADES,
                    "pro_precio_uni": dec(payload.imp_total),
                    "pro_total_item": dec(payload.imp_total),
                }
            ]

        total_items = sum(Decimal(i["pro_total_item"]) for i in items)
        if total_items != payload.imp_total:
            raise DomainError(
                f"imp_total {payload.imp_total} != suma de items {total_items}"
            )

        data = {
            "client_id": client["id"],
            "cbte_tipo": CBTE_TIPO_FACTURA_E,
            "punto_venta": settings.emisor.punto_venta,
            "fecha_cbte": fecha_cbte,
            "fecha_pago": fecha_pago,
            "tipo_expo": TIPO_EXPO_SERVICIOS,
            "permiso_existente": "",
            "dst_cmp": client["pais_dst"],
            # Snapshot del cliente: el comprobante queda inmutable aunque el
            # cliente se edite después (design.md §6.3).
            "cliente": client["razon_social"],
            "cuit_pais_cliente": client["cuit_pais"],
            "domicilio_cliente": client["domicilio"],
            "id_impositivo": client["id_impositivo"],
            "moneda_id": moneda_id,
            "moneda_ctz": dec(ctz),
            "incoterms": "",  # vacío en servicios (§0.1)
            "incoterms_ds": "",
            "forma_pago": client["forma_pago_default"],
            "idioma_cbte": client["idioma_default"],
            "imp_total": dec(payload.imp_total),
            "obs": payload.obs,
            "environment": self.config.env,
        }
        return repo.create_invoice(self.conn, data, items)

    def get_invoice(self, invoice_id: str, reconcile: bool = True) -> sqlite3.Row:
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:
            raise NotFoundError(f"Factura {invoice_id} no existe")
        if reconcile and inv["status"] == InvoiceStatus.UNKNOWN and inv["raw_request"]:
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
        # Serializa el authorize completo (ver _AUTHORIZE_LOCK): numeración
        # secuencial de ARCA + conexión SQLite compartida no reentrante.
        with _AUTHORIZE_LOCK:
            inv = repo.get_invoice(self.conn, invoice_id)
            if inv is None:
                raise NotFoundError(f"Factura {invoice_id} no existe")
            if inv["status"] == InvoiceStatus.AUTHORIZED:
                return inv  # idempotente: mismo CAE, sin tocar ARCA
            if inv["status"] == InvoiceStatus.REJECTED:
                raise ConflictError(
                    f"Factura rechazada por ARCA ({inv['last_error']}); "
                    "corregir los datos creando una nueva."
                )

            # Lock anti doble-submit de ESTA factura (regla 1).
            if not repo.try_transition_to_submitting(self.conn, invoice_id):
                raise ConflictError("Ya hay un authorize en curso para esta factura")

            inv = self._reload(invoice_id)
            try:
                if inv["raw_request"] is None:
                    return self._authorize_first_time(inv, force_desync)
                return self._authorize_retry(inv)
            except (ConflictError, ArcaUnavailableError):
                # No se llegó a enviar nada: volver a draft para no dejar el
                # lock tomado (raw_request sigue NULL).
                if inv["raw_request"] is None:
                    repo.update_invoice(
                        self.conn, invoice_id, status=InvoiceStatus.DRAFT
                    )
                raise

    def _authorize_first_time(
        self, inv: sqlite3.Row, force_desync: bool
    ) -> sqlite3.Row:
        invoice_id = inv["id"]
        try:
            last_cmp = self.wsfex.get_last_cmp(inv["punto_venta"], inv["cbte_tipo"])
            # El Id nuevo debe superar el último de ARCA Y las reservas
            # locales (facturas unknown/submitting que aún no llegaron a
            # ARCA); si no, colisiona con el UNIQUE de arca_id.
            arca_id = inv["arca_id"] or (
                max(self.wsfex.get_last_id(), repo.max_arca_id(self.conn)) + 1
            )
        except (WsfexError, httpx.HTTPError) as exc:
            raise ArcaUnavailableError(f"ARCA no disponible: {exc}") from exc

        # Detección de DB desactualizada (§2.5): si ARCA conoce comprobantes
        # que la DB local no tiene, bloquear (esquema multi-máquina).
        local_max = repo.max_authorized_cbte_nro(
            self.conn, inv["punto_venta"], inv["cbte_tipo"]
        )
        if last_cmp > local_max and not force_desync:
            raise StaleRegistryError(
                f"Registro local desactualizado: ARCA reporta último comprobante "
                f"{last_cmp} para PV {inv['punto_venta']} tipo {inv['cbte_tipo']} "
                f"pero la DB local llega a {local_max}. Restaurar el último "
                "backup antes de emitir (o forzar con force_desync=true si es "
                "intencional, p.ej. homologación)."
            )

        cbte_nro = last_cmp + 1
        wsfex_invoice = row_to_wsfex_invoice(
            inv, arca_id, cbte_nro, repo.get_invoice_items(self.conn, invoice_id)
        )
        # Regla 3: persistir el request completo ANTES de llamar. El try
        # atrapa una colisión del UNIQUE(arca_id) que, con el lock de
        # numeración, no debería ocurrir; si ocurriera (p.ej. restore de
        # backup a mitad de vuelo), se reporta como conflicto y el wrapper
        # devuelve la factura a draft en vez de dejarla colgada en submitting.
        try:
            repo.update_invoice(
                self.conn,
                invoice_id,
                arca_id=arca_id,
                cbte_nro=cbte_nro,
                raw_request=json.dumps(wsfex_invoice_to_raw(wsfex_invoice)),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"Colisión de numeración local (arca_id {arca_id} ya usado); "
                "reintentar la autorización."
            ) from exc
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
        wsfex_invoice = raw_to_wsfex_invoice(json.loads(inv["raw_request"]))
        return self._send(inv["id"], wsfex_invoice)

    def delete_draft(self, invoice_id: str) -> None:
        """Descarta un borrador que NUNCA llegó a ARCA (raw_request NULL).

        Cualquier otra cosa no se borra: una factura enviada (aun rechazada
        o unknown) es parte del registro y de la auditoría."""
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:
            raise NotFoundError(f"Factura {invoice_id} no existe")
        if inv["status"] != InvoiceStatus.DRAFT or inv["raw_request"] is not None:
            raise ConflictError(
                "Solo se pueden descartar borradores que nunca se enviaron a "
                f"ARCA (estado actual: {inv['status']})"
            )
        if not repo.delete_draft(self.conn, invoice_id):
            raise ConflictError(
                "El borrador cambió de estado mientras se descartaba; recargar"
            )

    def _reload(self, invoice_id: str) -> sqlite3.Row:
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:  # ya validada en authorize/get_invoice: no debe pasar
            raise RuntimeError(f"Factura {invoice_id} desapareció durante el flujo")
        return inv

    def _send(self, invoice_id: str, wsfex_invoice: Invoice) -> sqlite3.Row:
        try:
            result = self.wsfex.authorize(wsfex_invoice)
        except WsfexError as exc:
            # Respuesta concluyente de ARCA: rechazo con error legible.
            repo.update_invoice(
                self.conn,
                invoice_id,
                status=InvoiceStatus.REJECTED,
                last_error=f"ARCA {exc.code}: {exc.message}",
                raw_response=json.dumps({"error": exc.code, "message": exc.message}),
            )
            return self._reload(invoice_id)
        except httpx.HTTPError as exc:
            # Sin respuesta concluyente (timeout post-envío, corte, etc.):
            # unknown hasta reconciliar con FEXGetCMP (§1.5).
            repo.update_invoice(
                self.conn,
                invoice_id,
                status=InvoiceStatus.UNKNOWN,
                last_error=f"Sin respuesta de ARCA: {exc}",
            )
            return self._reload(invoice_id)

        # Verificación post-emisión (checklist punto 6).
        problemas = self.wsfex.verify_issued(wsfex_invoice, result)
        if problemas:
            repo.update_invoice(
                self.conn,
                invoice_id,
                status=InvoiceStatus.UNKNOWN,
                cae=result.cae,
                cae_fch_vto=result.cae_fch_vto,
                last_error="Verificación FEXGetCMP con discrepancias: "
                + "; ".join(problemas),
            )
            return self._reload(invoice_id)

        repo.update_invoice(
            self.conn,
            invoice_id,
            status=InvoiceStatus.AUTHORIZED,
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
        return self._reload(invoice_id)

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
        # (checklist punto 3). Se chequean Id E importe, y el importe NO es
        # redundante: en el esquema multi-máquina (§2.5), dos equipos que
        # restauran el mismo backup pueden reservar el MISMO arca_id y enviar
        # datos distintos al mismo número. ARCA guarda solo el primero; el
        # segundo, al reconciliar, ve un Id que coincide pero un importe que
        # no, y el chequeo de importe es lo único que evita que adopte un CAE
        # de un comprobante por otro monto.
        raw = json.loads(inv["raw_request"])
        id_registrado = registrado.get("Id")
        imp_registrado = registrado.get("Imp_total")
        es_propio = (
            id_registrado is not None
            and int(id_registrado) == raw["arca_id"]
            and imp_registrado is not None
            and Decimal(imp_registrado) == Decimal(raw["imp_total"])
        )
        if not es_propio:
            repo.update_invoice(
                self.conn,
                inv["id"],
                status=InvoiceStatus.UNKNOWN,
                last_error=(
                    f"ARCA registra el comprobante {inv['cbte_nro']} con "
                    f"Id={id_registrado} e importe {imp_registrado}, distintos "
                    f"de los enviados (Id={raw['arca_id']}, "
                    f"importe {raw['imp_total']}). Revisión manual requerida."
                ),
            )
            return self._reload(inv["id"])
        repo.update_invoice(
            self.conn,
            inv["id"],
            status=InvoiceStatus.AUTHORIZED,
            cae=cae,
            cae_fch_vto=registrado.get("Fch_venc_Cae"),
            last_error=None,
            raw_response=json.dumps({"reconciled_from": "FEXGetCMP", **registrado}),
        )
        logger.info(
            "Factura %s reconciliada vía FEXGetCMP: CAE recuperado", inv["id"]
        )
        return self._reload(inv["id"])

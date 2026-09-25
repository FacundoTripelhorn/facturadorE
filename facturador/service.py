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
from .arca.amounts import (
    IMP_TOTAL,
    MONEDA_CTZ,
    PRO_TOTAL_ITEM,
    ImporteFueraDeFormato,
    validar_importe,
)
from .arca.wsaa import WsaaError
from .arca.wscdc import (
    ConstatacionRequest,
    ConstatacionResult,
    WscdcClient,
    WscdcError,
)
from .arca.wsfex import (
    CmpNotFoundError,
    CmpRecord,
    Invoice,
    WsfexClient,
    WsfexError,
)
from .certs import CertificateError
from .config import Config
from .constants import (
    CBTE_TIPO_FACTURA_E,
    PDF_RENDER_VERSION,
    TIPO_DOC_CUIT,
    TIPO_EXPO_SERVICIOS,
    UMED_UNIDADES,
    InvoiceSource,
    InvoiceStatus,
)
from .fiscal_identity import FiscalIdentityError, resolve_profile_fiscal_cuit
from .mappers import (
    dec,
    raw_to_wsfex_invoice,
    row_to_wsfex_invoice,
    wsfex_invoice_to_raw,
)
from .profile import EnvironmentProfile
from .reconstruct import ReconstructError, ReconstructReport
from .restore import catch_up_register
from .schemas import (
    ArcaInvoiceItemOut,
    ArcaInvoiceOut,
    ArcaInvoicesOut,
    BackupSettingsIn,
    ClientIn,
    EmisorCreateIn,
    EmisorUpdateIn,
    InvoiceCreate,
)
from .seed_backup_sync import SeedBackupCoordinator, SeedBackupState
from .seed_import import SeedIdentityError
from .settings import (
    Emisor,
    Settings,
    get_active_emisor_id,
    load_settings,
    set_active_emisor,
)

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
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        wsfex: WsfexClient,
        seed_backup: SeedBackupCoordinator | None = None,
        wscdc: WscdcClient | None = None,
    ):
        self.config = config
        self.conn = conn
        self.wsfex = wsfex
        # Cliente WSCDC opcional en tests unitarios sin constatación.
        self.wscdc = wscdc or WscdcClient(config)
        # Opcional para tests unitarios del service sin coordinator.
        self.seed_backup = seed_backup

    def _notify_seed_backup(self, reason: str) -> None:
        """Config-change hook. Never raises; never called from authorize."""
        if self.seed_backup is None:
            return
        try:
            self.seed_backup.notify_config_changed(reason)
        except Exception:
            logger.exception(
                "Fallo al notificar seed backup (%s); el cambio de config "
                "ya quedó persistido",
                reason,
            )

    def notify_seed_backup(self, reason: str) -> None:
        """Public config-change hook (onboarding ready, recipients, tests)."""
        self._notify_seed_backup(reason)

    # ------------------------------------------------------------------
    # Configuración de dominio (vive en la DB, se edita desde la UI)
    # ------------------------------------------------------------------

    def get_settings(self) -> Settings:
        settings = load_settings(self.conn)
        self._check_emisor_profile(settings.emisor)
        return settings

    def _check_emisor_profile(self, emisor: Emisor) -> None:
        """Rechaza operar con un emisor activo de OTRO perfil.

        Contraparte de _check_invoice_profile: por construcción no debería
        pasar, pero si una DB ajena se restauró en el perfil equivocado,
        active_emisor_id puede apuntar a un emisor sellado con otro ambiente
        y ni settings ni emisión deben usarlo.
        """
        if emisor.id is not None and emisor.ambiente != self.config.env:
            raise ConflictError(
                f"El emisor activo {emisor.id} pertenece al ambiente "
                f"{emisor.ambiente} y este backend corre el perfil "
                f"{self.config.env}: la DB de este perfil contiene datos de "
                "otro. Restaurar cada backup en el perfil de su ambiente."
            )

    def update_backup_settings(self, payload: BackupSettingsIn) -> Settings:
        actual = self.get_settings()
        repo.save_settings(
            self.conn,
            {
                "backup_s3_bucket": payload.backup_s3_bucket,
                "backup_s3_prefix": payload.backup_s3_prefix,
            },
        )
        self._notify_seed_backup("ui_config")
        return Settings(
            emisor=actual.emisor,
            backup_s3_bucket=payload.backup_s3_bucket,
            backup_s3_prefix=payload.backup_s3_prefix,
        )

    def list_emisores(self) -> list[sqlite3.Row]:
        return repo.list_emisores(self.conn)

    def create_emisor(self, payload: EmisorCreateIn) -> sqlite3.Row:
        row = repo.create_emisor(
            self.conn,
            {
                "razon_social": payload.razon_social,
                "domicilio": payload.domicilio,
                "iibb": payload.iibb,
                "inicio_actividades": payload.inicio_actividades,
                "condicion_iva": payload.condicion_iva,
                # El ambiente NO es elegible por el usuario: todo emisor nace
                # sellado con el perfil corriente.
                "ambiente": self.config.env,
                "puntos_venta": json.dumps(list(payload.puntos_venta)),
            },
        )
        if get_active_emisor_id(self.conn) is None:
            set_active_emisor(self.conn, row["id"])
        self._notify_seed_backup("emisor")
        return row

    def update_emisor(self, emisor_id: str, payload: EmisorUpdateIn) -> sqlite3.Row:
        if repo.get_emisor(self.conn, emisor_id) is None:
            raise NotFoundError(f"Emisor {emisor_id} no existe")
        row = repo.update_emisor(
            self.conn,
            emisor_id,
            {
                "razon_social": payload.razon_social,
                "domicilio": payload.domicilio,
                "iibb": payload.iibb,
                "inicio_actividades": payload.inicio_actividades,
                "condicion_iva": payload.condicion_iva,
                "puntos_venta": json.dumps(list(payload.puntos_venta)),
            },
        )
        if row is None:
            raise NotFoundError(f"Emisor {emisor_id} no existe")
        self._notify_seed_backup("emisor")
        return row

    def activate_emisor(self, emisor_id: str) -> None:
        row = repo.get_emisor(self.conn, emisor_id)
        if row is None:
            raise NotFoundError(f"Emisor {emisor_id} no existe")
        if row["ambiente"] != self.config.env:
            raise ConflictError(
                f"El emisor {emisor_id} pertenece al ambiente "
                f"{row['ambiente']} y este backend corre el perfil "
                f"{self.config.env}: no se puede activar para emitir."
            )
        set_active_emisor(self.conn, emisor_id)
        self._notify_seed_backup("active_emisor")

    def get_seed_backup_state(self) -> SeedBackupState:
        """Estado queryable del seed backup."""
        if self.seed_backup is None:
            from .seed_backup_sync import load_seed_backup_state

            return load_seed_backup_state(self.conn)
        return self.seed_backup.get_state()

    def run_seed_backup_now(self) -> SeedBackupState:
        """Disparo manual / flush del seed backup."""
        if self.seed_backup is None:
            raise ConflictError(
                "Seed backup no está habilitado en este proceso."
            )
        return self.seed_backup.backup_now()

    def replace_seed_recipients(self, text: str) -> SeedBackupState:
        """Actualiza recipients.txt local y dispara backup."""
        if self.seed_backup is None:
            raise ConflictError(
                "Seed backup no está habilitado en este proceso."
            )
        return self.seed_backup.replace_recipients(text)

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
        except (WsfexError, httpx.HTTPError, CertificateError, OSError) as exc:
            # Cert ausente/inválido en refresh: mismo fallback que ARCA caído
            # (cache stale usable; sin cache → ArcaUnavailableError).
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

    def _param_description(self, kind: str, code: object) -> str:
        """Descripción de un código ARCA al momento del snapshot.

        Mejor esfuerzo: si el cache no trae descripción, queda vacío y el
        PDF imprime solo el código. No se relee en el render.
        """
        for row in self.get_params(kind):
            if row["code"] == str(code):
                return row["description"] or ""
        return ""

    # ------------------------------------------------------------------
    # Clientes
    # ------------------------------------------------------------------

    def create_client(self, payload: ClientIn) -> sqlite3.Row:
        self._validate_client_codes(payload)
        row = repo.create_client(self.conn, payload.model_dump())
        # Solo el cliente default viaja en el seed.
        if row["is_default"]:
            self._notify_seed_backup("default_client")
        return row

    def update_client(self, client_id: str, payload: ClientIn) -> sqlite3.Row:
        self._validate_client_codes(payload)
        before = repo.get_client(self.conn, client_id)
        was_default = bool(before["is_default"]) if before is not None else False
        row = repo.update_client(self.conn, client_id, payload.model_dump())
        if row is None:
            raise NotFoundError(f"Cliente {client_id} no existe")
        # No toca facturas: los datos viajan snapshoteados en cada invoice.
        if row["is_default"] or was_default:
            self._notify_seed_backup("default_client")
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
        if settings.emisor.id is None:
            raise ConflictError(
                "No hay emisor activo para emitir; elegir uno en Configuración."
            )
        # Identidad fiscal ANTES de cualquier WSFEX: get_param /
        # get_ctz también llevan Auth{Token,Sign,Cuit} del cert vivo.
        cuit_emisor = self._resolve_fiscal_cuit()

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

        try:
            ctz = validar_importe(ctz, MONEDA_CTZ)
        except ImporteFueraDeFormato as exc:
            raise DomainError(str(exc)) from None

        items: list[dict[str, Any]]
        if payload.items:
            items = []
            for nro, i in enumerate(payload.items, start=1):
                # qty y precio ya vienen acotados (12+6), así que el producto
                # es chico; lo que falta ver es que entre en 13 enteros y 2
                # decimales, como lo recibe ARCA.
                try:
                    total_item = validar_importe(
                        i.pro_qty * i.pro_precio_uni, PRO_TOTAL_ITEM
                    )
                except ImporteFueraDeFormato as exc:
                    raise DomainError(
                        f"Ítem {nro} (cantidad × precio): {exc}"
                    ) from None
                items.append(
                    {
                        "pro_codigo": i.pro_codigo,
                        "pro_ds": i.pro_ds,
                        "pro_qty": dec(i.pro_qty),
                        "pro_umed": i.pro_umed,
                        "pro_umed_ds": self._param_description("umed", i.pro_umed),
                        "pro_precio_uni": dec(i.pro_precio_uni),
                        "pro_total_item": dec(total_item),
                    }
                )
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
                    "pro_umed_ds": self._param_description("umed", UMED_UNIDADES),
                    "pro_precio_uni": dec(payload.imp_total),
                    "pro_total_item": dec(payload.imp_total),
                }
            ]

        total_items = sum(Decimal(i["pro_total_item"]) for i in items)
        if total_items != payload.imp_total:
            raise DomainError(
                f"imp_total {payload.imp_total} != suma de items {total_items}"
            )

        pvs = settings.emisor.puntos_venta
        if len(pvs) == 1:
            pv = pvs[0]
        elif payload.punto_venta is not None:
            pv = payload.punto_venta
        else:
            raise DomainError(
                "Indicar punto de venta: el emisor activo tiene más de uno habilitado"
            )
        if pv not in pvs:
            raise DomainError(
                f"Punto de venta {pv} no está habilitado para este emisor"
                f" ({', '.join(str(p) for p in pvs)})"
            )

        data = {
            "emisor_id": settings.emisor.id,
            "client_id": client["id"],
            "cbte_tipo": CBTE_TIPO_FACTURA_E,
            "punto_venta": pv,
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
            # Descripciones de params al crear el borrador: el PDF
            # regenera desde estos campos, nunca desde arca_params vivos.
            "dst_cmp_ds": self._param_description("pais", client["pais_dst"]),
            "cuit_pais_cliente_ds": self._param_description(
                "cuit_pais", client["cuit_pais"]
            ),
            "moneda_ds": self._param_description("moneda", moneda_id),
            "incoterms": "",  # vacío en servicios (§0.1)
            "incoterms_ds": "",
            "forma_pago": client["forma_pago_default"],
            "idioma_cbte": client["idioma_default"],
            "imp_total": dec(payload.imp_total),
            "obs": payload.obs,
            # Identidad fiscal del perfil: CUIT del certificado,
            # no editable vía emisor. Snapshot inmutable en el comprobante.
            "cuit_emisor": cuit_emisor,
            # Snapshot del emisor al crear el borrador: la revisión
            # y el PDF usan estos valores; editar emisores después no muda
            # el comprobante. emisor_id queda solo para trazabilidad.
            "emisor_razon_social": settings.emisor.razon_social,
            "emisor_domicilio": settings.emisor.domicilio,
            "emisor_condicion_iva": settings.emisor.condicion_iva,
            "emisor_iibb": settings.emisor.iibb,
            "emisor_inicio_actividades": settings.emisor.inicio_actividades,
            # Contrato de render; el registry despacha por versión.
            "pdf_render_version": PDF_RENDER_VERSION,
            # Auditoría inmutable (ADR 0001): el ambiente del
            # comprobante sale SIEMPRE del perfil corriente, nunca del
            # request, y después se valida en cada acceso por id.
            "environment": self.config.env,
        }
        return repo.create_invoice(self.conn, data, items)

    def _check_invoice_profile(self, inv: sqlite3.Row) -> None:
        """Rechaza el acceso a comprobantes de OTRO perfil.

        Por construcción no debería pasar (la DB es del perfil); si pasa, es
        una DB ajena restaurada en el perfil equivocado y ningún flujo debe
        operar sobre ese registro.
        """
        if inv["environment"] != self.config.env:
            raise ConflictError(
                f"La factura {inv['id']} pertenece al ambiente "
                f"{inv['environment']} y este backend corre el perfil "
                f"{self.config.env}: la DB de este perfil contiene datos de "
                "otro. Restaurar cada backup en el perfil de su ambiente."
            )

    def _profile(self) -> EnvironmentProfile:
        return EnvironmentProfile(
            environment=self.config.env, paths=self.config.paths
        )

    def _resolve_fiscal_cuit(self) -> str:
        """CUIT sellado del perfil; ConflictError si la identidad no cierra."""
        try:
            return resolve_profile_fiscal_cuit(self.conn, self._profile())
        except FiscalIdentityError as exc:
            raise ConflictError(str(exc)) from exc

    def _check_invoice_fiscal_identity(self, inv: sqlite3.Row) -> None:
        """El snapshot de la factura debe coincidir con el perfil.

        Se revalida antes de cualquier WSFEX (authorize y reconcile) para que
        un swap manual del certificado o del sello no opere bajo un CUIT
        distinto al del comprobante.
        """
        profile_cuit = self._resolve_fiscal_cuit()
        snap = inv["cuit_emisor"]
        if snap is None:
            raise ConflictError(
                f"La factura {inv['id']} no tiene CUIT fiscal snapshot; "
                "crear un borrador nuevo tras instalar el certificado del perfil."
            )
        if str(snap) != profile_cuit:
            raise ConflictError(
                f"La factura {inv['id']} tiene CUIT {snap} pero este perfil "
                f"está sellado al CUIT {profile_cuit}. Para cambiar de "
                "contribuyente hay que usar un perfil nuevo o resetear este "
                "(no hay migración automática de identidad fiscal)."
            )

    def get_invoice(self, invoice_id: str, reconcile: bool = True) -> sqlite3.Row:
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:
            raise NotFoundError(f"Factura {invoice_id} no existe")
        self._check_invoice_profile(inv)
        if reconcile and inv["status"] == InvoiceStatus.UNKNOWN and inv["raw_request"]:
            try:
                resolved = self._try_reconcile(inv)
            except (WsfexError, httpx.HTTPError):
                resolved = None  # ARCA no disponible: sigue unknown
            if resolved is not None:
                return resolved
        return inv

    def constatar_cae(self, invoice_id: str) -> ConstatacionResult:
        """Constatación WSCDC bajo demanda.

        Nunca se llama desde authorize / FEXGetCMP. El operador dispara el
        botón Constatar; el request sale del snapshot local (CUIT país = 80).
        """
        inv = self.get_invoice(invoice_id, reconcile=False)
        if inv["status"] != InvoiceStatus.AUTHORIZED:
            raise DomainError(
                "Solo se puede constatar una factura autorizada con CAE"
            )
        if inv["cbte_tipo"] != CBTE_TIPO_FACTURA_E:
            raise DomainError(
                "La constatación in-app solo cubre Factura E (tipo 19)"
            )
        cuit_raw = inv["cuit_emisor"]
        if cuit_raw:
            cuit_emisor = int(cuit_raw)
        else:
            # Snapshot sin CUIT fiscal (legado): caer al CUIT del certificado vivo.
            cuit_emisor = self.wsfex.cuit
        try:
            req = WscdcClient.request_from_invoice_row(
                inv, cuit_emisor=cuit_emisor
            )
        except WscdcError as exc:
            if exc.code == "local":
                raise DomainError(exc.message) from exc
            raise ArcaUnavailableError(f"WSCDC no disponible: {exc}") from exc
        return self._constatar(req)

    def constatar_cae_manual(
        self,
        *,
        punto_venta: int,
        cbte_nro: int,
        fecha_cbte: str,
        imp_total: Decimal,
        cae: str,
        doc_nro_receptor: str,
    ) -> ConstatacionResult:
        """Constatación WSCDC de un comprobante externo.

        Para Facturas E NO emitidas por la app (p.ej. Comprobantes en Línea
        u otro punto de venta): el operador tipea los mismos campos del
        portal. El CUIT emisor sale del certificado del perfil (identidad
        fiscal única); tipo 19, modo CAE y receptor CUIT (80) van
        fijos por alcance del producto.
        """
        if punto_venta <= 0 or cbte_nro <= 0:
            raise DomainError("Punto de venta y número deben ser positivos")
        try:
            dt.datetime.strptime(fecha_cbte, "%Y%m%d")
        except ValueError:
            raise DomainError("Fecha inválida: usar formato AAAAMMDD") from None
        # ImpTotal en WSCDC tiene el mismo formato que en WSFEX (13+2).
        try:
            imp_total = validar_importe(imp_total, IMP_TOTAL)
        except ImporteFueraDeFormato as exc:
            raise DomainError(str(exc)) from None
        if not (cae.isdigit() and len(cae) == 14):
            raise DomainError("El CAE debe tener 14 dígitos")
        if not doc_nro_receptor.isdigit():
            raise DomainError(
                "Doc. receptor: ingresar la CUIT país (solo números)"
            )
        # Identidad fiscal ANTES de WSCDC: resolver sella el CUIT
        # del perfil si esta es su primera operación fiscal, y después exige
        # sello/cert consistentes. Leer el cert directo saltaría ese control.
        cuit_emisor = int(self._resolve_fiscal_cuit())
        req = ConstatacionRequest(
            cuit_emisor=cuit_emisor,
            punto_venta=punto_venta,
            cbte_tipo=CBTE_TIPO_FACTURA_E,
            cbte_nro=cbte_nro,
            fecha_cbte=fecha_cbte,
            imp_total=imp_total,
            cae=cae,
            doc_tipo_receptor=str(TIPO_DOC_CUIT),
            doc_nro_receptor=doc_nro_receptor,
        )
        return self._constatar(req)

    def _constatar(self, req: ConstatacionRequest) -> ConstatacionResult:
        try:
            return self.wscdc.constatar(req)
        except WscdcError as exc:
            if exc.code == "local":
                raise DomainError(exc.message) from exc
            raise ArcaUnavailableError(f"WSCDC no disponible: {exc}") from exc
        except (WsaaError, httpx.HTTPError, OSError, CertificateError) as exc:
            raise ArcaUnavailableError(f"WSCDC no disponible: {exc}") from exc

    def list_arca_invoices(
        self,
        *,
        punto_venta: int | None = None,
        cbte_tipo: int = CBTE_TIPO_FACTURA_E,
        limit: int = 50,
        offset: int = 0,
        cbte_nro: int | None = None,
    ) -> ArcaInvoicesOut:
        """Peek de solo lectura del registro ARCA (sin escribir local).

        WSFEX no tiene listado: se consulta ``FEXGetLast_CMP`` y luego
        ``FEXGetCMP`` por número. ``offset`` cuenta desde el más reciente.
        """
        pv = self._resolve_peek_punto_venta(punto_venta)
        try:
            last_cmp = self.wsfex.get_last_cmp(pv, cbte_tipo)
        except (WsfexError, httpx.HTTPError, OSError) as exc:
            raise ArcaUnavailableError(f"ARCA no disponible: {exc}") from exc

        numbers = _arca_peek_numbers(
            last_cmp, limit=limit, offset=offset, cbte_nro=cbte_nro
        )
        invoices: list[ArcaInvoiceOut] = []
        gaps: list[int] = []
        for nro in numbers:
            try:
                record = self.wsfex.get_cmp_record(cbte_tipo, pv, nro)
            except CmpNotFoundError:
                gaps.append(nro)
                continue
            except (WsfexError, httpx.HTTPError, OSError) as exc:
                raise ArcaUnavailableError(
                    f"ARCA no disponible al consultar CMP {pv}/{cbte_tipo}/{nro}: "
                    f"{exc}"
                ) from exc
            invoices.append(_cmp_record_to_arca_out(record, fallback_nro=nro))
        return ArcaInvoicesOut(
            punto_venta=pv,
            cbte_tipo=cbte_tipo,
            last_cmp=last_cmp,
            invoices=invoices,
            gaps=gaps,
        )

    def _resolve_peek_punto_venta(self, punto_venta: int | None) -> int:
        settings = load_settings(self.conn)
        pvs = settings.emisor.puntos_venta
        if punto_venta is not None:
            if punto_venta < 1:
                raise DomainError("punto_venta debe ser >= 1")
            return punto_venta
        if len(pvs) == 1:
            return pvs[0]
        raise DomainError(
            "Indicar punto_venta: el emisor activo tiene más de uno habilitado"
        )

    # ------------------------------------------------------------------
    # Authorize (reglas §2.3)
    # ------------------------------------------------------------------

    def catch_up_from_arca(self) -> ReconstructReport:
        """Append N_local+1..N_arca en lotes.

        Remediación cuando el guard de registro desactualizado bloquea la
        emisión. Solo append (no wipe); usa conexión dedicada.
        """
        with _AUTHORIZE_LOCK:
            profile = EnvironmentProfile(
                environment=self.config.env, paths=self.config.paths
            )
            try:
                return catch_up_register(profile, self.wsfex)
            except ReconstructError as exc:
                raise ConflictError(str(exc)) from exc
            except SeedIdentityError as exc:
                raise ConflictError(str(exc)) from exc
            except (CertificateError, httpx.HTTPError, OSError) as exc:
                raise ArcaUnavailableError(
                    f"No se pudo sincronizar desde ARCA: {exc}"
                ) from exc

    def authorize(self, invoice_id: str, force_desync: bool = False) -> sqlite3.Row:
        # Serializa el authorize completo (ver _AUTHORIZE_LOCK): numeración
        # secuencial de ARCA + conexión SQLite compartida no reentrante.
        with _AUTHORIZE_LOCK:
            inv = repo.get_invoice(self.conn, invoice_id)
            if inv is None:
                raise NotFoundError(f"Factura {invoice_id} no existe")
            self._check_invoice_profile(inv)
            if inv["status"] == InvoiceStatus.AUTHORIZED:
                return inv  # idempotente: mismo CAE, sin tocar ARCA
            if inv["status"] == InvoiceStatus.REJECTED:
                raise ConflictError(
                    f"Factura rechazada por ARCA ({inv['last_error']}); "
                    "corregir los datos creando una nueva."
                )
            if self._invoice_source(inv) == InvoiceSource.IMPORTED:
                raise ConflictError(
                    "Los comprobantes importados son solo lectura histórica; "
                    "no se pueden autorizar ni reenviar a ARCA."
                )
            # Identidad fiscal antes de cualquier WSFEX.
            self._check_invoice_fiscal_identity(inv)

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

    @staticmethod
    def _invoice_source(inv: sqlite3.Row) -> str:
        if "source" in inv.keys() and inv["source"]:
            return str(inv["source"])
        return InvoiceSource.WSFEX

    def _assert_local_registry_consistent(
        self,
        inv: sqlite3.Row,
        last_cmp: int,
        *,
        force_desync: bool,
    ) -> None:
        """design.md §2.5: local wsfex vs FEXGetLast_CMP.

        Solo cuentan filas ``source=wsfex`` autorizadas. Si ARCA está adelante,
        el perfil local está desactualizado (otra máquina / backup viejo).
        Si el local está adelante de ARCA, el registro es inconsistente.
        """
        local_max = repo.max_authorized_cbte_nro(
            self.conn, inv["punto_venta"], inv["cbte_tipo"]
        )
        pv = inv["punto_venta"]
        tipo = inv["cbte_tipo"]
        if last_cmp > local_max and not force_desync:
            raise StaleRegistryError(
                f"Registro local desactualizado: ARCA reporta último comprobante "
                f"{last_cmp} para PV {pv} tipo {tipo} pero el registro local "
                f"autorizado (wsfex) llega a {local_max}. Sincronizar desde ARCA "
                "(catch-up) antes de emitir (o forzar con force_desync=true si es "
                "intencional, p.ej. homologación)."
            )
        if local_max > last_cmp:
            raise ConflictError(
                f"Registro local inconsistente: la DB local tiene comprobante "
                f"{local_max} para PV {pv} tipo {tipo} pero ARCA reporta último "
                f"{last_cmp}. No emitir: sincronizar o investigar antes de "
                "continuar."
            )

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

        # Antes de asignar número / persistir raw_request / llamar FEXAuthorize.
        self._assert_local_registry_consistent(
            inv, last_cmp, force_desync=force_desync
        )

        cbte_nro = last_cmp + 1
        wsfex_invoice = row_to_wsfex_invoice(
            inv, arca_id, cbte_nro, repo.get_invoice_items(self.conn, invoice_id)
        )
        # Los importes se validan al crear el borrador; esto cubre filas que
        # no pasaron por ahí (datos viejos o editados a mano). Antes de
        # persistir raw_request, así el wrapper devuelve la factura a draft.
        try:
            wsfex_invoice.validate()
        except ValueError as exc:
            raise ConflictError(
                f"El borrador no se puede enviar a ARCA: {exc}"
            ) from None
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
        try:
            wsfex_invoice.validate()
        except ValueError as exc:
            # Request guardado por una versión anterior, fuera del formato de
            # ARCA: no se reenvía. Queda "a reconciliar" (no trabada en
            # submitting); un nuevo intento vuelve a consultar ARCA primero.
            mensaje = (
                f"El request guardado no cumple el formato de ARCA ({exc}); "
                "no se reenvía. Revisar el comprobante manualmente."
            )
            repo.update_invoice(
                self.conn,
                inv["id"],
                status=InvoiceStatus.UNKNOWN,
                last_error=mensaje,
            )
            raise ConflictError(mensaje) from None
        return self._send(inv["id"], wsfex_invoice)

    def delete_draft(self, invoice_id: str) -> None:
        """Descarta un borrador que NUNCA llegó a ARCA (raw_request NULL).

        Cualquier otra cosa no se borra: una factura enviada (aun rechazada
        o unknown) es parte del registro y de la auditoría."""
        inv = repo.get_invoice(self.conn, invoice_id)
        if inv is None:
            raise NotFoundError(f"Factura {invoice_id} no existe")
        self._check_invoice_profile(inv)
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
        # FEXGetCMP autentica con Auth.Cuit del cert vivo.
        self._check_invoice_fiscal_identity(inv)
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


def _arca_peek_numbers(
    last_cmp: int,
    *,
    limit: int,
    offset: int,
    cbte_nro: int | None,
) -> list[int]:
    """Números a consultar: uno explícito, o ventana desde el más reciente."""
    if cbte_nro is not None:
        if cbte_nro < 1:
            raise DomainError("cbte_nro debe ser >= 1")
        return [cbte_nro]
    if last_cmp < 1:
        return []
    high = last_cmp - offset
    if high < 1:
        return []
    low = max(1, high - limit + 1)
    return list(range(high, low - 1, -1))


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _cmp_record_to_arca_out(
    record: CmpRecord, *, fallback_nro: int
) -> ArcaInvoiceOut:
    f = record.fields
    cbte_tipo = _optional_int(f.get("Cbte_tipo") or f.get("Cbte_Tipo")) or 0
    punto_venta = _optional_int(f.get("Punto_vta")) or 0
    cbte_nro = _optional_int(f.get("Cbte_nro")) or fallback_nro
    items = [
        ArcaInvoiceItemOut(
            pro_codigo=item.pro_codigo,
            pro_ds=item.pro_ds,
            pro_qty=item.pro_qty,
            pro_umed=item.pro_umed,
            pro_precio_uni=item.pro_precio_uni,
            pro_total_item=item.pro_total_item,
            pro_bonificacion=item.pro_bonificacion,
        )
        for item in record.items
    ]
    return ArcaInvoiceOut(
        cbte_tipo=cbte_tipo,
        punto_venta=punto_venta,
        cbte_nro=cbte_nro,
        arca_id=_optional_int(f.get("Id")),
        cae=f.get("Cae"),
        cae_fch_vto=f.get("Fch_venc_Cae"),
        fecha_cbte=f.get("Fecha_cbte"),
        fecha_pago=f.get("Fecha_pago"),
        tipo_expo=_optional_int(f.get("Tipo_expo")),
        dst_cmp=_optional_int(f.get("Dst_cmp")),
        cliente=f.get("Cliente"),
        cuit_pais_cliente=_optional_int(f.get("Cuit_pais_cliente")),
        domicilio_cliente=f.get("Domicilio_cliente"),
        id_impositivo=f.get("Id_impositivo"),
        moneda_id=f.get("Moneda_Id"),
        moneda_ctz=f.get("Moneda_ctz"),
        imp_total=f.get("Imp_total"),
        forma_pago=f.get("Forma_pago"),
        incoterms=f.get("Incoterms"),
        idioma_cbte=_optional_int(f.get("Idioma_cbte")),
        obs=f.get("Obs"),
        items=items,
    )

"""Reconstrucción del registro local desde ARCA (FAC-65).

ARCA es el ledger autoritativo. Este módulo re-consulta ``FEXGetLast_CMP`` +
``FEXGetCMP`` por cada (PV, tipo) y escribe el registro local en **lotes
cortos**. Dos modos comparten el loop:

* **full** — vacía el registro y recorre ``1..N`` (restore desde launcher).
* **catch-up** — append ``N_local+1..N_arca`` (remediación FAC-48).

Full rebuild **borra todo el registro local**, incluidos históricos
``source=imported``: ARCA es autoritativo; lo reinsertado es ``source=wsfex``.

Escrituras por lote (sin resume)::

1. Fetch del lote **fuera** de cualquier transacción (red nunca dentro).
2. Transacción corta: insert / gap → COMMIT (milisegundos).
3. Validación ``local_max == FEXGetLast_CMP`` **después** del último lote.

Un fallo a mitad de camino deja lotes previos commitidos. Ese registro
parcial tiene ``local_max < arca_last`` → el guard FAC-48 bloquea emisión
(fail-closed). Remedio: re-ejecutar el restore (full wipe + rebuild), no
reanudar.

Gaps confirmados por ARCA (``CmpNotFoundError`` / ErrCode 1521) se
persisten en ``registry_gaps``. Errores transitorios: reintento acotado y
**abort** (nunca un gap inventado).
"""

from __future__ import annotations

import enum
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import repo
from .arca.wsfex import CmpItem, CmpNotFoundError, CmpRecord, WsfexClient, WsfexError
from .constants import (
    PDF_RENDER_VERSION,
    UMED_UNIDADES,
    InvoiceSource,
    InvoiceStatus,
)
from .repo._common import new_id, now
from .settings import get_active_emisor_id, load_emisor

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 3
DEFAULT_RETRY_SLEEP_S = 0.5
DEFAULT_BATCH_SIZE = 25


class ReconstructMode(enum.StrEnum):
    FULL = "full"
    CATCH_UP = "catch_up"


class ReconstructError(RuntimeError):
    """Fallo del rebuild; lotes previos pueden haber quedado commitidos."""


@dataclass(frozen=True)
class RegistryGap:
    punto_venta: int
    cbte_tipo: int
    cbte_nro: int


@dataclass(frozen=True)
class PvTipoTarget:
    punto_venta: int
    cbte_tipo: int


@dataclass
class ReconstructReport:
    mode: ReconstructMode
    inserted: int = 0
    gaps: list[RegistryGap] = field(default_factory=list)
    last_cmp: dict[tuple[int, int], int] = field(default_factory=dict)
    ranges: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileSnapshot:
    """Datos de perfil que ARCA no devuelve (vienen del seed / emisor activo)."""

    environment: str
    cuit_emisor: str
    emisor_id: str | None
    emisor_razon_social: str
    emisor_domicilio: str
    emisor_condicion_iva: str
    emisor_iibb: str
    emisor_inicio_actividades: str
    pdf_render_version: int = PDF_RENDER_VERSION


ParamLookup = Callable[[str, object], str]

# Un ítem del plan: (target, nro) a consultar.
_WorkItem = tuple[PvTipoTarget, int]


def targets_from_seed(
    seed: Mapping[str, Any],
) -> list[PvTipoTarget]:
    """(PV, tipo) a reconsultar: producto cartesiano de PV del emisor activo
    (o todos) × ``comprobante_tipos`` del seed."""
    tipos_raw = seed.get("comprobante_tipos") or []
    tipos = [int(t) for t in tipos_raw]
    if not tipos:
        raise ReconstructError("El seed no lista comprobante_tipos.")

    pvs: list[int] = []
    active = seed.get("active_emisor_id")
    for emisor in seed.get("emisores") or []:
        if not isinstance(emisor, Mapping):
            continue
        if active and emisor.get("id") != active:
            continue
        for pv in emisor.get("puntos_venta") or []:
            if isinstance(pv, int) and pv >= 1 and pv not in pvs:
                pvs.append(pv)
    if not pvs:
        for emisor in seed.get("emisores") or []:
            if not isinstance(emisor, Mapping):
                continue
            for pv in emisor.get("puntos_venta") or []:
                if isinstance(pv, int) and pv >= 1 and pv not in pvs:
                    pvs.append(pv)
    if not pvs:
        raise ReconstructError("El seed no tiene puntos de venta.")

    return [PvTipoTarget(pv, tipo) for pv in pvs for tipo in tipos]


def targets_from_conn(conn: sqlite3.Connection) -> list[PvTipoTarget]:
    """Targets desde el emisor activo + tipos default del producto."""
    from .seed_backup import DEFAULT_COMPROBANTE_TIPOS

    active_id = get_active_emisor_id(conn)
    if not active_id:
        raise ReconstructError("No hay emisor activo para determinar PV/tipos.")
    emisor = load_emisor(conn, active_id)
    return [
        PvTipoTarget(pv, tipo)
        for pv in emisor.puntos_venta
        for tipo in DEFAULT_COMPROBANTE_TIPOS
    ]


def profile_snapshot_from_conn(
    conn: sqlite3.Connection,
    *,
    environment: str,
    cuit_emisor: str,
) -> ProfileSnapshot:
    active_id = get_active_emisor_id(conn)
    if active_id:
        emisor = load_emisor(conn, active_id)
        return ProfileSnapshot(
            environment=environment,
            cuit_emisor=cuit_emisor,
            emisor_id=emisor.id,
            emisor_razon_social=emisor.razon_social,
            emisor_domicilio=emisor.domicilio,
            emisor_condicion_iva=emisor.condicion_iva,
            emisor_iibb=emisor.iibb,
            emisor_inicio_actividades=emisor.inicio_actividades,
        )
    return ProfileSnapshot(
        environment=environment,
        cuit_emisor=cuit_emisor,
        emisor_id=None,
        emisor_razon_social="",
        emisor_domicilio="",
        emisor_condicion_iva="",
        emisor_iibb="",
        emisor_inicio_actividades="",
    )


def _fetch_cmp_with_retries(
    wsfex: WsfexClient,
    cbte_tipo: int,
    punto_venta: int,
    cbte_nro: int,
    *,
    retries: int,
    sleep_s: float,
) -> CmpRecord | None:
    """Devuelve el CmpRecord, ``None`` si gap confirmado, o aborta."""
    last_exc: BaseException | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            return wsfex.get_cmp_record(cbte_tipo, punto_venta, cbte_nro)
        except CmpNotFoundError:
            return None
        except (WsfexError, httpx.HTTPError, OSError) as exc:
            last_exc = exc
            logger.warning(
                "FEXGetCMP (%s,%s,%s) intento %s/%s falló: %s",
                punto_venta,
                cbte_tipo,
                cbte_nro,
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                time.sleep(sleep_s)
    raise ReconstructError(
        f"ARCA no respondió al consultar PV {punto_venta} tipo {cbte_tipo} "
        f"nro {cbte_nro} tras {attempts} intentos: {last_exc}"
    ) from last_exc


def _cmp_to_invoice_payload(
    record: CmpRecord,
    *,
    snapshot: ProfileSnapshot,
    param_lookup: ParamLookup | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    f = record.fields
    lookup = param_lookup or (lambda _kind, _code: "")

    def req(tag: str, *alts: str) -> str:
        for name in (tag, *alts):
            if name in f and f[name] != "":
                return f[name]
        raise ReconstructError(f"FEXGetCMP sin campo obligatorio {tag}.")

    cbte_tipo = int(req("Cbte_tipo", "Cbte_Tipo"))
    punto_venta = int(req("Punto_vta"))
    cbte_nro = int(req("Cbte_nro"))
    moneda_id = req("Moneda_Id")
    dst_cmp = int(req("Dst_cmp"))
    cuit_pais = int(req("Cuit_pais_cliente"))
    fecha_cbte = req("Fecha_cbte")
    fecha_pago = f.get("Fecha_pago") or fecha_cbte

    items_src = list(record.items)
    if not items_src:
        # Fallback: factura de una línea con el total (homologación mínima).
        items_src = [
            CmpItem(
                pro_codigo="0001",
                pro_ds=f.get("Obs") or "Ítem reconstruido",
                pro_qty="1",
                pro_umed=str(UMED_UNIDADES),
                pro_precio_uni=req("Imp_total"),
                pro_total_item=req("Imp_total"),
            )
        ]

    items: list[dict[str, Any]] = []
    for item in items_src:
        umed = int(item.pro_umed or UMED_UNIDADES)
        items.append(
            {
                "pro_codigo": item.pro_codigo or "0001",
                "pro_ds": item.pro_ds,
                "pro_qty": item.pro_qty,
                "pro_umed": umed,
                "pro_umed_ds": lookup("umed", umed),
                "pro_precio_uni": item.pro_precio_uni,
                "pro_total_item": item.pro_total_item,
            }
        )

    arca_id_raw = f.get("Id")
    data = {
        "emisor_id": snapshot.emisor_id,
        "client_id": None,
        "arca_id": int(arca_id_raw) if arca_id_raw else None,
        "cbte_tipo": cbte_tipo,
        "punto_venta": punto_venta,
        "cbte_nro": cbte_nro,
        "status": InvoiceStatus.AUTHORIZED,
        "source": InvoiceSource.WSFEX,
        "fecha_cbte": fecha_cbte,
        "fecha_pago": fecha_pago,
        "tipo_expo": int(f.get("Tipo_expo") or 2),
        "permiso_existente": f.get("Permiso_existente") or "",
        "dst_cmp": dst_cmp,
        "dst_cmp_ds": lookup("pais", dst_cmp),
        "cliente": req("Cliente"),
        "cuit_pais_cliente": cuit_pais,
        "cuit_pais_cliente_ds": lookup("cuit_pais", cuit_pais),
        "domicilio_cliente": f.get("Domicilio_cliente") or "",
        "id_impositivo": f.get("Id_impositivo") or "",
        "moneda_id": moneda_id,
        "moneda_ds": lookup("moneda", moneda_id),
        "moneda_ctz": req("Moneda_ctz"),
        "incoterms": f.get("Incoterms") or "",
        "incoterms_ds": f.get("Incoterms_Ds") or "",
        "forma_pago": f.get("Forma_pago") or "",
        "idioma_cbte": int(f.get("Idioma_cbte") or 1),
        "imp_total": req("Imp_total"),
        "obs": f.get("Obs") or "",
        "cae": req("Cae"),
        "cae_fch_vto": f.get("Fch_venc_Cae") or "",
        "raw_request": None,
        "raw_response": json.dumps(
            {"reconstructed_from": "FEXGetCMP", **f},
            ensure_ascii=False,
        ),
        "last_error": None,
        "cuit_emisor": snapshot.cuit_emisor,
        "emisor_razon_social": snapshot.emisor_razon_social,
        "emisor_domicilio": snapshot.emisor_domicilio,
        "emisor_condicion_iva": snapshot.emisor_condicion_iva,
        "emisor_iibb": snapshot.emisor_iibb,
        "emisor_inicio_actividades": snapshot.emisor_inicio_actividades,
        "pdf_render_version": snapshot.pdf_render_version,
        "environment": snapshot.environment,
    }
    return data, items


def _insert_authorized(
    conn: sqlite3.Connection,
    data: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
) -> str:
    """Inserta una factura authorized sin autocommit (participa del lote)."""
    invoice_id = new_id()
    ts = now()
    columns = (
        "id",
        "emisor_id",
        "client_id",
        "arca_id",
        "cbte_tipo",
        "punto_venta",
        "cbte_nro",
        "status",
        "source",
        "fecha_cbte",
        "fecha_pago",
        "tipo_expo",
        "permiso_existente",
        "dst_cmp",
        "dst_cmp_ds",
        "cliente",
        "cuit_pais_cliente",
        "cuit_pais_cliente_ds",
        "domicilio_cliente",
        "id_impositivo",
        "moneda_id",
        "moneda_ds",
        "moneda_ctz",
        "incoterms",
        "incoterms_ds",
        "forma_pago",
        "idioma_cbte",
        "imp_total",
        "obs",
        "cae",
        "cae_fch_vto",
        "raw_request",
        "raw_response",
        "last_error",
        "cuit_emisor",
        "emisor_razon_social",
        "emisor_domicilio",
        "emisor_condicion_iva",
        "emisor_iibb",
        "emisor_inicio_actividades",
        "pdf_render_version",
        "environment",
        "created_at",
        "updated_at",
    )
    values = [invoice_id] + [data[c] for c in columns[1:-2]] + [ts, ts]
    conn.execute(
        f"INSERT INTO invoices ({', '.join(columns)})"
        f" VALUES ({', '.join('?' * len(columns))})",
        values,
    )
    for item in items:
        conn.execute(
            "INSERT INTO invoice_items"
            " (id, invoice_id, pro_codigo, pro_ds, pro_qty, pro_umed,"
            "  pro_umed_ds, pro_precio_uni, pro_total_item)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                invoice_id,
                item["pro_codigo"],
                item["pro_ds"],
                item["pro_qty"],
                item["pro_umed"],
                item.get("pro_umed_ds", ""),
                item["pro_precio_uni"],
                item["pro_total_item"],
            ),
        )
    return invoice_id


def _clear_register(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM invoice_items")
    conn.execute("DELETE FROM invoices")
    conn.execute("DELETE FROM registry_gaps")


def _note_gap(conn: sqlite3.Connection, gap: RegistryGap) -> None:
    conn.execute(
        "INSERT INTO registry_gaps"
        " (punto_venta, cbte_tipo, cbte_nro, noted_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(punto_venta, cbte_tipo, cbte_nro) DO UPDATE SET"
        " noted_at = excluded.noted_at",
        (gap.punto_venta, gap.cbte_tipo, gap.cbte_nro, now()),
    )


def _run_write_txn(
    conn: sqlite3.Connection, fn: Callable[[sqlite3.Connection], None]
) -> None:
    """Transacción corta: no debe hacer I/O de red adentro."""
    previous = conn.isolation_level
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.isolation_level = previous


def reconstruct_register(
    conn: sqlite3.Connection,
    wsfex: WsfexClient,
    *,
    mode: ReconstructMode,
    targets: Sequence[PvTipoTarget],
    snapshot: ProfileSnapshot,
    param_lookup: ParamLookup | None = None,
    retries: int = DEFAULT_RETRIES,
    retry_sleep_s: float = DEFAULT_RETRY_SLEEP_S,
    abort_on_any_gap: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_begin: Callable[[sqlite3.Connection], None] | None = None,
) -> ReconstructReport:
    """Rebuild o catch-up por lotes (fetch fuera de txn; writes cortas).

    ``on_begin`` (p.ej. ``apply_seed``) corre en una txn corta **antes** de
    los fetches, tras el clear en modo full. La validación last-CMP es al
    final. Un abort a mitad deja lotes previos; re-ejecutar el restore.
    """
    if not targets:
        raise ReconstructError("Sin targets (PV, tipo) para reconstruir.")
    if batch_size < 1:
        raise ReconstructError(f"batch_size inválido: {batch_size}")

    planned: list[tuple[PvTipoTarget, int, int, int]] = []
    work: list[_WorkItem] = []
    report = ReconstructReport(mode=mode)

    for target in targets:
        try:
            arca_last = wsfex.get_last_cmp(target.punto_venta, target.cbte_tipo)
        except (WsfexError, httpx.HTTPError, OSError) as exc:
            raise ReconstructError(
                f"No se pudo obtener FEXGetLast_CMP para PV "
                f"{target.punto_venta} tipo {target.cbte_tipo}: {exc}"
            ) from exc
        if mode is ReconstructMode.FULL:
            start, end = 1, arca_last
        else:
            local_max = repo.max_authorized_cbte_nro(
                conn, target.punto_venta, target.cbte_tipo
            )
            start, end = local_max + 1, arca_last
            if local_max > arca_last:
                raise ReconstructError(
                    f"Registro local inconsistente: PV {target.punto_venta} "
                    f"tipo {target.cbte_tipo} local={local_max} > "
                    f"ARCA={arca_last}."
                )
        planned.append((target, start, end, arca_last))
        report.last_cmp[(target.punto_venta, target.cbte_tipo)] = arca_last
        report.ranges[(target.punto_venta, target.cbte_tipo)] = (start, end)
        if end >= start:
            for nro in range(start, end + 1):
                work.append((target, nro))

    def _prepare(c: sqlite3.Connection) -> None:
        if mode is ReconstructMode.FULL:
            _clear_register(c)
        if on_begin is not None:
            on_begin(c)

    _run_write_txn(conn, _prepare)

    for offset in range(0, len(work), batch_size):
        batch = work[offset : offset + batch_size]
        # Fetch fuera de transacción.
        fetched: list[
            tuple[PvTipoTarget, int, CmpRecord | None]
        ] = []
        for target, nro in batch:
            record = _fetch_cmp_with_retries(
                wsfex,
                target.cbte_tipo,
                target.punto_venta,
                nro,
                retries=retries,
                sleep_s=retry_sleep_s,
            )
            if record is None and abort_on_any_gap:
                raise ReconstructError(
                    f"Hueco en PV {target.punto_venta} tipo "
                    f"{target.cbte_tipo} nro {nro}: abort_on_any_gap."
                )
            fetched.append((target, nro, record))

        def _write_batch(
            c: sqlite3.Connection,
            *,
            _fetched: list[tuple[PvTipoTarget, int, CmpRecord | None]] = fetched,
        ) -> None:
            for target, nro, record in _fetched:
                if record is None:
                    gap = RegistryGap(
                        target.punto_venta, target.cbte_tipo, nro
                    )
                    _note_gap(c, gap)
                    report.gaps.append(gap)
                    continue
                data, items = _cmp_to_invoice_payload(
                    record,
                    snapshot=snapshot,
                    param_lookup=param_lookup,
                )
                data["punto_venta"] = target.punto_venta
                data["cbte_tipo"] = target.cbte_tipo
                data["cbte_nro"] = nro
                _insert_authorized(c, data, items)
                report.inserted += 1

        _run_write_txn(conn, _write_batch)

    # Validación final (después del último lote).
    for target, _start, _end, arca_last in planned:
        local_max = repo.max_authorized_cbte_nro(
            conn, target.punto_venta, target.cbte_tipo
        )
        if local_max != arca_last:
            raise ReconstructError(
                f"Validación post-rebuild falló: PV "
                f"{target.punto_venta} tipo {target.cbte_tipo} "
                f"local_max={local_max} != FEXGetLast_CMP={arca_last}. "
                "Re-ejecutar el restore (el registro parcial queda bloqueado "
                "por FAC-48)."
            )

    return report

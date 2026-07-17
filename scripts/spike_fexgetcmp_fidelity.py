"""FAC-63 operator smoke: emit (or re-query) a Factura E and diff FEXGetCMP
against the Cmp fields required to regenerate the PDF.

Requires a Homologación profile with a WSASS-registered cert authorized for
``wsfex`` and seeded ``arca_params`` (run ``scripts/check_wsfex.py`` first).

Usage:
  ARCA_ENV=homo uv run python scripts/spike_fexgetcmp_fidelity.py
  ARCA_ENV=homo uv run python scripts/spike_fexgetcmp_fidelity.py \\
      --pv 1 --nro 3 --fecha-pago 20260718

See docs/spike-factura-e-arca.md for the recorded finding.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facturador import db, repo
from facturador.arca.wsfex import Invoice, InvoiceItem, WsfexClient
from facturador.config import load_config, resolve_boot_profile
from facturador.constants import CBTE_TIPO_FACTURA_E, MONEDA_DOL

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Cmp tags the PDF / QR need (docs/spike-factura-e-arca.md §A).
PDF_CMP_FIELDS = (
    "Cbte_tipo",
    "Punto_vta",
    "Cbte_nro",
    "Fecha_cbte",
    "Fecha_pago",
    "Cliente",
    "Domicilio_cliente",
    "Cuit_pais_cliente",
    "Id_impositivo",
    "Dst_cmp",
    "Moneda_Id",
    "Moneda_ctz",
    "Forma_pago",
    "Incoterms",
    "Imp_total",
    "Cae",
    "Fch_venc_Cae",
    "Pro_codigo",
    "Pro_ds",
    "Pro_qty",
    "Pro_umed",
    "Pro_precio_uni",
    "Pro_total_item",
)

LOCAL_ONLY_REMINDER = (
    "emisor_* snapshot fields, cuit_emisor, environment, *_ds labels, "
    "pdf_render_version — see docs/spike-factura-e-arca.md §B"
)


def _lookup_param(conn, kind: str, description_prefix: str) -> int:
    filas = [
        f
        for f in repo.get_params(conn, kind)
        if (f["description"] or "").upper().startswith(description_prefix.upper())
    ]
    if not filas:
        raise SystemExit(
            f"No se encontró {kind} que empiece con {description_prefix!r}. "
            "Correr antes: uv run python scripts/check_wsfex.py"
        )
    return int(filas[0]["code"])


def _authorize_representative(client: WsfexClient, conn, pv: int) -> Invoice:
    hoy = dt.date.today().strftime("%Y%m%d")
    manana = (dt.date.today() + dt.timedelta(days=1)).strftime("%Y%m%d")
    dst_cmp = _lookup_param(conn, "pais", "URUGUAY")
    cuit_pais = _lookup_param(conn, "cuit_pais", "URUGUAY - Persona Jur")
    ctz, ctz_fecha = client.get_ctz(MONEDA_DOL)
    print(f"Cotización DOL (ARCA, {ctz_fecha}): {ctz}")
    arca_id = client.get_last_id() + 1
    cbte_nro = client.get_last_cmp(pv, CBTE_TIPO_FACTURA_E) + 1
    imp_total = Decimal("100.00")
    invoice = Invoice(
        arca_id=arca_id,
        fecha_cbte=hoy,
        punto_vta=pv,
        cbte_nro=cbte_nro,
        dst_cmp=dst_cmp,
        cliente="CLIENTE SPIKE FAC-63 S.A.",
        cuit_pais_cliente=cuit_pais,
        domicilio_cliente="Av. Spike 63, Montevideo",
        id_impositivo="RUT 219999830019",
        moneda_ctz=ctz,
        imp_total=imp_total,
        fecha_pago=manana,  # deliberately != fecha_cbte
        forma_pago="WIRE TRANSFER",
        items=[
            InvoiceItem(
                pro_ds="Servicios de desarrollo de software (FAC-63 spike)",
                pro_precio_uni=imp_total,
            )
        ],
    )
    print(
        f"Autorizando tipo 19 {pv:05d}-{cbte_nro:08d} "
        f"fecha_cbte={hoy} fecha_pago={manana} Id={arca_id}"
    )
    resultado = client.authorize(invoice)
    print(f"CAE={resultado.cae} vto={resultado.cae_fch_vto}")
    return invoice


def _diff(registrado: dict[str, str], expected_fecha_pago: str | None) -> list[str]:
    missing = [f for f in PDF_CMP_FIELDS if not registrado.get(f)]
    problems = [f"ausente o vacío en FEXGetCMP: {f}" for f in missing]
    if expected_fecha_pago is not None:
        got = registrado.get("Fecha_pago")
        if got != expected_fecha_pago:
            problems.append(
                f"Fecha_pago: esperado {expected_fecha_pago!r}, GetCMP={got!r}"
            )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pv", type=int, default=1, help="Punto de venta (default 1)")
    parser.add_argument(
        "--nro",
        type=int,
        default=None,
        help="Si se pasa, solo consulta FEXGetCMP (no emite)",
    )
    parser.add_argument(
        "--fecha-pago",
        default=None,
        help="Valor esperado de Fecha_pago al consultar un CMP existente",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Imprime el dict plano de get_cmp (sin secretos Auth)",
    )
    args = parser.parse_args()

    config = load_config(resolve_boot_profile())
    if config.env != "homo":
        raise SystemExit(
            f"Este script es solo para homologación (ARCA_ENV={config.env})."
        )
    client = WsfexClient(config)
    conn = db.connect(config.paths.db)

    expected_fecha_pago: str | None = args.fecha_pago
    if args.nro is None:
        invoice = _authorize_representative(client, conn, args.pv)
        pv, nro = invoice.punto_vta, invoice.cbte_nro
        expected_fecha_pago = invoice.fecha_pago
    else:
        pv, nro = args.pv, args.nro
        print(f"Consultando FEXGetCMP tipo 19 {pv:05d}-{nro:08d}")

    registrado = client.get_cmp(CBTE_TIPO_FACTURA_E, pv, nro)
    if args.dump:
        print(json.dumps(registrado, indent=2, ensure_ascii=False, sort_keys=True))

    print()
    print("PDF Cmp field ← FEXGetCMP")
    print("-" * 48)
    for field in PDF_CMP_FIELDS:
        value = registrado.get(field, "")
        mark = "OK" if value else "MISSING"
        print(f"  [{mark:7}] {field:22} = {value!r}")

    print()
    print(f"LOCAL-only (not in GetCMP; seed): {LOCAL_ONLY_REMINDER}")

    problems = _diff(registrado, expected_fecha_pago)
    if problems:
        print("\nFIDELITY FAIL:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    print("\nFIDELITY OK: all PDF Cmp fields present in FEXGetCMP", end="")
    if expected_fecha_pago:
        print(f"; Fecha_pago={expected_fecha_pago!r} matches.")
    else:
        print(".")


if __name__ == "__main__":
    main()

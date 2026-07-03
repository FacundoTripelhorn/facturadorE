"""Fase 3: primer CAE en homologación — núcleo del spike.

Flujo completo (spike.md §4 fase 3 + reglas de authorize §2.3):
  1. Lee los datos de la factura de data/invoice_input.json (fuera del repo).
     Si no existe, lo crea con placeholders de homologación y sigue.
  2. Cotización DOL del día vía FEXGetPARAM_Ctz (nunca cotización propia).
  3. FEXGetLast_ID y FEXGetLast_CMP: Id idempotente y numeración de ARCA.
  4. Persiste el request completo ANTES de llamar (data/authorize/<id>.json).
     Si quedó un request pendiente (submitting/unknown), lo reintenta con el
     MISMO Id y los MISMOS datos: reproceso seguro (checklist §2.1.1 punto 3).
  5. FEXAuthorize → CAE.
  6. Verificación post-emisión con FEXGetCMP (checklist punto 6).

Uso:  uv run python scripts/authorize_homo.py
"""

import datetime as dt
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from facturador import db
from facturador.config import load_config
from facturador.wsfex import Invoice, InvoiceItem, WsfexClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

PUNTO_VTA_HOMO = 1

PLACEHOLDER_INPUT = {
    "_nota": (
        "PLACEHOLDER de homologación. Reemplazar con los datos reales del "
        "cliente antes del caso canónico definitivo. Este archivo NO va al repo."
    ),
    "cliente": "CLIENTE DEL EXTERIOR PRUEBA S.A.",
    "domicilio_cliente": "Av. Siempreviva 123, Montevideo",
    "pais_descripcion": "URUGUAY",
    "cuit_pais_descripcion": "URUGUAY - Persona Jur",
    "id_impositivo": "RUT 219999830019",
    "descripcion_servicio": "Servicios de desarrollo de software",
    "imp_total": "100.00",
    "forma_pago": "WIRE TRANSFER",
    "fecha_pago": None,
}


def cargar_input(path: Path) -> dict:
    if not path.is_file():
        path.write_text(
            json.dumps(PLACEHOLDER_INPUT, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"AVISO: no había input; se creó {path} con datos PLACEHOLDER.\n")
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_param(conn, kind: str, description_prefix: str) -> int:
    filas = [
        f
        for f in db.get_params(conn, kind)
        if (f["description"] or "").upper().startswith(description_prefix.upper())
    ]
    if not filas:
        raise SystemExit(
            f"No se encontró {kind} que empiece con {description_prefix!r} en "
            "arca_params. Correr antes: uv run python scripts/check_wsfex.py"
        )
    return int(filas[0]["code"])


def request_pendiente(requests_dir: Path) -> dict | None:
    for archivo in sorted(requests_dir.glob("*.json")):
        registro = json.loads(archivo.read_text(encoding="utf-8"))
        if registro.get("status") in ("submitting", "unknown"):
            return registro
    return None


def invoice_de_registro(registro: dict) -> Invoice:
    datos = registro["invoice"]
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


def registro_de_invoice(invoice: Invoice, status: str) -> dict:
    datos = {
        k: (format(v, "f") if isinstance(v, Decimal) else v)
        for k, v in vars(invoice).items()
        if k != "items"
    }
    datos["items"] = [
        {
            "pro_ds": i.pro_ds,
            "pro_precio_uni": format(i.pro_precio_uni, "f"),
            "pro_codigo": i.pro_codigo,
            "pro_qty": format(i.pro_qty, "f"),
            "pro_umed": i.pro_umed,
        }
        for i in invoice.items
    ]
    return {"status": status, "invoice": datos}


def guardar_registro(requests_dir: Path, registro: dict) -> Path:
    path = requests_dir / f"{registro['invoice']['arca_id']}.json"
    path.write_text(
        json.dumps(registro, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def main() -> None:
    config = load_config()
    if config.env != "homo":
        raise SystemExit(
            f"Este script es solo para homologación (ARCA_ENV={config.env})."
        )
    client = WsfexClient(config)
    conn = db.connect(config.data_dir / "facturador.db")
    requests_dir = config.data_dir / "authorize"
    requests_dir.mkdir(parents=True, exist_ok=True)

    pendiente = request_pendiente(requests_dir)
    if pendiente is not None:
        invoice = invoice_de_registro(pendiente)
        print(
            f"Request pendiente (Id={invoice.arca_id}, status={pendiente['status']}): "
            "se reintenta con el MISMO Id y datos idénticos (reproceso ARCA)."
        )
    else:
        entrada = cargar_input(config.data_dir / "invoice_input.json")
        hoy = dt.date.today().strftime("%Y%m%d")

        dst_cmp = lookup_param(conn, "pais", entrada["pais_descripcion"])
        cuit_pais = lookup_param(conn, "cuit_pais", entrada["cuit_pais_descripcion"])

        ctz, ctz_fecha = client.get_ctz("DOL")
        print(f"Cotización DOL (ARCA, {ctz_fecha}): {ctz}")

        arca_id = client.get_last_id() + 1
        cbte_nro = client.get_last_cmp(PUNTO_VTA_HOMO, 19) + 1
        print(f"Id idempotente: {arca_id}  |  Comprobante: {PUNTO_VTA_HOMO:05d}-{cbte_nro:08d}")

        imp_total = Decimal(entrada["imp_total"])
        invoice = Invoice(
            arca_id=arca_id,
            fecha_cbte=hoy,
            punto_vta=PUNTO_VTA_HOMO,
            cbte_nro=cbte_nro,
            dst_cmp=dst_cmp,
            cliente=entrada["cliente"],
            cuit_pais_cliente=cuit_pais,
            domicilio_cliente=entrada["domicilio_cliente"],
            id_impositivo=entrada["id_impositivo"],
            moneda_ctz=ctz,
            imp_total=imp_total,
            fecha_pago=entrada.get("fecha_pago") or hoy,
            forma_pago=entrada["forma_pago"],
            items=[
                InvoiceItem(
                    pro_ds=entrada["descripcion_servicio"],
                    pro_precio_uni=imp_total,
                )
            ],
        )

    # Persistir ANTES de llamar: si el proceso muere post-envío, el próximo
    # run reintenta con el mismo Id (idempotencia, §2.3 regla 3).
    registro = registro_de_invoice(invoice, "submitting")
    # Ambiente registrado en cada comprobante (checklist §2.1.1 punto 1).
    registro["environment"] = config.env
    registro_path = guardar_registro(requests_dir, registro)

    try:
        resultado = client.authorize(invoice)
    except Exception:
        registro["status"] = "unknown"
        guardar_registro(requests_dir, registro)
        print(
            f"\nFallo en FEXAuthorize; request queda como 'unknown' en "
            f"{registro_path}. Reintentar este script reusa el mismo Id."
        )
        raise

    registro["status"] = "authorized"
    registro["cae"] = resultado.cae
    registro["cae_fch_vto"] = resultado.cae_fch_vto
    registro["reproceso"] = resultado.reproceso
    guardar_registro(requests_dir, registro)

    print()
    print("=" * 60)
    print(f"CAE            : {resultado.cae}")
    print(f"Vencimiento CAE: {resultado.cae_fch_vto}")
    print(f"Comprobante    : tipo 19, {invoice.punto_vta:05d}-{invoice.cbte_nro:08d}")
    print(f"Fecha emisión  : {invoice.fecha_cbte}  |  Fecha pago: {invoice.fecha_pago}")
    print(f"Importe        : {invoice.moneda_id} {invoice.imp_total} (ctz {invoice.moneda_ctz})")
    print(f"Reproceso      : {resultado.reproceso}")
    if resultado.motivos_obs:
        print(f"Observaciones  : {resultado.motivos_obs}")
    print("=" * 60)

    problemas = client.verify_issued(invoice, resultado)
    if problemas:
        print("\nVERIFICACIÓN FEXGetCMP CON DISCREPANCIAS:")
        for p in problemas:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerificación FEXGetCMP: OK (CAE, importe y número coinciden).")


if __name__ == "__main__":
    main()

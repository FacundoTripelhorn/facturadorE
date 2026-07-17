# Spike FAC-63: FEXGetCMP field fidelity for Factura E PDF regeneration

**Status:** Finding recorded (2026-07-17).  
**Blocks:** [FAC-44](https://linear.app/ftripelhorn/issue/FAC-44) (seed / sidecar), [FAC-65](https://linear.app/ftripelhorn/issue/FAC-65) vs [FAC-46](https://linear.app/ftripelhorn/issue/FAC-46) restore model.  
**Non-goals:** No backup/sidecar/restore implementation in this spike.

## Verdict (for FAC-44)

**The SQLite DB can be omitted from the backup.** Rebuild-from-ARCA is viable for the per-comprobante ledger.

**No per-comprobante sidecar is required** for WSFEX payload fields printed on the PDF — including the prime suspect `Fecha_pago`.

**The seed must carry profile-local data ARCA never stores** (emisor display fields, fiscal CUIT, environment, PV/tipo set, default client / UI defaults, schema version). Those belong in the FAC-44 seed, not in a per-número sidecar.

Optional live smoke (operator with a WSASS homologación cert):  
`uv run python scripts/spike_fexgetcmp_fidelity.py` — confirms `Fecha_pago` (and other Cmp fields) are non-empty in a real `FEXGetCMP` response. Schema + official manual already require this; the script is belt-and-suspenders.

---

## Method

1. Enumerate every value `facturador/pdf/invoice.html` + `pdf/qr.py` need to regenerate a Factura E PDF (v1 renderer).
2. Diff that set against the live homologación WSDL type `ClsFEXGetCMPR` (`FEXGetCMP` → `FEXResultGet`), fetched 2026-07-17 from `https://wswhomo.afip.gov.ar/wsfexv1/service.asmx?WSDL`.
3. Cross-check the AFIP WSFEX developer manual (v3.1.1): changelog **1.6.0 (2019-09-23)** added `Fecha_pago` to **both** authorization (`ClsFEXRequest`) **and** consultation (`ClsFEXGetCMPR`); validations 1671–1674 make it mandatory for tipo 19 + `Tipo_expo` 2/4.
4. Live emission in this Cloud Agent environment was **blocked** (no WSASS-registered homologación cert on the profile). Schema + product code are sufficient for the FAC-44 decision; the probe script below is for an operator smoke test.

Do **not** treat `tests/arca_fake.py` `FEXGetCMP` as evidence of ARCA behavior — the fake returns only a reconciliation subset (`Id`, `Cbte_*`, `Cae`, `Imp_total`, `Fch_venc_Cae`).

---

## Field-by-field: PDF regeneration vs `FEXGetCMP`

Legend:

| Mark | Meaning |
|------|---------|
| **RT** | Round-trips in `ClsFEXGetCMPR` (present in live WSDL) |
| **LOCAL** | Never sent to / returned by ARCA; must come from seed or re-derive |
| **DERIVE** | Code/description not in GetCMP; re-resolve from `FEXGetPARAM_*` / cert / profile after restore |

### A. WSFEX Cmp fields used on the PDF

| PDF / QR use | Local column / source | `FEXGetCMP` tag | Status |
|---|---|---|---|
| COD. / tipo | `cbte_tipo` | `Cbte_tipo` | **RT** |
| Compr. Nro (PV) | `punto_venta` | `Punto_vta` | **RT** |
| Compr. Nro (nro) | `cbte_nro` | `Cbte_nro` | **RT** |
| Fecha de Emisión | `fecha_cbte` | `Fecha_cbte` | **RT** |
| Fecha de Pago | `fecha_pago` | `Fecha_pago` | **RT** (see note) |
| Señor(es) | `cliente` | `Cliente` | **RT** |
| Domicilio | `domicilio_cliente` | `Domicilio_cliente` | **RT** |
| CUIT País (code) | `cuit_pais_cliente` | `Cuit_pais_cliente` | **RT** |
| ID Impositivo | `id_impositivo` | `Id_impositivo` | **RT** |
| Destino (code) | `dst_cmp` | `Dst_cmp` | **RT** |
| Divisa (code) | `moneda_id` | `Moneda_Id` | **RT** |
| Tipo de Cambio | `moneda_ctz` | `Moneda_ctz` | **RT** |
| Forma de Pago | `forma_pago` | `Forma_pago` | **RT** |
| Incoterms | `incoterms` | `Incoterms` | **RT** |
| Importe Total | `imp_total` | `Imp_total` | **RT** |
| Observaciones | `obs` | `Obs` | **RT** |
| CAE | `cae` | `Cae` | **RT** |
| Vto. CAE | `cae_fch_vto` | `Fch_venc_Cae` | **RT** |
| Ítem código | `invoice_items.pro_codigo` | `Items/Item/Pro_codigo` | **RT** |
| Ítem descripción | `pro_ds` | `Pro_ds` | **RT** |
| Cantidad | `pro_qty` | `Pro_qty` | **RT** |
| U. Medida (code) | `pro_umed` | `Pro_umed` | **RT** |
| Precio unit. | `pro_precio_uni` | `Pro_precio_uni` | **RT** |
| Total ítem | `pro_total_item` | `Pro_total_item` | **RT** |
| (QR) mismo set | — | codes above + CAE | **RT** |

**`Fecha_pago` note:** Present in live `ClsFEXGetCMPR`, documented in the official GetCMP response table, and mandatory on authorize for services/otros since 2019-11-01. It is **not** a silent local-only field. Prime-suspect status is **rejected** for the sidecar decision.

Also returned by GetCMP but not required by the current PDF v1: `Id`, `Tipo_expo`, `Permiso_existente`, `Permisos`, `CanMisMonExt`, `Obs_comerciales`, `Cmps_asoc`, `Incoterms_Ds`, `Idioma_cbte`, `Fecha_cbte_cae`, `Resultado`, `Motivos_Obs`, `Opcionales`, `Actividades`, `Pro_bonificacion`. Useful for full register rebuild (FAC-65); irrelevant to PDF fidelity.

### B. Fields the PDF needs that ARCA does **not** return

| PDF use | Local column | Why missing from GetCMP | Where it belongs |
|---|---|---|---|
| Razón Social (emisor) | `emisor_razon_social` | Never travels in FEXAuthorize (`design.md` §0.1) | **Seed** (emisor entity) |
| Domicilio Comercial | `emisor_domicilio` | idem | **Seed** |
| Condición IVA | `emisor_condicion_iva` | idem | **Seed** |
| Ingresos Brutos | `emisor_iibb` | idem | **Seed** |
| Inicio de Actividades | `emisor_inicio_actividades` | idem | **Seed** |
| CUIT emisor (header + QR) | `cuit_emisor` | Not in `ClsFEXGetCMPR`; Auth.Cuit is the live cert | **Seed** (`fiscal_cuit`) |
| Homologación banner | `environment` | Profile seal, not a Cmp field | **Seed** |
| Destino descripción | `dst_cmp_ds` | Code RT; label from params | **DERIVE** via `FEXGetPARAM_DST_pais` |
| CUIT País descripción | `cuit_pais_cliente_ds` | Code RT; label from params | **DERIVE** via `FEXGetPARAM_DST_CUIT` |
| Divisa descripción | `moneda_ds` | Code RT; label from params | **DERIVE** via `FEXGetPARAM_MON` |
| U. Medida label | `pro_umed_ds` | Code RT; label from params | **DERIVE** via `FEXGetPARAM_UMed` |
| PDF renderer pin | `pdf_render_version` | App-local (FAC-53) | Default current renderer on rebuild (or seed app/schema version) |

Static PDF chrome (letter E, leyenda IVA EXENTO, descargo, “Comprobante Autorizado”) lives in the git template — not seed, not ARCA.

---

## Explicit list: does **not** round-trip through `FEXGetCMP`

### Per-comprobante WSFEX fields that fail to round-trip

**(empty)** — for the representative Factura E (tipo 19, `tipo_expo=2`, USD + cotización, single line, `Fecha_pago` set), every Cmp field the PDF prints is in `ClsFEXGetCMPR`, including `Fecha_pago` and `Items`.

### Local-authoritative data (not a GetCMP gap; seed contents)

These are the fields ARCA never had. Losing the DB without a seed loses them forever:

1. Emisor display snapshot fields: `razon_social`, `domicilio`, `condicion_iva`, `iibb`, `inicio_actividades`
2. Fiscal CUIT of the profile
3. Environment seal (`homo` / `prod`)
4. PV + comprobante-tipo set (what to re-query)
5. Default client / UI defaults (not needed to reprint historical PDFs once Cmp is rebuilt; needed to re-bootstrap the product)
6. Schema / app version (incl. which PDF renderer to use for reconstructed rows)

Param **descriptions** are re-derivable after a params sync; they need not be sidecared per número.

---

## Recommendation for FAC-44 / FAC-65

| Option | Decision |
|--------|----------|
| Omit DB from backup? | **Yes** |
| Per-comprobante sidecar? | **No** (empty field list) |
| Seed must carry | Full emisor entity (not only CUIT), environment, PV/tipo set, fiscal CUIT, default client / UI config, schema version, manifest high-water marks |
| Restore model | Prefer **FAC-65** (rebuild-from-ARCA). FAC-46 DB-in-bundle is not required for PDF/ledger fidelity under this finding |
| Historical emisor edits | Rebuilt PDFs use the **seed’s current emisor** text. If an emisor was edited after some invoices were issued, those PDFs will not match the original snapshot unless a future product choice stores historical emisor revisions in the seed. At ~1 invoice/week this is an accepted tradeoff; it is **not** a reason to keep the full DB in the backup |

### Implementation notes for FAC-65 (out of scope here)

- `WsfexClient.get_cmp` today flattens leaf tags; multi-item invoices need a nested `Items` parse before reconstruction.
- After rebuild, re-resolve `*_ds` / `pro_umed_ds` from a fresh params sync; set `pdf_render_version` to the current registered renderer; set `cuit_emisor` / emisor snapshot columns from the imported seed; set `environment` from the seed seal.
- `source` for reconstructed rows should be marked appropriately (existing `imported` vs a new value — product choice in FAC-65).

---

## Evidence sources

| Source | What it proves |
|--------|----------------|
| Live WSDL `ClsFEXGetCMPR` (homo, 2026-07-17) | `Fecha_pago`, `Items`, and full Cmp set are in the GetCMP response type |
| WSFEX Manual v3.1.1 § changelog 1.6.0 + §2.2 response table | `Fecha_pago` added to authorize **and** consulta; listed in GetCMP response |
| `design.md` §0.1 + `facturador/settings.py` | Emisor PDF lines never travel to ARCA |
| `facturador/pdf/invoice.html`, `pdf/qr.py`, `schema.sql` | Complete PDF field inventory |
| This agent environment | No homologación cert → live authorize skipped; probe script left for operator |

---

## Operator smoke test

With a Homologación profile that has a WSASS cert authorized for `wsfex` and seeded params:

```bash
ARCA_ENV=homo uv run python scripts/spike_fexgetcmp_fidelity.py
# or against an already-authorized CMP:
ARCA_ENV=homo uv run python scripts/spike_fexgetcmp_fidelity.py --pv 1 --nro <N> --fecha-pago <YYYYMMDD>
```

Expected: printed matrix shows all Cmp PDF fields present in GetCMP, including `Fecha_pago` matching the authorized value when a new invoice is emitted.

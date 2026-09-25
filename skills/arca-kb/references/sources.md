# Sources and citation policy

## Pinned Official (resolvable)

| Artifact | Stable URL / pin | Used for |
|----------|------------------|----------|
| WSFEXv1 developer manual **v3.1.1** | [PDF (afip.gob.ar)](https://www.afip.gob.ar/ws/documentacion/manuales/WSFEX-Manualparaeldesarrollador_V3.1.1_ARCA.pdf) · [mirror arca.gob.ar](https://arca.gob.ar/ws/documentacion/manuales/WSFEX-Manualparaeldesarrollador_V3.1.1_ARCA.pdf) · index: [ws-factura-electronica.asp](https://www.afip.gob.ar/ws/documentacion/ws-factura-electronica.asp) | Endpoints, `Fecha_pago` validations **1671–1674**, GetCMP field table, GetCMP error **1020**, authorize validations (~1520 sequence), `Reproceso`, infra errors 500/501/502/505 |
| Changelog note **1.6.0** (2019-09-23) | Same PDF (historial / changelog section) | `Fecha_pago` added to authorize **and** GetCMP response |
| WSAA developer manual | [PDF](https://www.afip.gob.ar/ws/WSAA/WSAAmanualDev.pdf) · [arca.gob.ar](https://arca.gob.ar/ws/WSAA/WSAAmanualDev.pdf) | LoginCms, TA reuse while vigente (~12 h), already-authenticated / retention guidance |
| WSAA technical spec 1.2.2 | [PDF](https://www.afip.gob.ar/ws/wsaa/especificacion_tecnica_wsaa_1.2.2.pdf) · [arca.gob.ar](https://arca.gob.ar/ws/WSAA/Especificacion_Tecnica_WSAA_1.2.2.pdf) | TRA fields, CMS, generation/expiration windows |
| Live WSFEX WSDL (homo) | `https://wswhomo.afip.gov.ar/wsfexv1/service.asmx?WSDL` (fetched **2026-07-17** for the fidelity matrix) | `ClsFEXGetCMPR` includes `Fecha_pago`, `Items`; NS `http://ar.gov.afip.dif.fexv1/` |
| Live WSFEX WSDL (prod) | `https://servicios1.afip.gov.ar/wsfexv1/service.asmx?WSDL` | Production schema check |

Do **not** vendor PDF binaries in this skill unless licensing is explicit. Prefer the
URLs above when citing or refreshing pins. If a portal moves files, update this
table — do not leave ellipsis-only references.

## Observed (this project)

| Finding | Where | Evidence strength |
|---------|-------|-------------------|
| `Fecha_pago` / Cmp fidelity | Operator GetCMP matrix; live WSDL | **Obs** + **O** |
| Rebuild gap allow-list = ErrCode **1521** | `docs/wsfex-gap-vs-error.md`; `CMP_NOT_FOUND_CODES`; synthetic capture | **Pending** wire — product/fake contract only. **Contrast O:** manual documents **1020**. Promote when a redacted live `FEXGetCMP` dump is attached. |
| Endpoints / SOAP client behavior | `docs/design.md` §1; `facturador/constants.py`; `facturador/arca/*` | **Obs** aligned with **O** URLs |
| Reproceso (`Reproceso=S` same Id) | **O** authorize response fields; product authorize path | **O** / Obs |
| ErrCode **1462** “Nro de comprobante ya utilizado” | `tests/arca_fake.py` + skill table | **Pending** wire capture — not in manual v3.1.1 tables; see [errors-observed.md](errors-observed.md) |

**Note:** the in-process fake is **not** proof of ARCA wire behavior by itself.
The fidelity matrix warned against treating a truncated fake GetCMP as fidelity evidence.
Use the fake as a **contract mirror** only where product docs say so (rebuild
**1521**), and keep **Pending** labels for codes still lacking a redacted live dump
or Official listing. In particular, **do not** upgrade fake/product ErrCodes
(`1521`, `1462`) to **Observed** without a checked-in redacted live response.

## Community (secondary, always contrasted)

| Item | Citation | Contrast |
|------|----------|----------|
| `coe.alreadyAuthenticated` / CEE ya posee TA | [AfipSDK — errores frecuentes](https://docs.afipsdk.com/recursos/errores-frecuentes); [AfipSDK blog 2025-02-21](https://afipsdk.com/blog/solucion-a-error-alreadyauthenticated/); [afip.php#95](https://github.com/AfipSDK/afip.php/issues/95) | Matches **O** WSAA reuse rule. Prefer TA **cache reuse** over community cooldown as primary mitigation. |
| WSFEv1 `10016` / PV `11002` | Same AfipSDK errores page | **WSFEv1**, not WSFEX. Map conceptually to `FEXGetLast_CMP` / Official ~1520 sequence checks — do not copy codes blindly. |
| RG 5616 Condición IVA receptor | AfipSDK / ARCA RG materials | Applies to domestic WSFEv1 flows; WSFEX foreign receptor uses tax-id + país CUIT fields (design §1.5). |

**Policy:** paraphrase; do not copy proprietary SDK source or protected manual
prose wholesale. No runtime dependency on AfipSDK or any third-party billing SDK.

## Claim checklist

Before stating a behavior in an agent answer:

1. Label **Official / Observed / Community / Pending**.
2. Prefer a resolvable URL from this file for Official claims.
3. If only Community or Pending: state uncertainty and what Official/live check would confirm.
4. If unknown: say “not evidenced in arca-kb” and suggest WSDL/manual/homo probe.

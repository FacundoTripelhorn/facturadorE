# Sources and citation policy

## Pinned Official

| Artifact | Pin / note | Used for |
|----------|------------|----------|
| WSFEXv1 developer manual | **v3.1.1** (FAC-63); changelog **1.6.0** for `Fecha_pago` | Field presence, validations 1671–1674, GetCMP table |
| WSAA LoginCms / TA docs | Official WSAA manual (TRA window, NTP `time.afip.gov.ar`, TA lifetime) | Auth flow |
| Live WSFEX WSDL (homo) | Fetched **2026-07-17** from `…/wsfexv1/service.asmx?WSDL` | `ClsFEXGetCMPR` includes `Fecha_pago`, `Items`; NS `http://ar.gov.afip.dif.fexv1/` |

Retrieve manuals from ARCA/AFIP developer portals when refreshing pins. Do not
vendor PDF binaries in this skill unless licensing is explicit.

## Observed (this project)

| Finding | Where |
|---------|-------|
| FAC-63 `Fecha_pago` / Cmp fidelity | Linear FAC-63; spike matrix (also summarized in gotchas); operator GetCMP dump |
| FAC-65 gap = ErrCode **1521** | `docs/wsfex-gap-vs-error.md`; `CMP_NOT_FOUND_CODES` |
| Endpoints / SOAP client behavior | `docs/design.md` §1; `facturador/constants.py`; `facturador/arca/*` |
| Reproceso / 1462 / events | Authorize path + `tests/arca_fake.py` (fake mirrors Observed contracts used in CI) |

**Note:** the in-process fake is **not** proof of ARCA wire behavior by itself.
FAC-63 explicitly warned against treating a truncated fake GetCMP as fidelity
evidence. Use fake only where product docs state it mirrors a confirmed contract
(e.g. 1521 for rebuild).

## Community (secondary, always contrasted)

| Item | Citation | Contrast |
|------|----------|----------|
| `coe.alreadyAuthenticated` / CEE ya posee TA | [AfipSDK — errores frecuentes](https://docs.afipsdk.com/recursos/errores-frecuentes); [AfipSDK blog 2025-02-21](https://afipsdk.com/blog/solucion-a-error-alreadyauthenticated/); [afip.php#95](https://github.com/AfipSDK/afip.php/issues/95) | Matches Official WSAA rule. Prefer TA **cache reuse** over community cooldown as primary mitigation. Cooldown values are Community operational hints, not a substitute for `expirationTime`. |
| WSFEv1 `10016` / PV `11002` | Same AfipSDK errores page | **WSFEv1**, not WSFEX. Map conceptually to `FEXGetLast_CMP` / WSFEX ErrCodes — do not copy codes blindly. |
| RG 5616 Condición IVA receptor | AfipSDK / ARCA RG materials | Applies to domestic WSFEv1 flows; WSFEX foreign receptor uses tax-id + país CUIT fields (design §1.5). |

**Policy:** paraphrase; do not copy proprietary SDK source or protected manual
prose wholesale. No runtime dependency on AfipSDK or any third-party billing SDK.

## Claim checklist

Before stating a behavior in an agent answer:

1. Label **Official / Observed / Community**.
2. If only Community: state uncertainty and what Official check would confirm.
3. If unknown: say “not evidenced in arca-kb” and suggest WSDL/manual/homo probe.

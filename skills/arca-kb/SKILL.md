---
name: arca-kb
description: >-
  Distilled, citation-backed knowledge of ARCA (ex AFIP) WSAA and WSFEXv1 for
  Argentine Factura E (export invoices). Use whenever the user or task mentions
  ARCA, AFIP, WSAA, WSFEX, WSFEXv1, Ticket de Acceso (TA), LoginCms, FEXAuthorize,
  FEXGetCMP, FEXGetLast_CMP, FEXGetPARAM_Ctz, CAE, ErrCode 1521, Fecha_pago,
  reproceso, cotización, homologación/producción endpoints, or rebuild-from-ARCA
  gap handling — even if they do not say "skill" or "knowledge base". Do not use
  for unrelated app/UI/Python questions with no ARCA/WS surface.
license: MIT
metadata:
  version: "1.0"
  pinned_manuals: "WSFEXv1 developer manual v3.1.1; WSAA LoginCms (official)"
  related_issues: "FAC-63, FAC-65, FAC-66"
---

# ARCA Knowledge Base (`arca-kb`)

Curated facts about **WSAA** + **WSFEXv1** for Factura E. Prefer this skill over
training-memory defaults when they conflict.

## Source labels (mandatory)

Tag every non-obvious claim:

| Label | Meaning |
|-------|---------|
| **Official** | Pinned ARCA/AFIP manuals or live WSDL |
| **Observed** | Homologación / project findings (FAC-63, FAC-65, design spike) |
| **Community** | Third-party tech notes (e.g. AfipSDK); cite URL; contrast with Official/Observed |

**Rule:** do not invent ARCA behavior. If evidence is missing, say so and point
to the manual/WSDL/probe to verify.

This skill is **not** FacturadorE internal architecture docs. Product code lives
in the repo (`docs/design.md`, `facturador/arca/`). Load references below only
when needed.

## Progressive disclosure

| Need | Read |
|------|------|
| Observed `FEXErr` / gap vs error | [references/errors-observed.md](references/errors-observed.md) |
| Gotchas (`Fecha_pago`, TA reuse, numbering, cotización) | [references/gotchas.md](references/gotchas.md) |
| Citation index + community contrasts | [references/sources.md](references/sources.md) |
| Redacted XML shapes | [captures/](captures/) + [captures/README.md](captures/README.md) |
| Eval / portability notes | [VALIDATION.md](VALIDATION.md) |

---

## Endpoints

**Official** + mirrored in project constants (`facturador/constants.py`):

| Service | Homologación | Producción |
|---------|--------------|------------|
| WSAA `LoginCms` | `https://wsaahomo.afip.gov.ar/ws/services/LoginCms` | `https://wsaa.afip.gov.ar/ws/services/LoginCms` |
| WSFEXv1 | `https://wswhomo.afip.gov.ar/wsfexv1/service.asmx` | `https://servicios1.afip.gov.ar/wsfexv1/service.asmx` |

- SOAP 1.1 ASMX. WSDL: append `?WSDL` to the WSFEX URL.
- **Observed** namespace (live WSDL 2026-07): `http://ar.gov.afip.dif.fexv1/` (lowercase `fexv1`, not `FEXV1`).
- Pair cert ↔ URLs atomically per environment. Never mix homo cert with prod URL (**Official** / design checklist).

Factura E is **WSFEX**, not WSFEv1 (national A/B/C).

## Authentication (WSAA → TA)

1. Build TRA (`LoginTicketRequest`) with `service=wsfex`, `uniqueId`, `generationTime`, `expirationTime`.
2. Sign as CMS/PKCS#7 with the contributor X.509 + private key; Base64; SOAP `loginCms`.
3. Response: `token` + `sign` + times. Pass `Auth { Token, Sign, Cuit }` on every WSFEX call.
4. **Cache and reuse** the TA until `expirationTime`. Typical validity ~12 h (**Official**); always parse expiry — do not hard-code.
5. Requesting a new TA while one is still valid for that cert/service yields SOAP fault `coe.alreadyAuthenticated` / “El CEE ya posee un TA válido…” (**Official** behavior; **Community** frequently reports it — see gotchas).
6. Clock skew is a top WSAA failure mode: NTP vs `time.afip.gov.ar`; TRA window often gen−10 / exp+10 min (**Official**).

**Secrets:** never log Token, Sign, or CMS (**Official** checklist / design §2.1.1.9). Captures in this skill are redacted.

## Methods used for Factura E

| Method | Role |
|--------|------|
| `FEXDummy` | Liveness (App/Db/Auth servers) |
| `FEXGetLast_ID` | Last client `Id` used in authorize |
| `FEXGetLast_CMP` | Last authorized number for `(Pto_venta, Cbte_Tipo)` — **authoritative** |
| `FEXAuthorize` | Issue Cmp; client supplies int64 `Id` |
| `FEXGetCMP` | Fetch registered Cmp by tipo + PV + nro (reconcile / rebuild) |
| `FEXGetPARAM_*` | Dynamic tables (moneda, país, CUIT país, UMed, tipos, idiomas, Incoterms, …) |
| `FEXGetPARAM_Ctz` | FX rate for a currency/date (foreign currency invoices) |

**Observed:** WSFEXv1 has **no** bulk/range GetCMP API; rebuild walks numbers one-by-one (FAC-65).

## Canonical Factura E fields (services)

**Observed** from real Cel invoice + WSFEX mapping (`design.md` §0.1):

| Concept | Field | Notes |
|---------|-------|-------|
| Tipo | `Cbte_Tipo = 19` | ND 20 / NC 21 out of happy path |
| Expo | `Tipo_expo = 2` | servicios; bienes=1, otros=4 |
| Dates | `Fecha_cbte`, `Fecha_pago` | `AAAAMMDD`. Payment **may differ** from issue date |
| FX | `Moneda_Id=DOL`, `Moneda_ctz` | Rate from `FEXGetPARAM_Ctz`, not a private rate |
| Party | `Cliente`, `Domicilio_cliente`, `Cuit_pais_cliente`, `Id_impositivo`, `Dst_cmp` | |
| Line | `Items[]` (`Pro_*`) | Typical: qty 1, umed 7, precio = total |
| Total | `Imp_total` | Sum of items |
| Incoterms | often empty for services | Still a Cmp field |

**FAC-63 / Observed + Official (manual v3.1.1 changelog 1.6.0):** `Fecha_pago` is on **both** authorize request and `FEXGetCMP` response (`ClsFEXGetCMPR`). Mandatory for tipo 19 + expo 2/4 (validations 1671–1674). It **round-trips**; it is not a silent local-only field. Details: [gotchas.md](references/gotchas.md).

Formats:

- Dates: `YYYYMMDD` (no separators).
- Decimals: ARCA SOAP text (project serializes with invariant separators).
- Currency codes: ARCA table ids (`DOL`, `PES`), not necessarily ISO display labels.

## Idempotency and recovery

1. Persist client `Id` + full request **before** `FEXAuthorize`.
2. Same `Id` + same payload → **reproceso**: ARCA returns the same CAE (`Reproceso=S`) (**Official** / Observed).
3. On timeout after send: `FEXGetCMP` before inventing a new number (**Official** pattern).
4. Numbering: always `FEXGetLast_CMP + 1`; never trust a local counter alone.
5. Concurrent issuers against one PV collide; serialize authorize (**Observed** product constraint).

## Gap vs error on `FEXGetCMP` (FAC-65)

**Observed** (documented rebuild contract):

| Signal | Meaning |
|--------|---------|
| `FEXErr.ErrCode = 1521` | Cmp **does not exist** (“No existen datos para el comprobante”) → known gap |
| Other `ErrCode ≠ 0` | Business/server error → **not** a gap; retry then abort rebuild |
| HTTP / timeout / SOAP Fault | Transport → retry then abort; **never** invent a gap |

Full table: [errors-observed.md](references/errors-observed.md). Capture: [captures/fexgetcmp-not-found-1521.xml](captures/fexgetcmp-not-found-1521.xml).

## Quick answers

**Q: Does `Fecha_pago` come back from `FEXGetCMP`?**  
Yes — **Official** (manual v3.1.1 / WSDL `ClsFEXGetCMPR`) and **Observed** (FAC-63 fidelity matrix, live homo WSDL 2026-07-17; operator smoke confirmed non-empty).

**Q: How do I know a number is a hole vs ARCA failure during rebuild?**  
Only treat **1521** (or an explicitly extended not-found set) as absence. Everything else aborts after retries — [errors-observed.md](references/errors-observed.md).

**Q: Why did WSAA say the CEE already has a valid TA?**  
You requested a new TA while one is still valid. Reuse the cached TA (**Official** + **Community**). See [gotchas.md](references/gotchas.md).

## Redaction

When quoting wire traffic, follow [captures/README.md](captures/README.md) (Token/Sign/CMS/certs/real CUIT/PII stripped — design §2.1.1.9).

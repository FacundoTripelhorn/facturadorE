# Observed WSFEX / WSAA error codes

Practical interpretation for Factura E work. Prefer **Observed** rows for rebuild
and authorize recovery. Expand only with new evidence.

## Source key

- **O** = Official manual / WSDL semantics (resolvable links in [sources.md](sources.md))
- **Obs** = Project homologación / FAC-65 contract / in-repo fake mirroring Obs
- **C** = Community (AfipSDK etc.) — contrast before applying to WSFEX
- **Pending** = Behavior believed real / modeled in product; **wire ErrCode not yet
  captured** in a redacted homologación dump or listed in the pinned manual

## WSFEX `FEXErr`

| ErrCode | Typical message | Practical meaning | Source | Action |
|---------|-----------------|-------------------|--------|--------|
| `0` | OK | Success | O / Obs | Proceed |
| `1521` | No existen datos para el comprobante | Requested `(Cbte_tipo, Punto_vta, Cbte_nro)` is **not** on ARCA’s ledger | **Obs** (FAC-65 product contract; `CMP_NOT_FOUND_CODES`; mirrored in `tests/arca_fake.py`). **Contrast O:** pinned WSFEX manual v3.1.1 §2.2.4 documents GetCMP missing as **`1020` Comprobante inexistente** — see note below. | Treat **1521** as confirmed gap for this product’s rebuild allow-list. If live ARCA returns **1020** (or another not-found), extend `CMP_NOT_FOUND_CODES` and this table — do **not** invent gaps from unknown codes. |
| `1020` | Comprobante inexistente | Manual’s documented “missing Cmp” on consult | **O** WSFEX v3.1.1 §2.2.4 | Same recovery intent as 1521 **if** observed on the wire; add to not-found allow-list only after confirmation. |
| `1462` | Nro de comprobante ya utilizado | Number already registered under a **different** authorize `Id` (same Id → reproceso success, not this error) | **Pending** — product + `tests/arca_fake.py` model this code/msg; **not** listed in WSFEX manual v3.1.1 error tables searched 2026-07-28. Behavioral distinction (reuse Id vs collide on number) is **O**/Obs via `Reproceso` + sequential `Cbte_nro`. | Reconcile via `FEXGetCMP` / `FEXGetLast_CMP`; do not invent a new series. When live ARCA returns this (or a different code for the same situation), capture a redacted dump and promote the row to **Obs**. |
| `600` | No se corresponden token con firma / ValidacionDeToken… | Auth token/sign mismatch or stale TA for this call | O / Obs / C | Refresh TA from cache correctly; ensure Token+Sign pair from same TA; check env mix. |
| `500` / `501` | Error interno… | Infrastructure / application faults | **O** (manual lists 500/501/502/505); Obs rebuild abort path | Bounded retry, then **abort** rebuild — **not** a gap. |
| `1671`–`1674` | (Fecha_pago validations) | `Fecha_pago` required / invalid for tipo 19 + expo 2/4 | **O** WSFEX manual v3.1.1 (since 1.6.0 / 2019-11-01) | Send valid `Fecha_pago` (`YYYYMMDD`). |
| `1520` / seq. checks | (Cbte_nro / next-to-authorize) | Number must be in range and the **immediate next** after last authorized | **O** authorize validations (~1520, sequence text near Punto_vta/Cbte_nro) | Always take `FEXGetLast_CMP + 1`. Do not confuse validation **1520** with not-found **1521**. |

### Note: Official `1020` vs Observed `1521` (GetCMP missing)

Pinned **Official** manual (v3.1.1 §2.2.4) lists **`1020` Comprobante inexistente** under GetCMP errors. This project’s FAC-65 rebuild contract and in-process fake use **`1521`** with message *No existen datos para el comprobante* (`docs/wsfex-gap-vs-error.md`). Until a redacted live dump is attached here, agents must:

- Prefer the product allow-list (**1521**) for FacturadorE rebuild code paths.
- Mention the manual **1020** as a possible alternate not-found signal.
- Never treat an unknown `ErrCode` as a gap.

### FAC-65 rule (gap vs failure)

On `FEXGetCMP` only:

1. **Allow-listed not-found codes (currently `1521`) → gap** (continue rebuild / record known gap).
2. **Any other `ErrCode ≠ 0` → error** (retry, then abort).
3. **HTTP / timeout / SOAP Fault → error** (retry, then abort). Never synthesize a gap.

Wire shape (1521): [../captures/fexgetcmp-not-found-1521.xml](../captures/fexgetcmp-not-found-1521.xml).

## WSFEX `FEXEvents`

| EventCode | Example | Meaning | Source | Action |
|-----------|---------|---------|--------|--------|
| (any) | Mantenimiento programado | Advisory / maintenance / normative notice | O / Obs | Log and surface as **warning**, never as hard failure of a successful `ErrCode=0` response. |

## WSAA SOAP faults

| Fault / text | Meaning | Source | Action |
|--------------|---------|--------|--------|
| `coe.alreadyAuthenticated` / “El CEE ya posee un TA válido…” | New TA requested while a valid TA exists for this cert+service | **O** WSAA developer manual (retention / reuse while TA vigente ~12 h); **C** AfipSDK | Reuse cached TA until `expirationTime`. Do not hammer LoginCms. Community wait hints (≈10 min homo / ≈2 min prod between *forced* renewals) are operational folklore — primary fix is **cache reuse**, not busy-wait. |
| `coe.notAuthorized` / computador no autorizado | Cert not linked to the requested WS | O / C | Authorize `wsfex` in WSASS (homo) or production relaciones. |
| Untrusted / wrong-AC cert | Homo cert against prod (or inverse), or self-signed | O / C / Obs | Fix cert↔env pairing. Self-signed → `cms.cert.untrusted` locally. |

## Community codes that are **not** WSFEX

AfipSDK “errores frecuentes” lists WSFEv1 codes such as **`10016`** (“número o fecha no se corresponde con el próximo a autorizar”) and PV errors like **`11002`**. Those apply to **WSFEv1**, not WSFEXv1.

**Contrast:** On WSFEX, align with `FEXGetLast_CMP` and Official sequence validations (~1520 / next-to-authorize). Do not paste WSFEv1 ErrCodes into WSFEX diagnostics. Number-collision **behavior** is real; the specific **`1462`** label remains **Pending** wire confirmation (see table).

## References

- FAC-65 doc in repo: `docs/wsfex-gap-vs-error.md`
- Product mapping: `facturador/arca/wsfex.py` (`CMP_NOT_FOUND_CODES`, `CmpNotFoundError`)
- Citations: [sources.md](sources.md)

# Observed WSFEX / WSAA error codes

Practical interpretation for Factura E work. Prefer **Observed** rows for rebuild
and authorize recovery. Expand only with new evidence.

## Source key

- **O** = Official manual / WSDL semantics
- **Obs** = Project homologación / FAC-65 contract / in-repo fake mirroring Obs
- **C** = Community (AfipSDK etc.) — contrast before applying to WSFEX

## WSFEX `FEXErr`

| ErrCode | Typical message | Practical meaning | Source | Action |
|---------|-----------------|-------------------|--------|--------|
| `0` | OK | Success | O / Obs | Proceed |
| `1521` | No existen datos para el comprobante | Requested `(Cbte_tipo, Punto_vta, Cbte_nro)` is **not** on ARCA’s ledger | **Obs** (FAC-65); mirrored in `tests/arca_fake.py` | Treat as **confirmed gap**. Do **not** treat as transport failure. |
| `1462` | Nro de comprobante ya utilizado | Number already used with a **different** authorize `Id` | Obs (fake + authorize path) | Reconcile via `FEXGetCMP` / `FEXGetLast_CMP`; do not invent a new series. |
| `600` | No se corresponden token con firma / ValidacionDeToken… | Auth token/sign mismatch or stale TA for this call | O / Obs / C | Refresh TA from cache correctly; ensure Token+Sign pair from same TA; check env mix. |
| `500` | Error interno… | Server-side business fault (when returned as FEXErr) | Obs (rebuild abort path) | Bounded retry, then **abort** rebuild — **not** a gap. |
| `1671`–`1674` | (Fecha_pago validations) | `Fecha_pago` required / invalid for tipo 19 + expo 2/4 | **O** WSFEX manual v3.1.1 (since 1.6.0 / 2019-11-01) | Send valid `Fecha_pago` (`YYYYMMDD`). |

### FAC-65 rule (gap vs failure)

On `FEXGetCMP` only:

1. **`1521` → gap** (continue rebuild / record known gap).
2. **Any other `ErrCode ≠ 0` → error** (retry, then abort).
3. **HTTP / timeout / SOAP Fault → error** (retry, then abort). Never synthesize a gap.

If a future WSFEX revision changes the not-found code, extend the allow-list
explicitly — do not treat unknown codes as gaps.

Wire shape: [../captures/fexgetcmp-not-found-1521.xml](../captures/fexgetcmp-not-found-1521.xml).

## WSFEX `FEXEvents`

| EventCode | Example | Meaning | Source | Action |
|-----------|---------|---------|--------|--------|
| (any) | Mantenimiento programado | Advisory / maintenance / normative notice | O / Obs | Log and surface as **warning**, never as hard failure of a successful `ErrCode=0` response. |

## WSAA SOAP faults

| Fault / text | Meaning | Source | Action |
|--------------|---------|--------|--------|
| `coe.alreadyAuthenticated` / “El CEE ya posee un TA válido…” | New TA requested while a valid TA exists for this cert+service | O; **C** AfipSDK frequent-errors & blog | Reuse cached TA until `expirationTime`. Do not hammer LoginCms. Community wait hints (≈10 min homo / ≈2 min prod between *forced* renewals) are operational folklore — contrast with Official: primary fix is **cache reuse**, not busy-wait. |
| `coe.notAuthorized` / computador no autorizado | Cert not linked to the requested WS | O / C | Authorize `wsfex` in WSASS (homo) or production relaciones. |
| Untrusted / wrong-AC cert | Homo cert against prod (or inverse), or self-signed | O / C / Obs | Fix cert↔env pairing. Self-signed → `cms.cert.untrusted` locally. |

## Community codes that are **not** WSFEX

AfipSDK “errores frecuentes” lists WSFEv1 codes such as **`10016`** (“número o fecha no se corresponde con el próximo a autorizar”) and PV errors like **`11002`**. Those apply to **WSFEv1**, not WSFEXv1.

**Contrast:** On WSFEX, align with `FEXGetLast_CMP` and expect WSFEX-specific rejects (e.g. **1462** for number reuse). Do not paste WSFEv1 ErrCodes into WSFEX diagnostics without checking the WSFEX manual / response.

## References

- FAC-65 doc in repo: `docs/wsfex-gap-vs-error.md`
- Product mapping: `facturador/arca/wsfex.py` (`CMP_NOT_FOUND_CODES`, `CmpNotFoundError`)
- Citations: [sources.md](sources.md)

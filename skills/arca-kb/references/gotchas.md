# Confirmed gotchas (WSAA / WSFEXv1)

Only items with Official, Observed, and/or cited Community evidence.

## 1. `Fecha_pago` round-trips on `FEXGetCMP` (FAC-63)

**Suspect (rejected):** that `Fecha_pago` is authorize-only and lost on consult.

| Evidence | Label |
|----------|-------|
| WSFEX manual v3.1.1 changelog **1.6.0** (2019-09-23): field added to authorize **and** GetCMP response | Official |
| Live homo WSDL type `ClsFEXGetCMPR` includes `Fecha_pago` (2026-07-17) | Observed |
| Validations 1671–1674: mandatory for Factura E tipo 19 + `Tipo_expo` 2/4 | Official |
| Operator fidelity matrix: `Fecha_pago` returned matching authorized value (e.g. issue `20260723`, payment `20260724`) | Observed |

**Implication:** rebuild-from-ARCA does **not** need a per-número sidecar for `Fecha_pago`. Emisor PDF chrome (razón social, etc.) never travels in FEXAuthorize — that stays profile/seed-local.

## 2. Reproceso requires same `Id` **and** same payload

**Official / Observed:** retrying `FEXAuthorize` with the same client `Id` and identical Cmp data returns the prior CAE (`Reproceso=S` — field documented in WSFEX authorize response). Same number with a **different** `Id` → reject (number already taken / not next-to-authorize).

**ErrCode note:** this project’s fake/skill historically label that reject as **`1462`** (“Nro de comprobante ya utilizado”). That exact code is **Pending** wire confirmation (not in manual v3.1.1 error tables as of 2026-07-28). Official sequence validations (~**1520** / next-to-authorize text) still require reconciling with `FEXGetLast_CMP` / `FEXGetCMP` rather than inventing a series. Capture a redacted live dump when seen and promote the code to **Obs**.

**Practical:** persist `Id` + `raw_request` before the SOAP call; on retry reuse both; verify CAE against persisted request before accepting.

## 3. Concurrent numbering

**Observed:** ARCA numbering per `(PV, tipo)` is strictly sequential. Two parallel authorize attempts race on `FEXGetLast_CMP + 1`. Serialize issuance for a given PV.

## 4. Cotización del día

**Official / design Observed:** foreign-currency Cmp must carry `Moneda_ctz` from ARCA (`FEXGetPARAM_Ctz` for the emission date), not a private FX feed. Emission date for this product’s happy path is the collection day.

## 5. TA cache vs `alreadyAuthenticated`

**Official:** one valid TA per cert/service; LoginCms while valid → fault.

**Community (cited):** AfipSDK documents `coe.alreadyAuthenticated` and suggests waiting ~10 min (dev) / ~2 min (prod) between *forced* new TAs, and always caching ([AfipSDK errores frecuentes](https://docs.afipsdk.com/recursos/errores-frecuentes), [AfipSDK blog](https://afipsdk.com/blog/solucion-a-error-alreadyauthenticated/)). GitHub AfipSDK/afip.php#95 reports the same in multi-server setups without shared TA storage.

**Contrast:** Community wait timers are a **workaround for forced renewals**. The durable fix (Official + Observed product behavior) is: **persist TA until `expirationTime` (~12 h)** and never call LoginCms on every business request. If fault appears with empty local cache, another process likely holds the TA — wait for expiry or restore the cache file; do not spin LoginCms.

## 6. Maintenance / `FEXEvents`

**Official / Observed:** successful responses may still include `FEXEvents` (maintenance windows, normative notices). Log and warn; do not fail the call solely because events are present.

## 7. WSFEX ≠ WSFEv1 (RG 5616 / Condición IVA receptor)

**Official / design:** RG 5616 forced receptor IVA condition on **WSFEv1**. Export receptor on WSFEX is foreign: relevant fields are `Id_impositivo` + `Cuit_pais_cliente` / `Dst_cmp`, not the domestic Condición IVA field set.

**Community:** AfipSDK and related posts often discuss RG 5616 in a **WSFEv1** context. **Contrast** before applying those field requirements to WSFEX templates.

## 8. No range query for rebuild

**Observed (FAC-65):** only one-by-one `FEXGetCMP`. Portal “Mis Comprobantes” CSV is manual cross-check, not an API.

## 9. Incoterms empty on services

**Observed:** services invoices often authorize with empty `Incoterms`; GetCMP may return empty string. Empty ≠ “field missing from schema”. FAC-63 noted empty Incoterms when not populated on authorize — not a fidelity blocker when the product does not send it for `tipo_expo=2`.

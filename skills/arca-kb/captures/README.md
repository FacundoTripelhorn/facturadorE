# Redacted homologación captures

XML **shapes** for agents. Values are placeholders. These are **not** live
secrets and must stay that way.

## Redaction checklist (design.md §2.1.1 + FAC-66)

Before adding or editing a capture, verify **all** of:

| # | Rule | Status required |
|---|------|-----------------|
| 1 | No WSAA/WSFEX `Token` or `Sign` plaintext | Must be `REDACTED_TOKEN` / `REDACTED_SIGN` or omitted |
| 2 | No CMS / PKCS#7 (`loginCms` payload) | Omit or `REDACTED_CMS` |
| 3 | No certificate PEM/DER or private key material | Absent |
| 4 | No real fiscal CUIT (use clearly fake `20000000001`-style) | Fake only |
| 5 | No real customer names, addresses, tax IDs, or CAE from production | Anonymized / fake |
| 6 | No production secrets from logs | N/A |
| 7 | Label file purpose in the XML comment header | Present |

§2.1.1 point 9 (never log Token/Sign/CMS) is the product rule these captures
mirror for agent-facing examples.

## Files

| File | What it shows |
|------|----------------|
| `fexgetcmp-not-found-1521.xml` | FAC-65 gap signal on `FEXGetCMP` |
| `fexgetcmp-success-redacted.xml` | Successful GetCMP including `Fecha_pago` + Items (FAC-63 shape) |
| `fexauthorize-success-redacted.xml` | Authorize result with `Reproceso` |
| `wsaa-already-authenticated-fault.xml` | WSAA fault when TA already valid |

Auth blocks in WSFEX examples use redacted Token/Sign so the **element structure**
remains visible without credentials.

# WSFEX: distinguishing "comprobante does not exist" from errors (FAC-65)

**Status:** Documented for rebuild/catch-up gap handling.  
**Related:** `facturador/arca/wsfex.py` (`CmpNotFoundError`, `CMP_NOT_FOUND_CODES`),
`facturador/reconstruct.py`.

## Finding

On **WSFEXv1 `FEXGetCMP`**, ARCA signals a missing voucher with a business
`FEXErr` (not a SOAP Fault / HTTP transport failure):

| Signal | Meaning for FAC-65 |
|--------|--------------------|
| `FEXErr.ErrCode = 1521` | **Does not exist** — confirmed gap. Message typically *"No existen datos para el comprobante"*. Record a known-gap row and continue. |
| Other `FEXErr.ErrCode ≠ 0` | Business / server error — **not** a gap. Bounded retry, then **abort** the rebuild. |
| HTTP error, timeout, SOAP Fault | Transient / transport — bounded retry, then **abort**. Never invent a gap. |

`WsfexClient.call` → `_raise_on_error` maps `1521` to `CmpNotFoundError`
(subclass of `WsfexError`). Reconstruct treats only that type as a gap.

## Evidence

1. **In-repo fake** (`tests/arca_fake.py`): for an unknown `(tipo, PV, nro)`,
   `FEXGetCMP` returns ErrCode `1521` with the message above. Production
   rebuild tests rely on this contract.
2. **Product code already depended on it:** `InvoiceService._try_reconcile`
   treats a `WsfexError` from `get_cmp` as "ARCA does not register this
   number" (safe to retry authorize). The rebuild path makes the same code
   explicit as `CmpNotFoundError`.
3. **Live homologación probe** (operator with a WSASS cert): query
   `FEXGetCMP` for `Cbte_nro = FEXGetLast_CMP + 1` on a known PV/tipo. Expect
   ErrCode `1521` (or an equivalent not-found code). If a future WSFEX
   revision changes the code, add it to `CMP_NOT_FOUND_CODES` and update
   this doc — do **not** treat unknown codes as gaps.

```bash
# Optional operator check (not part of pytest / CI):
ARCA_ENV=homo uv run python -c "
from facturador.config import load_config
from facturador.profile import EnvironmentProfile
from facturador.constants import ArcaEnvironment
from facturador.arca.wsfex import WsfexClient, CmpNotFoundError
from facturador.arca.wsaa import WsaaClient
p = EnvironmentProfile.resolve(ArcaEnvironment.HOMO)
c = load_config(p)
w = WsfexClient(c, wsaa=WsaaClient(c))
last = w.get_last_cmp(1, 19)
try:
    w.get_cmp(19, 1, last + 1)
    print('UNEXPECTED: beyond-last returned data')
except CmpNotFoundError as e:
    print(f'OK not-found: {e.code} {e.message}')
"
```

## Fallback

If not-found and error become indistinguishable on the wire, reconstruct
supports `--abort-on-any-gap` / `abort_on_any_gap=True` (manual override):
any missing number aborts instead of recording a gap.

## Range queries

WSFEXv1 still has **no** bulk / range / paginated consult API. Rebuild uses
one `FEXGetCMP` per number. ("Mis Comprobantes" CSV remains portal-only.)

# Known non-bugs (agents)

Intentional behaviors and environment limitations that agents often misdiagnose
as product bugs. **Do not “fix” these unless the task explicitly changes
product design** (see [`docs/design.md`](../design.md)).

This document is **not** a blanket excuse for failures. If behavior contradicts
[`docs/design.md`](../design.md), tests, or the acceptance criteria of the task
at hand, treat it as a real bug.

For commands, constraints, and the expected agent response format, see
[`AGENTS.md`](../../AGENTS.md). For architecture diagrams, see
[`architecture-graphs.md`](architecture-graphs.md). Environment/profile
contract: [`docs/adr/0001-perfiles-de-ambiente-aislados.md`](../adr/0001-perfiles-de-ambiente-aislados.md).

---

## Localhost-only binding (native / uvicorn)

**Symptom:** The app listens on `127.0.0.1` only; it is unreachable from other
hosts on the LAN or from a remote machine.

**Expected.** The product is a single-user local tool (~1 invoice/week). Binding
to localhost is a deliberate security decision: the only inbound surface is the
user’s own browser; outbound HTTPS to ARCA is the only other network use.

Implementation: `facturador/__main__.py` sets `host = "127.0.0.1"` when not
running in Docker. Design rationale: [`docs/design.md`](../design.md) §2.5.

**Do not:**

- Change uvicorn to `0.0.0.0` on the host.
- Add reverse proxies, tunnels, or auth layers to expose the service on a network.
- “Fix” connectivity from another machine by widening the bind address.

**Legitimate follow-ups** (only when explicitly scoped): better error messages
when the user opens the wrong URL, or documentation — not widening the bind.

Opening the app via a non-loopback hostname (or a DNS-rebinding hostname that
resolves to 127.0.0.1) yields **400 Host no permitido** (FAC-41). That is
intentional middleware, not a routing bug.

---

## Docker: internal `0.0.0.0` bind, localhost-only publish

**Symptom:** Inside the container, the process listens on `0.0.0.0:8399`, but the
app is still only reachable at `http://127.0.0.1:PORT` on the host.

**Expected.** Docker port forwarding requires the process inside the container
to listen on all interfaces. The **security boundary** is `docker-compose.yml`,
which publishes **only** `127.0.0.1:${FACTURADOR_PORT:-8399}:8399` — never
`0.0.0.0:PORT` on the host side.

| Layer | Bind / publish | Why |
|-------|----------------|-----|
| Host (uvicorn, no Docker) | `127.0.0.1` | Direct localhost-only |
| Container process | `0.0.0.0` (when `FACTURADOR_IN_DOCKER=1`) | Docker port mapping |
| Host port publish | `127.0.0.1:PORT` only | Network isolation |

`FACTURADOR_IN_DOCKER` is set only in the `Dockerfile`; the host bind address
is not configurable by design (`facturador/__main__.py`). Compose injects
`FACTURADOR_PUBLIC_PORT` from the host-side `FACTURADOR_PORT` so the FAC-41
Host/Origin allowlist accepts browser requests on a custom published port
while the container keeps listening on 8399.

**Do not:**

- Remove `FACTURADOR_IN_DOCKER` handling and force `127.0.0.1` inside the
  container (breaks Docker healthcheck and port forward).
- Change compose to publish `0.0.0.0:PORT` on the host.
- Drop `FACTURADOR_PUBLIC_PORT` without another way to allowlist the Docker
  host publish port (browser Host would 400 on non-default mappings).

---

## Missing real ARCA homologación credentials

**Symptom:** ARCA-backed endpoints return 5xx (`WsaaError`, SOAP faults, health
check failures) once the profile is ``ready``. `GET /` may return 500 when a
default client exists and WSAA cannot obtain a TA. If setup is incomplete,
invoice/ARCA routes (including `GET /`) return **503** with `setup_state`
(FAC-35) — that is the onboarding guard, not a credential bug.

**Expected** in environments without a registered homologación certificate
(CI, fresh clones, agent VMs, offline dev).

What works **without** real ARCA credentials:

- Client management (`/clientes`, `POST /clients`) once `arca_params` is seeded
  (see `tests/conftest.py::seed_params`).
- Full test suite (`tests/arca_fake.py` simulates WSFEX in-process).

What **requires** a real cert registered in WSASS and authorized for `wsfex`:

- Home cotización, invoice draft with live params, authorize, `/health` ARCA
  checks, and scripts `scripts/get_ta.py`, `scripts/check_wsfex.py`.

CI intentionally does **not** upload certificates; contract tests against real
homologación are local-only (`.github/workflows/ci.yml`).

**Do not:**

- “Fix” 5xx on ARCA-backed routes by swallowing `WsaaError` on the home handler
  unless the task explicitly asks for better UX around missing credentials.
- Add certs to the repo or CI.

**Fix:** Follow [`docs/setup-homologacion.md`](../setup-homologacion.md) — place
`secrets/cert.crt` and `secrets/cert.key` on the Homologación profile, register
in WSASS, authorize service `wsfex`.

---

## Incomplete profile setup returns 503 (FAC-35)

**Symptom:** `POST /invoices`, `POST …/authorize`, `GET /`, `/params/…`, or
`GET /health/arca` return **503** with `setup_state` such as
`certificate_required` or `emisor_required`.

**Expected.** Each profile stores setup progress in `data/onboarding.json`.
Until the state is `ready` (valid cert pair + active complete emisor with at
least one point of sale), the guard blocks invoice and ARCA operations.
`GET /health` and `GET /setup` stay available; `/configuracion` remains open
so emisor/PV can be completed (FAC-38).

**Do not** treat this as a broken health check or a missing exception handler.
Inspect `GET /setup` and finish the pending step (FAC-37/38 UI, or manual
certs + Configuración).

---

## Self-signed certificate limitations

**Symptom:** WSAA rejects the login CMS with `cms.cert.untrusted` (or similar)
when using a test-generated cert.

**Expected.** ARCA WSAA only accepts certificates issued through WSASS
(homologación) or the production certificate workflow. A self-signed pair is
useful for **local unit tests** (subject must include `CUIT <11 digits>`; recipe
in `tests/conftest.py`) but will **never** obtain a real TA from WSAA.

**Do not:**

- Patch WSAA validation or disable certificate checks in production code.
- Assume a self-signed cert is sufficient to validate authorize/cotización flows
  against real homologación endpoints.

**Fix:** Use a WSASS-issued homologación certificate, or restrict testing to
`tests/arca_fake.py` / offline flows.

---

## SQLite as intentional source of truth (per profile)

**Symptom:** Domain configuration (emisor, punto de venta, S3 backup bucket,
clients, invoices) lives in the **active profile’s** SQLite DB, not in `.env`
or environment variables. Agents may look for “missing env vars” for emisor/PV
or expect a shared `~/facturador/data/facturador.db` for both environments.

**Expected.** Each Homologación / Producción profile owns its DB (~1 invoice/week,
single user, single process per environment). Bootstrap `.env` under
`FACTURADOR_HOME` is optional for non-launcher entrypoints (`ARCA_ENV`,
`FACTURADOR_PORT`); CUIT comes from the certificate. Emisor and backup settings
are edited via `/configuracion` and travel inside encrypted backups of that
profile.

Invoice numbering is **not** a local-only counter: always reconcile with ARCA
`FEXGetLast_CMP` before authorizing.

**Do not:**

- Move emisor/PV/backup config into `.env` without an explicit design change.
- Add Postgres, Redis, queues, or sync services to “fix” perceived scale limits.
- Rely on a local sequence alone for `Cbte_nro`.
- Assume one shared DB holds both homologación and producción data.

Multi-machine use is via **restore from S3**, not live replication
([`docs/design.md`](../design.md) §2.5).

---

## No hot environment switching

**Symptom:** Editing `ARCA_ENV`, swapping certs, or expecting the UI to change
WSAA/WSFEX clients inside a running backend does not switch environments
in-process. “Cambiar ambiente” stops the current backend and starts another.

**Expected.** ADR 0001 invariant: one backend process = one immutable
environment + one profile. The launcher orchestrates restart-based switching.
There is one FacturadorE app — not two installations — with two isolated
profiles.

**Do not:**

- Implement in-process hot switching of ARCA clients, certificates, or DB.
- Document or recommend editing `.env` mid-flight as the way to change
  environment without restart.
- Treat restart-on-change as a bug.

---

## WSFEX vs WSFEv1 (do not mix export with domestic)

**Symptom:** Agent searches for WSFEv1 / `FECAESolicitar` / domestic Factura A/B/C
patterns, or tries to authorize Factura E through the wrong ARCA service.

**Expected.** **Factura E (exportación)** uses **WSFEX / WSFEXv1**, not WSFEv1
(the domestic “común” web service). This repo implements only the export path.

| | WSFEv1 | WSFEX / WSFEXv1 |
|---|--------|-----------------|
| Use case | Domestic A/B/C, monotributo, etc. | Export invoices (Factura E, NC/ND E) |
| Auth service name in TRA | `wsfe` (etc.) | `wsfex` |
| Key SOAP ops | `FECAESolicitar`, … | `FEXAuthorize`, `FEXGetLast_CMP`, … |
| In this repo | **Not implemented** | `facturador/arca/wsfex.py` |

Cbte types for export (verify with `FEXGetPARAM_Cbte_Tipo`): `19` Factura E,
`20` Nota de Débito E, `21` Nota de Crédito E. WSFEX requires extra fields
(items, moneda + cotización, país destino, Incoterms, etc.) that WSFEv1 does not.

**Do not:**

- Add WSFEv1 clients or map Factura E to domestic WSFE calls.
- Copy domestic pyafipws/ARCA examples without switching to WSFEX method names
  and parameter tables.

Authoritative domain section: [`docs/design.md`](../design.md) §1.1.

---

## Multi-emisor: schema ready, selection UI pending (FAC-8)

**Symptom:** The `emisores` table can hold multiple rows in the same profile
DB, but without an explicit `active_emisor_id` there is no operative emisor.
`/configuracion` does not offer a full alta/lista UX yet. Environment is not a
user-editable field on the emisor form (it is a profile seal).

**Expected.** Emisores are **local to the profile** (FAC-26 / FAC-27). Runtime
uses `active_emisor_id` (`facturador/settings.py`). Invoicing uses
`Emisor.punto_venta` (first entry in `puntos_venta`). FAC-8 (richer multi-emisor
UI / PV selection) is still open.

**Do not:**

- Reintroduce per-emisor environment selection in the API/UI.
- Delete “duplicate” emisor rows as a bug fix unless the task explicitly covers
  data migration.
- Move emisor fields back into the flat `settings` key/value table.
- Assume `/configuracion` already implements full alta de emisores — that is FAC-8.

**Fix (when scoped):** implement FAC-8 (alta/lista/selección de emisor y PV).
Until then, see [`repo-map.md`](repo-map.md) § Emisor entity / multi-emisor.

---

## Quick triage

| Observation | Likely cause | Action |
|-------------|--------------|--------|
| Cannot reach app from another PC | Localhost-only bind | Expected; do not widen bind |
| `0.0.0.0` in `docker ps` / container logs | Docker internal listen | Expected; check host publish is `127.0.0.1` |
| 5xx on authorize / cotización | No real homologación cert | Setup credentials or use tests |
| `cms.cert.untrusted` | Self-signed test cert | WSASS cert or `arca_fake` tests |
| Emisor not in `.env` | Config in profile SQLite | Use `/configuracion` or DB seed |
| Looking for WSFEv1 code | Wrong service for Factura E | Use WSFEX (`FEX*` methods) |
| Extra `emisores` rows / no active | FAC-8 UI incomplete | Expected; set `active_emisor_id` |
| Env did not change in-process | Restart-based switch | Expected; use launcher / Cambiar ambiente |
| Shared `~/facturador/data` for both envs | Old shared-home model | Use per-profile roots via `ProfilePaths` |

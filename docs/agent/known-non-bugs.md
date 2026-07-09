# Known non-bugs (agents)

Intentional behaviors and environment limitations that agents often misdiagnose
as product bugs. **Do not “fix” these unless the task explicitly changes
product design** (see [`docs/design.md`](../design.md)).

This document is **not** a blanket excuse for failures. If behavior contradicts
[`docs/design.md`](../design.md), tests, or the acceptance criteria of the task
at hand, treat it as a real bug.

For commands, constraints, and the expected agent response format, see
[`AGENTS.md`](../../AGENTS.md). For architecture diagrams, see
[`architecture-graphs.md`](architecture-graphs.md).

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
is not configurable by design (`facturador/__main__.py`).

**Do not:**

- Remove `FACTURADOR_IN_DOCKER` handling and force `127.0.0.1` inside the
  container (breaks Docker healthcheck and port forward).
- Change compose to publish `0.0.0.0:PORT` on the host.

---

## Missing real ARCA homologación credentials

**Symptom:** ARCA-backed endpoints return 5xx (`WsaaError`, SOAP faults, health
check failures). `GET /` may return 500 when a default client exists and WSAA
cannot obtain a TA.

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
`secrets/homo.crt` and `secrets/homo.key` under `FACTURADOR_HOME`, register in
WSASS, authorize service `wsfex`.

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

## SQLite as intentional source of truth

**Symptom:** Domain configuration (emisor, punto de venta, S3 backup bucket,
clients, invoices) lives in `FACTURADOR_HOME/data/facturador.db`, not in `.env`
or environment variables. Agents may look for “missing env vars” for emisor/PV.

**Expected.** SQLite is the local source of truth by design (~1 invoice/week,
single user, single process). Bootstrap `.env` holds only `ARCA_ENV` and optional
`FACTURADOR_PORT`; CUIT comes from the certificate. Emisor and backup settings
are edited via `/configuracion` and travel inside encrypted backups.

Invoice numbering is **not** a local-only counter: always reconcile with ARCA
`FEXGetLast_CMP` before authorizing.

**Do not:**

- Move emisor/PV/backup config into `.env` without an explicit design change.
- Add Postgres, Redis, queues, or sync services to “fix” perceived scale limits.
- Rely on a local sequence alone for `Cbte_nro`.

Multi-machine use is via **restore from S3**, not live replication
([`docs/design.md`](../design.md) §2.5).

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

## Quick triage

| Observation | Likely cause | Action |
|-------------|--------------|--------|
| Cannot reach app from another PC | Localhost-only bind | Expected; do not widen bind |
| `0.0.0.0` in `docker ps` / container logs | Docker internal listen | Expected; check host publish is `127.0.0.1` |
| 5xx on authorize / cotización | No real homologación cert | Setup credentials or use tests |
| `cms.cert.untrusted` | Self-signed test cert | WSASS cert or `arca_fake` tests |
| Emisor not in `.env` | Config in SQLite | Use `/configuracion` or DB seed |
| Looking for WSFEv1 code | Wrong service for Factura E | Use WSFEX (`FEX*` methods) |

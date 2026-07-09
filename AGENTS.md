# AGENTS.md

Shared contract for coding agents (Cursor, Claude, Codex, and future tools)
working on **facturador**. Read this file first; follow links for depth instead
of exploring the repo blindly.

## Mandatory first reads

1. This file (`AGENTS.md`) — project shape, commands, constraints, output format.
2. [`docs/agent/repo-map.md`](docs/agent/repo-map.md) — **where code lives by task type**;
   read this before broad repository search or exploratory greps.
3. [`docs/agent/verification-matrix.md`](docs/agent/verification-matrix.md) — **which
   checks to run** by task type (default lint/types/tests order and narrow scopes).
4. [`docs/agent/architecture-graphs.md`](docs/agent/architecture-graphs.md) — **Mermaid
   diagrams** for runtime shape, boundaries, flows, and security (navigation aids;
   code/tests/`docs/design.md` are authoritative).
5. [`docs/agent/known-non-bugs.md`](docs/agent/known-non-bugs.md) — **intentional
   behaviors and environment limits** agents often misdiagnose as bugs (localhost
   bind, Docker publish, ARCA credentials, WSFEX vs WSFEv1).
6. [`README.md`](README.md) — user-facing overview, data layout, Docker vs local dev.
7. [`docs/design.md`](docs/design.md) — architecture, domain rules, security decisions.
8. Setup for the active environment:
   - [`docs/setup-homologacion.md`](docs/setup-homologacion.md) — homologación (start here).
   - [`docs/setup-produccion.md`](docs/setup-produccion.md) — production.
9. Other agent supplements: [`docs/agent/`](docs/agent/).

## Project shape

- **Stack:** Python 3.12, single-process FastAPI (JSON API + Jinja/HTMX frontend),
  SQLite datastore, WeasyPrint PDFs.
- **Domain:** Argentine export invoices ("Factura E") via ARCA WSAA/WSFEX SOAP.
- **Layout:** one service package under `facturador/`; tests in `tests/`; scripts in
  `scripts/`; schema in `facturador/schema.sql`.
- **Dev entrypoint:** `uv run python -m facturador` (binds `127.0.0.1:8399`; port
  override via `FACTURADOR_PORT`).
- **Tooling:** [uv](https://docs.astral.sh/uv/). Dev deps live in the `dev`
  dependency-group.

## Standard commands

Run from the repo root after `uv sync`:

| Purpose | Command |
|---|---|
| Install / refresh deps | `uv sync` |
| Lint | `uv run ruff check .` |
| Types | `uv run mypy` |
| Tests | `uv run pytest` |

Tests use an in-process ARCA fake (`tests/arca_fake.py`); no network or real
credentials required. CI runs the same three checks (see `.github/workflows/ci.yml`).
For narrower scopes by task type, see
[`docs/agent/verification-matrix.md`](docs/agent/verification-matrix.md).

## Local dev prerequisites

Data lives under `FACTURADOR_HOME` (default `~/facturador`), outside the repo.
On first start the app creates the home layout and a bootstrap `.env`; only the
cert/key pair must be placed manually. See the setup docs and
[`README.md`](README.md) § Configuración.

- **Bootstrap `.env`:** read only from `<FACTURADOR_HOME>/.env`, never the CWD.
  Auto-created with `ARCA_ENV=homo` if missing. Holds only `ARCA_ENV` and
  optional `FACTURADOR_PORT`; CUIT is extracted from the certificate.
- **App config:** emisor fields, punto de venta, and S3 backup settings live in
  SQLite and are edited via `/configuracion` — they travel inside encrypted
  backups, not in `.env`.
- **Backups:** `facturador.backup` reads the S3 bucket/prefix from the `settings`
  row in the snapshot being backed up. On a fresh machine with no DB yet,
  `facturador.restore --latest` needs `--bucket`/`--prefix` on the CLI.
- **Certificates:** `secrets/<env>.crt` and `secrets/<env>.key` (mode 400/600).
  Private keys have **no passphrase** (product decision). All gitignored.

For offline work without real ARCA credentials:

- Point `FACTURADOR_HOME` at a test directory, place a self-signed pair there
  (subject must include `CUIT <11 digits>`). Recipe: `tests/conftest.py`.
- **Client management** (`/clientes`, `POST /clients`) works fully offline once
  `arca_params` is seeded (`facturador/repo/params.py::replace_params`;
  `seed_params` in `tests/conftest.py`).
- **ARCA-backed flows** (home cotización, invoice draft, authorize, health check)
  need a real certificate registered in ARCA homologación (WSASS) and authorized
  for `wsfex`. A self-signed cert is rejected (`cms.cert.untrusted`).

## Architectural and security rules

Do not regress these without an explicit design change in `docs/design.md`:

- **Localhost only.** The app must not be made externally reachable. Bind to
  `127.0.0.1` on the host; Docker publishes only `127.0.0.1:PORT`. Do not change
  uvicorn to `0.0.0.0` outside the container, add reverse proxies, tunnels, or
  auth layers to expose the service on a network.
- **Secrets stay local.** Cert/key pairs never belong in the repo, CI, or logs.
  Key files must remain mode 400/600.
- **SQLite is the local source of truth.** Domain config (emisor, punto de
  venta, backup bucket) lives in the DB, not in env vars. No external DB, queues,
  or sync services unless explicitly scoped.
- **ARCA is authoritative for numbering.** Always reconcile with `FEXGetLast_CMP`;
  never rely on a local counter alone.
- **Single-process, no workers.** Volume is ~1 invoice/week; keep the stack simple.
- **Redact sensitive values in logs** (TA token/sign, CMS payloads).

## Known environment limitations (not product bugs)

Full detail: [`docs/agent/known-non-bugs.md`](docs/agent/known-non-bugs.md).

- Missing or untrusted ARCA credentials cause 5xx on ARCA-backed endpoints.
  This is expected — fix credentials, not application error handling, unless the
  task explicitly asks for better UX around that case.
- `GET /` may return 500 when a default client exists and WSAA fails
  (`WsaaError` is not swallowed on the home handler).
- Contract tests against real homologación (`scripts/get_ta.py`,
  `scripts/check_wsfex.py`) are intentionally local-only; CI does not upload certs.

## Expected agent response format

End every task with these four sections:

1. **Summary** — what changed and why, in plain language.
2. **Changed files** — list of paths touched (or "none" for read-only tasks).
3. **Verification** — commands run and their results (lint, types, tests, manual
   checks). Say explicitly if something was not run and why.
4. **Remaining risks** — open questions, credential gaps, follow-ups, or edge
   cases not covered.

Keep responses proportional to task complexity; do not pad with unrelated detail.

## Agent-specific supplements

Tool-specific notes belong in `docs/agent/` when they exist. Until then:

- **Cursor Cloud:** dependency refresh may be automated by the environment
  (`uv sync` on boot). WeasyPrint system libs (pango/cairo/gdk-pixbuf) are
  preinstalled in the base image. ARCA homologación endpoints are network-reachable
  from the VM, but a self-signed cert is still rejected by WSAA.

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
10. For **larger tasks** (multi-file, ARCA, schema, or ambiguous scope), start
    from [`docs/agent/task-brief-template.md`](docs/agent/task-brief-template.md)
    — paste the scaffold into the Linear issue or prompt so goal, constraints,
    and verification are explicit without duplicating this file.

## Project shape

- **Stack:** Python 3.12, single-process FastAPI (JSON API + Jinja/HTMX frontend),
  SQLite datastore, WeasyPrint PDFs.
- **Domain:** Argentine export invoices ("Factura E") via ARCA WSAA/WSFEX SOAP.
- **Environments:** one app, launcher-selected **Homologación** / **Producción**,
  isolated hidden profiles, one immutable environment per backend process
  ([ADR 0001](docs/adr/0001-perfiles-de-ambiente-aislados.md)).
- **Layout:** one service package under `facturador/`; tests in `tests/`; scripts in
  `scripts/`; schema baseline in `facturador/schema.sql`, applied via
  `facturador/migrations/` on connect (FAC-43).
- **Dev entrypoint:** `uv run python -m facturador.launcher` (chooser; binds
  `127.0.0.1:8399`; port override via `FACTURADOR_PORT` / `--port`). Backend
  alone: `uv run python -m facturador` with an explicit `ARCA_ENV`.
- **Tooling:** [uv](https://docs.astral.sh/uv/). Dev deps live in the `dev`
  dependency-group.

## Standard commands

Run from the repo root after `uv sync`:

| Purpose | Command |
|---|---|
| Install / refresh deps | `uv sync` (commits `uv.lock` when deps change) |
| Lint | `uv run ruff check .` |
| Types | `uv run mypy` |
| Tests | `uv run pytest` |

Tests use an in-process ARCA fake (`tests/arca_fake.py`); no network or real
credentials required. CI runs `uv sync --frozen` then the same three checks
(see `.github/workflows/ci.yml`).
For narrower scopes by task type, see
[`docs/agent/verification-matrix.md`](docs/agent/verification-matrix.md).

## Local dev prerequisites

Runtime state lives in **launcher-selected isolated profiles** (ADR 0001), not
in a shared home with both environments mixed. The launcher
(`uv run python -m facturador.launcher`) offers **Homologación** /
**Producción**; each maps to a hidden profile (OS app-data) owning its own DB,
certs, PDF cache, WSAA TA cache, params cache, logs, onboarding, and backups.
One backend process = one immutable environment. Changing environment restarts
the backend — no hot switching. See setup docs and [`README.md`](README.md).

- **Environment selection:** launcher chooser (or `--env homo|prod`). Backend
  entrypoints without the launcher need an explicit `ARCA_ENV` in the process
  environment (or bootstrap `.env`). There is no silent default to homologación.
- **Bootstrap `.env`:** optional, read only from `<FACTURADOR_HOME>/.env`
  (default `~/facturador`), never the CWD. Holds optional `ARCA_ENV` /
  `FACTURADOR_PORT` for non-launcher entrypoints; CUIT comes from the
  certificate. Authoritative contract:
  [`docs/adr/0001-perfiles-de-ambiente-aislados.md`](docs/adr/0001-perfiles-de-ambiente-aislados.md).
- **App config:** emisor fields, punto de venta, and S3 backup settings live in
  the **profile SQLite** and are edited via `/configuracion` — they travel
  inside encrypted backups, not in `.env`. Emisor environment is not user-
  selectable (profile-local; `ambiente` is a seal).
- **Backups:** `facturador.backup --env homo|prod` backs up one profile. On a
  fresh machine with no DB yet, `facturador.restore --latest` needs
  `--bucket`/`--prefix` on the CLI.
- **Certificates:** `secrets/cert.crt` and `secrets/cert.key` under the active
  profile (mode 400/600). Private keys have **no passphrase** (product
  decision). All gitignored. Paths resolve via `ProfilePaths`
  (`facturador/profile.py`).

For offline work without real ARCA credentials:

- Use a test profile root (`EnvironmentProfile.for_testing` / fixtures in
  `tests/conftest.py`), place a self-signed pair as `cert.crt`/`cert.key`
  (subject must include `CUIT <11 digits>`).
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
- **SQLite is the local source of truth (per profile).** Domain config (emisor,
  punto de venta, backup bucket) lives in the profile DB, not in env vars. No
  external DB, queues, or sync services unless explicitly scoped.
- **One environment per backend process.** Environment is fixed at boot from the
  selected profile; no in-process hot switching of ARCA clients, certs, or DB.
- **ARCA is authoritative for numbering.** Always reconcile with `FEXGetLast_CMP`;
  never rely on a local counter alone.
- **Single-process, no workers.** Volume is ~1 invoice/week; keep the stack simple.
- **Redact sensitive values in logs** (TA token/sign, CMS payloads).

## Known environment limitations (not product bugs)

Full detail: [`docs/agent/known-non-bugs.md`](docs/agent/known-non-bugs.md).

- Incomplete profile setup returns **503** with `setup_state` on invoice/ARCA
  routes until `ready` (FAC-35); `GET /health` and `GET /setup` stay up.
- Missing or untrusted ARCA credentials cause 5xx on ARCA-backed endpoints
  once the profile is ready. This is expected — fix credentials, not application
  error handling, unless the task explicitly asks for better UX around that case.
- `GET /` may return 500 when setup is ready, a default client exists, and WSAA
  fails (`WsaaError` is not swallowed on the home handler).
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

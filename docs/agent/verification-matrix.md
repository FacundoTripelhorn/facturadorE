# Verification matrix (agents)

Task-oriented guidance for **which checks to run** after a change. Read
[`repo-map.md`](repo-map.md) first to locate code; use this file to avoid
running the full suite when a narrower scope is enough.

For project constraints and the expected agent response format, see
[`AGENTS.md`](../../AGENTS.md).

---

## Default verification order

Run from the repo root after `uv sync`. **Always use this order** when you
need full confidence (or before opening a PR):

| Step | Command | When required |
|------|---------|---------------|
| 1 | `uv run ruff check .` | Any Python change |
| 2 | `uv run mypy` | Any Python change |
| 3 | `uv run pytest` | Any behavior change |

CI runs the same three steps in `.github/workflows/ci.yml` (`checks` job). A
separate `docker` job runs `docker build -t facturador:ci .` — add that
locally only when `Dockerfile`, `docker-compose.yml`, or `docker/entrypoint.sh`
change.

**Docs-only changes** (markdown under `docs/`, `README.md`, `AGENTS.md`) do not
need lint, types, or tests unless you edited embedded commands or code samples.

---

## Narrow checks by task type

After the default trio passes (or when iterating quickly), prefer the **smallest
test file set** that covers your edit. Re-run the full default order before
finishing.

| Task type | Likely paths | Narrow verification |
|-----------|--------------|---------------------|
| REST API / invoice service | `facturador/api/`, `facturador/service.py`, `facturador/schemas.py` | `uv run pytest tests/test_api.py tests/test_authorize.py` |
| ARCA clients (WSAA / WSFEX) | `facturador/arca/` | `uv run pytest tests/test_wsaa.py tests/test_wsfex.py` |
| HTMX / HTML UI | `facturador/web/` | `uv run pytest tests/test_web.py` |
| PDF layout / QR | `facturador/pdf/` | `uv run pytest tests/test_pdf.py` (needs WeasyPrint system libs; CI installs them) |
| Row ↔ SOAP mapping | `facturador/mappers.py`, `facturador/constants.py` | `uv run pytest tests/test_mappers.py tests/test_constants.py` |
| Bootstrap config / certs | `facturador/config.py` | `uv run pytest tests/test_config.py` |
| SQLite settings / emisor | `facturador/settings.py`, `facturador/repo/settings.py`, `facturador/repo/emisores.py` | `uv run pytest tests/test_settings.py` |
| Backup / restore CLI | `facturador/backup.py`, `facturador/restore.py` | `uv run pytest tests/test_backup.py` |
| Schema / migrations | `facturador/schema.sql`, `facturador/db.py` | `uv run pytest` (full suite — many modules touch the DB) |
| Docker / launchers | `Dockerfile`, `docker-compose.yml`, `docker/`, `scripts/launch.*` | `docker build -t facturador:ci .` plus targeted pytest if app wiring changed |
| Agent / design docs only | `docs/agent/`, `AGENTS.md`, `docs/design.md` | None required; spot-check links and command snippets |

**Lint/types scope:** `ruff` and `mypy` run on the whole tree; there is no
per-module narrow mode. After a focused edit, still run both once.

**Manual UI check** (optional, localhost only): `uv run python -m facturador`
then open `http://127.0.0.1:8399`. Requires a configured `FACTURADOR_HOME`; ARCA
calls need a real homologación certificate (see below).

---

## ARCA in tests: always use fakes

Normal tests and CI **must not** call ARCA over the network. Integration with
WSAA/WSFEX is exercised in-process:

| Piece | Location | Role |
|-------|----------|------|
| WSFEX simulator | `tests/arca_fake.py` (`FakeArca`, `FakeWsaa`) | Stateful fake: numbering, authorize, param tables, failure modes |
| Shared fixtures | `tests/conftest.py` | Self-signed test cert, `TestClient`, `httpx.MockTransport(arca.handler)` |
| Injected clients | `WsfexClient(..., wsaa=FakeWsaa(), http=MockTransport(...))` | Keeps `facturador/arca/` code paths real without outbound HTTPS |

When adding or changing ARCA-related behavior:

- Extend `FakeArca` / fixtures — do **not** add live SOAP calls to `tests/`.
- Do **not** require real certificates in CI or in `uv run pytest`.
- Do **not** upload fiscal certs to GitHub Actions (see `docs/design.md` and
  `.github/workflows/ci.yml` comments).

Local **contract scripts** under `scripts/` (`get_ta.py`, `check_wsfex.py`,
`authorize_homo.py`) are operator tools, not part of the pytest suite.

---

## When real ARCA credentials are required

| Scenario | Real cert needed? | Notes |
|----------|-------------------|-------|
| `uv run pytest` / CI | **No** | Autosigned test pair + `FakeArca` |
| Client CRUD, params UI (offline) | **No** | Seed `arca_params` via `seed_params` in `tests/conftest.py` |
| `scripts/get_ta.py`, `scripts/check_wsfex.py` | **Yes** | WSASS-registered homologación cert authorized for `wsfex` |
| `scripts/authorize_homo.py` | **Yes** | End-to-end homologación invoice |
| Running the app: cotización, draft, authorize, health | **Yes** | Self-signed cert is rejected (`cms.cert.untrusted`) |
| Production invoicing | **Yes** | Production cert in `FACTURADOR_HOME/secrets/prod.{crt,key}` |

Real credentials live only under `<FACTURADOR_HOME>/secrets/` (mode 400/600),
never in the repo or CI. Missing or untrusted certs causing 5xx on ARCA-backed
endpoints is an **environment limitation**, not a test failure — see
`AGENTS.md` § Known environment limitations.

---

## Reporting skipped verification

Every agent task ends with a **Verification** section (`AGENTS.md`). When you
skip a check, say so explicitly:

1. **Which command** was not run (e.g. `uv run pytest`, `docker build`, manual
   browser check).
2. **Why** — common reasons:
   - Docs-only change
   - Narrow iteration still in progress (note that full suite is pending)
   - Missing WeasyPrint system libraries (PDF tests)
   - No `FACTURADOR_HOME` / no real ARCA cert for manual homologación
   - Out of scope for the task (e.g. user asked for a doc fix only)
3. **Residual risk** — what could still be broken (e.g. "schema change not
   covered by narrow tests; full `pytest` not run yet").

Example:

> **Verification:** `uv run ruff check .` and `uv run mypy` passed. Ran
> `uv run pytest tests/test_web.py` only (HTMX template change). Full
> `uv run pytest` not run — will run before merge. Manual browser check skipped
> (no local `FACTURADOR_HOME`).

Do not claim "all tests pass" if you only ran a subset unless you name the
subset and note that the full suite was not executed.

---

## Related reading

- [`AGENTS.md`](../../AGENTS.md) — standard commands, security rules, response format
- [`repo-map.md`](repo-map.md) — where code lives by task type
- [`docs/design.md`](../design.md) — domain rules and CI/homologación policy

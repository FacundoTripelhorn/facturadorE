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
| 1 | `uv run ruff check .` or `./scripts/agent/lint.sh` | Any Python change |
| 2 | `uv run mypy` or `./scripts/agent/typecheck.sh` | Any Python change |
| 3 | `uv run pytest` or `./scripts/agent/test.sh` | Any behavior change |

**Helper scripts** under `scripts/agent/` wrap the same `uv` commands so agents
do not need to guess tooling. Run `./scripts/agent/doctor.sh` for a full
environment check: Python and `uv` versions, `uv sync`, then lint, typecheck,
and tests in order.

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

## Reporting verification results

Every agent task ends with a **Verification** section (`AGENTS.md`). Use the
status labels below so reviewers can scan results quickly. Report **every**
check that matters for the task — not only failures.

### Status labels

| Status | Meaning | When to use |
|--------|---------|-------------|
| **Passed** | Command ran; exit code 0 | `ruff`, `mypy`, `pytest`, `docker build`, manual check succeeded |
| **Failed** | Command ran; errors found | Include the command and a one-line summary of the failure |
| **Not run** | Deliberately omitted; no blocker | Docs-only change, out of scope, or full suite deferred with reason |
| **Skipped** | Would run but could not | Missing WeasyPrint libs, no `FACTURADOR_HOME`, no homologación cert |
| **Blocked** | External constraint prevents the check | Wrong repo access, CI secret unavailable, network policy |

For **Skipped** or **Blocked**, always add **why** and **residual risk** (what
might still be broken). For **Not run**, say whether the full default trio is
still pending before merge.

### What to include

1. **Which command** — exact invocation (e.g. `uv run pytest tests/test_web.py`,
   `./scripts/agent/doctor.sh`, `docker build -t facturador:ci .`).
2. **Status** — from the table above.
3. **Outcome** — pass count, error snippet, or skip reason.
4. **Residual risk** — only when Not run, Skipped, or Blocked (e.g. "schema
   change not covered by narrow tests; full `pytest` pending").

Do not claim "all tests pass" if you only ran a subset unless you name the
subset and note that the full suite was not executed.

### Examples

**Full default trio (behavior change):**

> **Verification**
> - `uv run ruff check .` — Passed
> - `uv run mypy` — Passed
> - `uv run pytest` — Passed (136 tests)
> - `docker build` — Not run (no Dockerfile changes)

**Narrow iteration (HTMX template):**

> **Verification**
> - `uv run ruff check .` — Passed
> - `uv run mypy` — Passed
> - `uv run pytest tests/test_web.py` — Passed (12 tests)
> - `uv run pytest` (full) — Not run; will run before merge
> - Manual browser check — Skipped (no local `FACTURADOR_HOME`)

**Docs-only:**

> **Verification**
> - `uv run ruff check .` — Not run (markdown only; no code or command samples changed)
> - `uv run pytest` — Not run (docs-only per verification matrix)

**ARCA contract script (operator, not pytest):**

> **Verification**
> - `uv run ruff check .` — Passed
> - `uv run mypy` — Passed
> - `uv run pytest` — Passed (136 tests)
> - `uv run python scripts/check_wsfex.py` — Passed (local homologación cert)
> - CI — N/A (contract scripts are local-only by design)

**Environment limitation (not a product bug):**

> **Verification**
> - `uv run pytest` — Passed (136 tests; uses `FakeArca`, no network)
> - Manual authorize in homologación — Blocked (self-signed cert rejected by WSAA;
>   see [`known-non-bugs.md`](known-non-bugs.md))

### PR description snippet

When opening a PR, a short verification block helps reviewers:

```markdown
## Verification

- [x] `uv run ruff check .`
- [x] `uv run mypy`
- [x] `uv run pytest` (136 passed)
- [ ] `docker build` — not applicable
- [ ] Manual homologación — skipped (no WSASS cert in agent environment)
```

---

## Related reading

- [`AGENTS.md`](../../AGENTS.md) — standard commands, security rules, response format
- [`repo-map.md`](repo-map.md) — where code lives by task type
- [`known-non-bugs.md`](known-non-bugs.md) — environment limits often mistaken for bugs
- [`docs/design.md`](../design.md) — domain rules and CI/homologación policy

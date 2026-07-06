# AGENTS.md

## Cursor Cloud specific instructions

`facturador` is a single-process Python 3.12 FastAPI app (JSON API + Jinja/HTMX
frontend) that issues Argentine export invoices ("Factura E") against ARCA's
WSAA/WSFEX SOAP web services. SQLite is the only datastore. There is one
service; the dev entrypoint is `uv run python -m facturador` (binds
`127.0.0.1:8399`, port override via `FACTURADOR_PORT`).

Tooling is `uv` (installed at `~/.local/bin`, on PATH via `.bashrc`). The update
script runs `uv sync`; dev deps live in the `dev` dependency-group. WeasyPrint's
system libs (pango/cairo/gdk-pixbuf) are already present in the base image.

Standard commands (see `pyproject.toml`):
- Lint: `uv run ruff check .`
- Types: `uv run mypy`
- Tests: `uv run pytest` (uses an in-process ARCA fake, no network needed)

### Running the app locally (non-obvious)

The app refuses to start unless a cert/key pair exists for the active
`ARCA_ENV`. On startup `load_config()` requires `secrets/<env>.crt` and
`secrets/<env>.key` (key mode must be 400/600) plus a `.env`. These are
gitignored and NOT in the repo. For a local `homo` run without real ARCA
credentials, generate a self-signed pair (subject must carry `CUIT <11 digits>`)
and a `.env` with at least `ARCA_ENV=homo`, `FACTURADOR_HOME=/workspace`,
`ARCA_CUIT=<11 digits>`. See `tests/conftest.py` for the exact self-signed cert
recipe.

- Client management (create/list/edit clients) works fully offline. The
  clients form validates country/currency codes against the `arca_params`
  cache, which is normally synced from ARCA. With no real credentials, seed it
  directly (see `facturador/repo/params.py::replace_params` and the
  `seed_params` helper in `tests/conftest.py`) so the `/clientes` page and
  `POST /clients` work.
- Anything that reaches ARCA (home page cotización, creating an invoice draft,
  `POST /invoices/{id}/authorize`, `GET /health/arca`) needs a **real
  certificate registered in ARCA homologación (WSASS) and authorized for the
  `wsfex` service**. ARCA's homologation endpoints ARE network-reachable from
  the VM, but a self-signed cert is rejected (`cms.cert.untrusted`), so those
  flows return 5xx until a real cert/key is supplied. `WsaaError` is not caught
  by the home handler, so `GET /` returns 500 when a default client exists and
  the cert is not ARCA-trusted — this is a credential limitation, not a bug.

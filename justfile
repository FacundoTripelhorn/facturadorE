# Shared recipes (check, fix, test, build): docs/agent/rules/ci.md.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# List the recipes
default:
    @just --list

# Lint, format check and type check
check:
    uv run --locked ruff check .
    uv run --locked ruff format --check .
    uv run --locked mypy

# Formatter and lint autofixes
fix:
    uv run --locked ruff format .
    uv run --locked ruff check --fix .

# Run the tests (the backup tests need age and age-keygen on the PATH)
test:
    uv run --locked pytest -q

# Build the Docker image, the runtime the app ships in
build:
    docker build -t facturador:local .

# Run the app: the launcher with its environment chooser
dev:
    uv run --locked python -m facturador.launcher

# Print tool versions, sync dependencies, then run check and test
doctor:
    uv run python --version
    uv --version
    uv sync --locked
    just check
    just test

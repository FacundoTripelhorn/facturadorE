#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Python: $(uv run python --version)"
echo "==> uv: $(uv --version)"
echo

echo "==> uv sync"
uv sync
echo

"$SCRIPT_DIR/lint.sh"
echo

"$SCRIPT_DIR/typecheck.sh"
echo

"$SCRIPT_DIR/test.sh"
echo

echo "==> doctor: all checks passed"

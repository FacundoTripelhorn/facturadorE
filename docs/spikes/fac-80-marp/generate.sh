#!/usr/bin/env bash
# FAC-80: reproducible Marp → PDF generation for the sprint-review spike.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${ROOT}/sprint-review.md"
OUT_DIR="${ROOT}/out"
PDF="${OUT_DIR}/facturador-backend-sprint-review.pdf"
# Pin CLI for reproducibility (Marp CLI 4.2.x supports custom themes + Chrome PDF).
MARP_CLI="${MARP_CLI:-@marp-team/marp-cli@4.2.3}"

mkdir -p "${OUT_DIR}"

if ! command -v npx >/dev/null 2>&1; then
  echo "error: npx is required (Node.js 18+)" >&2
  exit 1
fi

# Marp PDF conversion needs a Chromium-based browser.
export CHROME_PATH="${CHROME_PATH:-}"
if [[ -z "${CHROME_PATH}" ]]; then
  for candidate in google-chrome-stable google-chrome chromium chromium-browser chrome; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      CHROME_PATH="$(command -v "${candidate}")"
      export CHROME_PATH
      break
    fi
  done
fi

echo "Generating PDF with ${MARP_CLI}"
echo "  source: ${SRC}"
echo "  output: ${PDF}"
if [[ -n "${CHROME_PATH}" ]]; then
  echo "  chrome: ${CHROME_PATH}"
fi

npx --yes "${MARP_CLI}" \
  "${SRC}" \
  --theme-set "${ROOT}/theme" \
  --allow-local-files \
  --pdf \
  --pdf-outlines \
  -o "${PDF}"

echo "OK: ${PDF}"
ls -lh "${PDF}"

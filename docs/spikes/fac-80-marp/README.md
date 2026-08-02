# FAC-80 spike — Marp two-sprint backend review PDF

Repository-local proof of concept: generate an internal sprint-review PDF from
Marp Markdown + a minimal theme, using fixture cycle/issue data reconstructed
from merged GitHub history (Linear automation out of scope).

## Contents

| Path | Purpose |
|------|---------|
| `sprint-review.md` | Marp source (cover, completed tickets, two developer presentation sections) |
| `theme/facturador-review.css` | Minimal custom Marp theme |
| `data/cycles.json` | Fixture cycles / issues / assignees used to author the deck |
| `generate.sh` | One-command PDF generation |
| `out/facturador-backend-sprint-review.pdf` | Generated PDF |
| `FINDINGS.md` | Spike evaluation and recommendation |

## Generate the PDF

From the repository root (requires Node.js 18+ and Chrome/Chromium):

```bash
./docs/spikes/fac-80-marp/generate.sh
```

Equivalent direct invocation:

```bash
npx --yes @marp-team/marp-cli@4.2.3 \
  docs/spikes/fac-80-marp/sprint-review.md \
  --theme-set docs/spikes/fac-80-marp/theme \
  --allow-local-files \
  --pdf \
  --pdf-outlines \
  -o docs/spikes/fac-80-marp/out/facturador-backend-sprint-review.pdf
```

Optional: `CHROME_PATH=/path/to/chrome ./docs/spikes/fac-80-marp/generate.sh`

## Deck structure

1. Cover — team, two sprint names/dates, presenting developers
2. Completed issues — identifier + title, grouped by sprint
3. Facundo Tripelhorn — featured impact slides (`Problem` / `Backend changes` / `Impact`)
4. Cursor Agent — same structure

No demo steps are included; demos stay outside the deck.

## Fixture notes

Linear was not reachable from the cloud agent (MCP auth unavailable). Sprint
windows and ticket lists come from `master` merges between 2026-07-14 and
2026-07-29. Git history has one human backend author; Cursor Agent is used as
Developer 2 so the required two-presenter layout can be evaluated. See
`data/cycles.json`.

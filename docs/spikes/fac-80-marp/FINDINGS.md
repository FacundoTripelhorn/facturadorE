# FAC-80 findings — Marp sprint-review PDF spike

**Date:** 2026-08-02  
**Artifact:** [`out/facturador-backend-sprint-review.pdf`](out/facturador-backend-sprint-review.pdf)  
**Command:** `./docs/spikes/fac-80-marp/generate.sh`

## Recommendation

**Adopt Marp for internal FacturadorE backend sprint-review PDFs**, kept as a
repository-local Markdown convention (this spike), not as a service.

Do **not** build a standalone generator yet. A small **repo skill / agent prompt**
that assembles `sprint-review.md` from Linear cycle exports (or a checked-in
`cycles.json`) is a sensible next step once real assignees/cycles are available;
it is not required to get value from Marp today.

## What was produced

| Deliverable | Status |
|-------------|--------|
| Marp source (`sprint-review.md`) | Done |
| Minimal theme (`theme/facturador-review.css`) | Done |
| Fixture cycle data (`data/cycles.json`) | Done |
| Reproducible generate script | Done (`generate.sh`, pinned `@marp-team/marp-cli@4.2.3`) |
| Generated PDF (10 slides, 16:9) | Done (~186 KB) |
| Cover + completed tickets + two developer impact sections | Done |
| Featured slides with Problem / Backend changes / Impact | Done |
| No demo steps in the deck | Confirmed |

Fixture note: Linear MCP was unavailable in the cloud agent. Sprint windows and
ticket lists were reconstructed from `master` merges (2026-07-14…2026-07-29).
Git history has one human backend author; **Cursor Agent** is used as Developer 2
so the required two-presenter layout could be evaluated.

## Output quality

- Cover is clear (team, two sprints/dates, presenters).
- Completed-ticket slide stays readable with two-column lists per sprint;
  identifier + title only, as requested.
- Featured slides fit 16:9 without clipping when copy stays to ~3 short bullets
  under Backend changes.
- Theme is intentionally minimal (teal accent, light body, dark cover). Good
  enough for internal reviews; not a brand system.

**Readability / overflow review (rasterized pages):** no clipped titles, no
overflow past the footer on any of the 10 pages. Long titles wrap cleanly.
Footer page numbers are large (Marp default) but do not collide with body text.

## Ease of authoring and updating

| Aspect | Observation |
|--------|-------------|
| Edit loop | Change Markdown → re-run `generate.sh` (~5s with warm npx cache). |
| Structure | Front matter + `---` separators are easy for developers who already write docs. |
| Theme | One CSS file; `@import "default"` + a few variables. Low maintenance. |
| Ticket summary | Manual list today. Becomes tedious above ~25 issues unless generated from `cycles.json`. |
| Featured slides | Human judgment still needed for “most impactful” and the Problem/Impact prose. |

Authoring effort for this PoC (fixture gather + deck + theme + script + review)
was small (S-sized), matching the issue estimate.

## PDF rendering consistency

- Chrome/Chromium is required (`google-chrome-stable` used here).
- Pinning `@marp-team/marp-cli@4.2.3` makes the CLI version reproducible.
- Google Fonts (`IBM Plex`) are loaded at render time; offline runs fall back to
  system sans. For stricter reproducibility, vendor the font files later.
- Same Markdown + pinned CLI + same Chrome major version produced a stable PDF
  in this environment; expect minor glyph/metric drift across OS/Chrome versions.

## Limitations encountered

1. **No Linear automation** — expected for this spike; fixture data is honest but
   not assignee-authoritative.
2. **Browser dependency** — PDF path needs Chrome/Edge/Firefox; pure headless
   without a browser is not supported by Marp CLI.
3. **Dense summary slides** — two sprints fit; three busy sprints would need a
   second summary slide or generated pagination.
4. **Network font import** — see above.
5. **Not PowerPoint** — out of scope; Marp can emit PPTX if ever needed, but was
   not evaluated.
6. **npx deprecation noise** — `mathjax-full` warning from the CLI dependency
   tree; does not affect output.

## Should a Devin / repo skill be next?

**Yes, as a thin assembler — not as a product.**

A skill that:

1. Reads Linear cycles (or accepts `cycles.json`),
2. Filters completed issues for selected backend developers,
3. Emits/updates the ticket-summary section and stub featured slides,

…would remove the boring copy-paste while leaving Problem/Impact narrative to
humans (or a follow-up prompt). That is the right next increment if the team
wants this deck every sprint.

**Out of scope / reject for now:** scheduled generation, org-wide service,
production Linear sync in-app, and replacing spoken demos with slide steps.

## Acceptance checklist

- [x] PDF generated successfully from Marp source in the repository
- [x] Cover, complete ticket-summary slide, separate impact slides for two developers
- [x] Featured slides contain Problem / Backend changes / Impact
- [x] Demo steps not included
- [x] One documented command (`./docs/spikes/fac-80-marp/generate.sh`)
- [x] PDF reviewed for readability / overflow
- [x] Clear recommendation: **adopt Marp (repo-local); skill next; no service**

---
marp: true
theme: facturador-review
paginate: true
size: 16:9
footer: FacturadorE · Backend sprint review · FAC-80 spike
style: |
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;650&family=IBM+Plex+Mono:wght@400;500&display=swap');
---

<!-- _class: cover -->
<!-- _paginate: false -->
<!-- _footer: "" -->

# FacturadorE Backend
## Two-sprint review

<div class="meta">

**Sprint A — Onboarding & fiscal safety**  
14–18 Jul 2026

**Sprint B — Recovery, seed backup & polish**  
19–29 Jul 2026

**Presenting**  
Facundo Tripelhorn · Cursor Agent

</div>

---

<!-- _class: summary -->

## Completed issues — both sprints

### Sprint A · Onboarding & fiscal safety · 14–18 Jul

- `FAC-35` Per-profile setup state and onboarding guard
- `FAC-40` Production confirmation and safety cues
- `FAC-37` Certificate upload onboarding step
- `FAC-48` Block issuance when local profile is behind ARCA
- `FAC-38` Emisor and point-of-sale onboarding
- `FAC-10` Snapshot issuer fields on invoices at draft creation
- `FAC-52` Persist complete immutable invoice rendering snapshot
- `FAC-53` Version PDF renderers and make generated PDFs disposable

### Sprint B · Recovery, seed backup & polish · 19–29 Jul

- `FAC-44` Encrypted profile seed backup and manifest
- `FAC-45` S3 storage adapter for encrypted profile seeds
- `FAC-47` Trigger encrypted seed backup on profile config changes
- `FAC-65` Rebuild register from ARCA (reconstruct-based restore)
- `FAC-66` Portable arca-kb ARCA knowledge skill
- `FAC-68` GET /invoices/arca read-only register peek
- `FAC-70` Redirect incomplete-setup HTML routes to /setup
- `FAC-71` Sort País destino and CUIT país dropdowns by description
- `FAC-72` Actionable 503 when PDF render lacks WeasyPrint/GTK
- `FAC-73` Default Ingresos Brutos to Exento on emisor forms
- `FAC-74` Fix UTF-8 mojibake in emisor templates from FAC-73

---

<!-- _class: divider -->

# Facundo Tripelhorn
Platform · identity · recovery

Selected impact tickets from Sprints A & B

---

<!-- _class: feature -->

## FAC-35 · Setup state & onboarding guard

### Problem
A half-configured profile could still reach invoice/ARCA routes, producing confusing failures instead of a guided setup path.

### Backend changes
- Added per-profile setup state (`ready` / incomplete) with a request guard.
- Incomplete HTML routes redirect to `/setup`; JSON APIs return **503** with `setup_state`.
- `GET /health` and `GET /setup` remain available for diagnostics.

### Impact
Operators cannot emit until the profile is ready. Failures become setup guidance rather than opaque ARCA/config errors.

---

<!-- _class: feature -->

## FAC-44 + FAC-45 · Encrypted seed + S3

### Problem
Profile config (emisor, PV, backup settings) lived only on disk. Rebuilding a workstation lacked a portable, encrypted config artifact.

### Backend changes
- `seed.age` backup: config + manifest, age-encrypted to `recipients.txt`.
- Seed excludes DB/PDFs — ARCA remains authoritative for the register.
- S3 adapter uploads/downloads the seed without changing crypto or seed shape.

### Impact
Config can travel safely between machines. Restore is a defined seed + (later) ARCA rebuild path, not an ad-hoc file copy.

---

<!-- _class: feature -->

## FAC-65 · Rebuild register from ARCA

### Problem
After seed restore, the local invoice register could be empty or stale while ARCA still held the authoritative series.

### Backend changes
- Full rebuild via launcher `--restore`: identity gate → apply seed → wipe → batched `FEXGetCMP` `1..N`.
- Catch-up API/UI for FAC-48 remediation (`N_local+1..N_arca`).
- Gaps (`ErrCode 1521`) recorded; transients abort after retries — never invent numbers.

### Impact
A lost local DB can be reconstructed from ARCA with hard identity checks. Numbering stays ARCA-authoritative end to end.

---

<!-- _class: divider -->

# Cursor Agent
Invoice fidelity · backup hooks · ARCA knowledge

Selected impact tickets from Sprints A & B

---

<!-- _class: feature -->

## FAC-10 / 52 / 53 · Immutable PDF contract

### Problem
Authorized PDFs could drift if live emisor/client/params changed after draft creation. Cached PDFs were also treated like durable fiscal artifacts.

### Backend changes
- Snapshot issuer/display fields and `pdf_render_version` at draft creation.
- Renderers take only invoice-owned data — no live lookups.
- Versioned renderer registry; disposable local PDF cache regenerates from the snapshot.

### Impact
Reprinting an authorized invoice stays faithful to what was authorized. Layout evolution is versioned; cache loss does not lose fiscal records.

---

<!-- _class: feature -->

## FAC-47 · Seed backup on config change

### Problem
FAC-44/45 could upload a seed, but nothing automatically produced a fresh remote copy when profile config changed.

### Backend changes
- Event hooks on onboarding→ready, emisor edits, backup settings, `recipients.txt` replace.
- Debounced coalesce so one session of edits yields one upload.
- Persist retry state; retry on launch and via `POST /backup/seed`. Emission never triggers backup.

### Impact
Remote seed stays aligned with config without operator ritual. Failures are isolated from config saves and are retryable.

---

<!-- _class: feature -->

## FAC-66 · Portable `arca-kb` skill

### Problem
Agents answered WSAA/WSFEX questions from stale training memory, inventing endpoints, ErrCodes, and TA reuse rules.

### Backend changes
- Added vendor-neutral `skills/arca-kb/` with citation-backed manuals and observed homologación behavior.
- Documented methods used by FacturadorE, TA reuse, gap vs error, redacted capture shapes.
- Linked from `AGENTS.md` / repo-map so agents load it on ARCA protocol questions.

### Impact
ARCA protocol answers become citation-backed and portable. Product architecture stays in `docs/design.md`; protocol trivia stops polluting agent guesses.

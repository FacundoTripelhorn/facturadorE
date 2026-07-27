# `arca-kb` validation (FAC-66)

Portable checks — no vendor-specific build UI required.

## 1. Structure (`skills-ref`)

Agent Skills reference validator ([agentskills.io](https://agentskills.io/specification)):

```bash
uvx --from skills-ref agentskills validate skills/arca-kb
uvx --from skills-ref agentskills read-properties skills/arca-kb
uvx --from skills-ref agentskills to-prompt skills/arca-kb
```

Expect: no validation problems; `name=arca-kb`; description lists ARCA/WSFEX/WSAA triggers.
(PyPI package `skills-ref` exposes the `agentskills` CLI.)

## 2. Triggering evals (description-only)

Judge whether metadata alone should load the skill:

| Prompt | Expect |
|--------|--------|
| “Does `Fecha_pago` round-trip on `FEXGetCMP`?” | **Trigger** |
| “ErrCode 1521 on FEXGetCMP — gap or failure?” | **Trigger** |
| “WSAA says CEE ya posee un TA válido” | **Trigger** |
| “Homologación WSFEX endpoint URL?” | **Trigger** |
| “Rename the HTMX partial for clients list” | **No trigger** |
| “Fix ruff import order in `settings.py`” | **No trigger** |

## 3. Technical answer eval (against sources, not training memory)

**Prompt:** How does WSFEX signal “comprobante does not exist” vs a real error during rebuild?

**Must match skill + captures:**

- Business `FEXErr` with **ErrCode 1521** and message about no data for the Cmp
- Other `ErrCode ≠ 0` / transport / SOAP Fault → not a gap
- Cite Observed (FAC-65) and point at `captures/fexgetcmp-not-found-1521.xml`

**Prompt:** Does `Fecha_pago` come back from `FEXGetCMP`?

**Must match:**

- Yes — Official manual v3.1.1 / WSDL `ClsFEXGetCMPR` + Observed FAC-63
- May differ from `Fecha_cbte`
- See `captures/fexgetcmp-success-redacted.xml`

## 4. Portability (two environments)

Equivalent validation without maintaining forked skills:

| Environment | What was run |
|-------------|--------------|
| **A — `agentskills` CLI (skills-ref)** | `validate` + `read-properties` + `to-prompt` on `skills/arca-kb` (Agent Skills spec; vendor-neutral) |
| **B — Cursor Cloud agent (this FAC-66 run)** | Same tree under `skills/arca-kb`; §3 prompts answered from skill + captures and matched to FAC-65 / FAC-63 sources |

Both consume the **same** directory. Do not create Claude-only or Cursor-only variants.

## 5. Secrets scan

```bash
rg -n -i 'BEGIN (RSA |EC )?PRIVATE KEY|BEGIN CERTIFICATE|loginCmsReturn|[A-Za-z0-9+/]{80,}={0,2}' skills/arca-kb || true
rg -n -i 'Token>|Sign>|CMS' skills/arca-kb/captures
```

Expect: only `REDACTED_*` placeholders or structural tag names in comments/docs — no real Token/Sign/CMS/certs. Fake CUIT `20000000001` only.

## Record

Fill when validating a revision:

| Check | Result | Date |
|-------|--------|------|
| agentskills validate | **Passed** (`Valid skill: skills/arca-kb`) | 2026-07-27 |
| Trigger table (§2) | **Passed** (description keywords cover ARCA/WSFEX/WSAA/1521/Fecha_pago; excludes generic UI/lint prompts) | 2026-07-27 |
| Technical Q §3 (1521) | **Passed** — answer: FEXErr 1521 = gap; other codes/transport = abort (matches `captures/fexgetcmp-not-found-1521.xml` + `docs/wsfex-gap-vs-error.md`) | 2026-07-27 |
| Technical Q §3 (Fecha_pago) | **Passed** — round-trips per manual v3.1.1 + FAC-63; see `captures/fexgetcmp-success-redacted.xml` | 2026-07-27 |
| Portability A+B | **Passed** — CLI env A + Cursor Cloud env B, single tree | 2026-07-27 |
| Secrets scan | **Passed** — only `REDACTED_TOKEN`/`REDACTED_SIGN`; fake CUIT `20000000001`; no PEM | 2026-07-27 |

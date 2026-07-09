# Task brief template (agents)

Reusable scaffold for **larger** coding-agent tasks — Linear issues, PR
descriptions, or handoff prompts. Copy the template below into the issue or
prompt and fill in the placeholders.

**When to use:** multi-file changes, ARCA/authorization work, schema or backup
changes, or anything where blind repo search would waste tokens. **When to skip:**
one-line fixes, typo edits, or tasks already scoped to a single file and test.

Shared project context lives in [`AGENTS.md`](../../AGENTS.md) and the other
[`docs/agent/`](.) supplements — **link to those docs; do not paste them here.**

---

## Template

```markdown
## Goal

<!-- One or two sentences: what should be true when the task is done? -->

## Non-goals

<!-- What is explicitly out of scope? Prevents scope creep. -->

- 

## Relevant files

<!-- Pointers, not a full tree. Use repo-map links when unsure. -->

- `path/to/file.py` — <!-- why it matters -->
- See also: [`docs/agent/repo-map.md`](docs/agent/repo-map.md) § <!-- section -->

## Constraints

<!-- Task-specific rules. Link global constraints instead of restating them. -->

- Follow [`AGENTS.md`](AGENTS.md) architectural and security rules.
- <!-- e.g. preserve authorize idempotency; no new runtime dependencies -->

## Expected verification

<!-- Which checks must pass? Link the matrix for defaults. -->

- Default order: [`docs/agent/verification-matrix.md`](docs/agent/verification-matrix.md)
- <!-- Narrow scope, e.g. `uv run pytest tests/test_api.py` -->
- <!-- Manual checks, e.g. authorize flow in homologación -->

## Risk areas

<!-- What could break or confuse an agent? -->

- <!-- e.g. ARCA numbering reconciliation, SQLite migration ordering -->
- See [`docs/agent/known-non-bugs.md`](docs/agent/known-non-bugs.md) if symptoms look like env limits.

## Expected final summary

<!-- Remind the agent how to close the task. -->

End with the four sections from [`AGENTS.md`](AGENTS.md): **Summary**,
**Changed files**, **Verification**, **Remaining risks**.
```

---

## Example (filled in)

```markdown
## Goal

Add a `GET /health/arca` endpoint that returns WSAA/WSFEX reachability without
exposing secrets.

## Non-goals

- No changes to invoice authorization or numbering logic.
- No new dependencies.

## Relevant files

- `facturador/api/app.py` — route registration
- `facturador/arca/wsaa.py`, `facturador/arca/wsfex.py` — SOAP clients
- `tests/test_api.py` — API coverage
- See also: [`docs/agent/repo-map.md`](docs/agent/repo-map.md) § ARCA clients

## Constraints

- Follow [`AGENTS.md`](AGENTS.md) architectural and security rules.
- Redact TA token/sign in logs and responses.
- Tests must use `tests/arca_fake.py`; no real ARCA calls in pytest.

## Expected verification

- `uv run ruff check .` → `uv run mypy` → `uv run pytest tests/test_api.py`
- Optional manual: `scripts/check_wsfex.py` against homologación (local only).

## Risk areas

- Health check must not block the event loop on long SOAP timeouts.
- Missing certs should return a clear status, not a 500 on unrelated routes.

## Expected final summary

End with **Summary**, **Changed files**, **Verification**, **Remaining risks**
per [`AGENTS.md`](AGENTS.md).
```

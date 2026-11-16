# Documentation

<!-- agent-standards v0.8 — synced from agent-standards. Edit it there, not here. -->

Read before creating, moving or editing any document.

## Which documents exist

Documentation that drifts from the code, or that says the same thing in two
places with two different answers, is worse than no documentation. The rule
is: few documents, each with one job, and every fact stated in exactly one of
them.

**Every repository has exactly these documents:**

| File | Its one job |
|---|---|
| `README.md` | What the project is and how to run it. Short; links to the rest. |
| `AGENTS.md` | Base rules (synced block) plus this project's own rules. |
| `docs/agent/repo-map.md` | Where things live: task type → files and directories. |
| `docs/agent/verification-matrix.md` | Which checks to run for which kind of change, including manual steps only the owner can do. |
| `docs/agent/known-non-bugs.md` | Behaviour that looks wrong but is intended, so nobody "fixes" it. |
| `docs/agent/rules/` | Shared rules, synced from agent-standards. Never edited in the repository. |
| `docs/design.md` | How the system works now, and why (optional). Expands the Architecture section of `AGENTS.md` without repeating its module order or invariants. Headings: Context, Components, Data flow, External systems, Trade-offs that still hold. |

- **Project-specific documents** that a project genuinely needs (a game's
  design docs, an operator setup guide, license files that must sit next to
  third-party assets) are listed in the repository's `AGENTS.md` under
  "Other documents", each with its one job. The owner approves additions to
  that list.
- **Nothing else.** A document in neither table should not exist. No status
  reports, plans, backlogs, investigations or per-milestone validation logs as
  files: those belong in issues and PRs, which are dated and close.
- **No decision records (ADRs).** A record describes a past decision and is
  never updated, so it soon contradicts the code while still reading like a
  rule. Write the reason that still holds next to what it explains: in
  `docs/design.md`, in `known-non-bugs.md`, or in a code comment. The history
  of how it was decided stays in the issue and the PR.
- **One fact, one place.** Before writing something, search the docs for it.
  If it exists, link to it or edit it there; never restate it.
- **Docs follow the code.** If behaviour, setup or an interface changes,
  update the one doc that owns that fact in the same PR. When a doc and the
  code disagree, the code is right: fix the doc.
- **No issue or PR references** to this project's tracker (`code.md`,
  Issue references).
- **No dates or versions as current truth** in prose ("as of v0.3 we…").
  State what is true now; history lives in git and the issues.
- **Keep them short.** Prefer a table or a list over paragraphs. Delete what
  is no longer true instead of adding a correction below it.

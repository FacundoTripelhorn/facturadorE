# facturador

Issues Argentine export invoices ("Factura E") through ARCA's WSAA and WSFEX
SOAP web services. A local app for one user: Python 3.12, one FastAPI process
(JSON API plus a Jinja/HTMX UI), SQLite, PDFs with fpdf2. It ships as a
Windows installer, with Docker as the alternative runtime.

## Commands

`just check`, `just fix`, `just test` and `just build` (the Docker image) are
described in the shared rules below. The tests need `age` and `age-keygen` on
the PATH. This project also has:

| Do this | Command |
|---|---|
| Run the app: launcher with the environment chooser (binds `127.0.0.1:8399`) | `just dev` |
| Backend only, for one environment | `ARCA_ENV=homo uv run python -m facturador` |
| Tool versions, sync, then check and test | `just doctor` |
| Contract tests against real homologación (local only, needs a real certificate) | `uv run python scripts/get_ta.py`, `uv run python scripts/check_wsfex.py` |

## Architecture

- **Modules:** `web` (UI) and `api` (JSON) → `service` → `arca` (WSAA/WSFEX
  clients), `repo` (SQLite), `pdf`. `launcher` supervises the backend process;
  `profile` resolves the per-environment data paths. Detail: `docs/design.md`.
- **External systems:** ARCA WSAA and WSFEX (only through `facturador/arca/`),
  S3 for the encrypted seed backup (`s3_seed.py`), the `age` binary (backups).
- **Invariants.** Do not regress these without a design change in
  `docs/design.md`:
  - Localhost only: bind `127.0.0.1`; Docker publishes only `127.0.0.1:PORT`.
    No proxies, tunnels or auth layers that expose the service.
  - Certificates and keys never enter the repo, CI or logs; key files stay mode
    400/600. TA token/sign and CMS payloads are redacted in logs.
  - One backend process = one immutable environment and one profile, with its
    own SQLite, certificates, caches, logs and backups. No hot switching.
  - ARCA is authoritative for numbering: always reconcile with
    `FEXGetLast_CMP`, never a local counter alone.
  - Single process, no workers or queues (~1 invoice a week).

## Versioning and release

- The version lives in `pyproject.toml`.
- A release is the tag `v<version>` on `master`. The Windows workflow then
  publishes `FacturadorE-Setup-<tag>.exe` in a GitHub Release.

## Project rules

- End every task with four sections: **Summary**, **Changed files**,
  **Verification** (commands and results; say what was not run and why) and
  **Remaining risks**. Keep them proportional to the task.
- `tests/test_no_issue_refs.py` enforces the shared issue-references rule for
  this repository's tracker: `FAC-<n>`, Linear links, and its GitHub issues and
  PRs.

## Other documents

| File | Its one job |
|---|---|
| `docs/setup-homologacion.md` | Operator setup for homologación. |
| `docs/setup-produccion.md` | Operator setup for production. |
| `docs/windows-smoke-test.md` | The manual Windows checks CI cannot do. |
| `docs/wsfex-gap-vs-error.md` | How the rebuild tells a missing voucher from an error. |
| `docs/agent/architecture-graphs.md` | Mermaid diagrams for orientation; `docs/design.md` is authoritative. |
| `skills/README.md`, `skills/arca-kb/` | Portable ARCA protocol knowledge, with sources. |
| `SECURITY.md` | How to report a vulnerability. |
| `.cursor/rules/` | Pointers for Cursor to this file and `docs/agent/`. |

<!-- agent-standards:base v0.8 — synced from agent-standards. Edit it there, not here. -->
## Shared rules

These rules apply to all of the owner's repositories. The project sections
above this block come first: if one of them disagrees with a shared rule,
follow the project section and point out the conflict in your PR.

`project.yml` gives the project key (`FAC`, `GHOST`…), the language for
PRs, issues and docs, the issue tracker, the package manager and where the
version lives.

### Commands

Every repository runs its checks with [just](https://just.systems), under
the same names:

| Command | What it runs |
|---|---|
| `just check` | Lint, format check, type check |
| `just fix` | Formatter and lint autofixes |
| `just test` | The tests |
| `just build` | The build |

`just --list` shows the project's other recipes. When a recipe cannot run
here (for example, there is no Unity editor), it stops with a message that
says so. Report that check as "not run", never as passed.

### Before you start

- Work on one issue only. If you find something else that needs fixing,
  mention it in the PR instead of fixing it.
- Read `docs/agent/repo-map.md` to find where things live,
  `docs/agent/verification-matrix.md` to see what to run, and
  `docs/agent/known-non-bugs.md` to see what is intended.
- Before some kinds of work, also read the rule file for it:

  | Before you… | Read |
  |---|---|
  | write or change code | `docs/agent/rules/code.md` |
  | handle input, files, secrets or saved data | `docs/agent/rules/security.md` |
  | create a branch, commit, open a PR or release | `docs/agent/rules/workflow.md` |
  | add, remove or upgrade a dependency | `docs/agent/rules/dependencies.md` |
  | change a workflow or a `justfile` | `docs/agent/rules/ci.md` |
  | add, move or edit a document | `docs/agent/rules/docs.md` |

### Code

- Files and functions stay under the limits in
  `docs/agent/rules/thresholds.md`. If a file is already over its limit,
  move code out of it before you add anything.
- Use a named constant or enum for every value the code compares, branches
  on, stores or sends, even if it appears only once: `Status.PAID`, not
  `"paid"`. Each value gets one name: copying it into a second constant
  under another name breaks this rule. Reviewers check this; CI does not.
- Type all new code. Do not add `Any`, `@ts-nocheck`, `@ts-ignore` or
  `#pragma warning disable`, unless it is deliberate: then add
  `allow-suppression: <why>` in a comment on that line.
- Handle every error or let it propagate. Never catch an error and do
  nothing with it.
- Never reference this project's issues or PRs in code, comments, tests or
  docs: no IDs, tracker links or "PR #<n>". The commit history already links
  each change to its issue.
- Each piece of logic lives in one place. If two languages share a data
  format, generate one side from the other, or add a test that checks
  they still match.
- Respect the module boundaries in this file's Architecture section.
- Every change comes with tests. A bug fix includes a test that fails
  without the fix.

### Input and user data

- Treat all outside input as untrusted. That includes requests, files, env
  vars, CLI arguments, API and LLM responses, and rows that users wrote.
  Validate it with a schema where it enters.
- Escape or encode input for where it is used: SQL parameters, argument
  lists for subprocesses, template escaping. Never put input in a message
  you show back or in a redirect target.
- If saved user data cannot be read, keep the file and report the error.
  Never delete or overwrite it.

### Branches, commits and PRs

- Branch name is the issue ID: `KEY-12` (`KEY` is the project key). Use
  `claude/KEY-12` only when the platform requires a prefix.
- PR title is the ID plus the issue title: `KEY-12 Export invoices as CSV`.
  It becomes the single squash commit.
- In the PR, list every check you ran with its real result. Write "not run"
  and the reason for any check you skipped.
- When code changes a fact that a doc states, update that doc in the same
  PR.

### Ask the owner first

- Adding, removing or upgrading a dependency.
- Changing CI, the `justfile`, linter or formatter settings, or repository
  settings.
- Creating a new document outside the list in `docs/agent/rules/docs.md`.

### Never

- Commit secrets, tokens or real personal data, including in test fixtures.
  Never write them to logs either.
- Push to the default branch, rewrite history on a branch you did not
  create, or merge a PR.
- Change the version unless the issue is a release. Never create tags: the
  owner tags releases.
- Skip, disable or delete a test to make CI pass.
- Edit this block or `docs/agent/rules/` in this repository. Change them in
  agent-standards.
<!-- /agent-standards:base -->

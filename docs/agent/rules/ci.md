# CI and the justfile

<!-- agent-standards v0.8 — synced from agent-standards. Edit it there, not here. -->

Read before changing a workflow under `.github/workflows/` or the `justfile`.
Both changes need the owner's approval first (shared rules, "Ask the owner
first").

## Same commands locally and in CI

- The `justfile` is the one place that says how to lint, format, type-check,
  test and build. CI calls its recipes; it never repeats their commands.
  What passes on your machine is what CI runs.
- Every repository has the recipes `check`, `fix`, `test` and `build`. A
  recipe that does not apply to a project prints that and exits 0 (for
  example, a library with nothing to build). A recipe that applies but
  cannot run here exits non-zero with the reason.
- Formatters run in check mode in `just check`. Do not commit unformatted
  code.
- Unity is the one exception to "CI calls the recipe": the editor exists in
  CI only as a Docker image, so the `test` job runs the same Edit Mode tests
  through GameCI instead of `just test`.
- If the repository has an architecture check (import-linter,
  dependency-cruiser, assembly references), `just check` runs it. It is
  optional, so new repositories are not blocked by it.

## Same job names everywhere

Every repository's CI has these jobs, with these names, so every ruleset
requires the same checks:

| Job | Runs |
|---|---|
| `check` | Toolchain setup, then `just check` |
| `test` | Toolchain setup, then `just test` |
| `build` | Toolchain setup, then `just build` |

The shared checks from agent-standards run in a `standards` job that uses
the agent-standards action, pinned by SHA, after a checkout with
`fetch-depth: 0`. It checks `project.yml`, the PR title, file and function
size, suppressions, pinned actions, release tags and secrets (gitleaks).
Coding practices that need judgement, such as names over literals, are not
checked by CI; they are reviewed. Findings appear on the lines
of the PR diff, each with the fix and the rule. It starts in `mode: warn`;
the owner switches a repository to `mode: fail` once its warnings are
understood. Secrets fail in either mode. Jobs that
only make sense for one project (a Windows build, a Docker image) are extra
jobs, listed in `docs/agent/verification-matrix.md`.

## Workflow hygiene

- `permissions: contents: read` at the top. Grant more only to the job that
  needs it, with a comment saying why.
- Every job has `timeout-minutes`.
- Pull request runs cancel older runs of the same PR (`concurrency` with
  `cancel-in-progress` on pull requests only).
- Actions are pinned to a commit SHA (`dependencies.md`, GitHub Actions).
- Secrets are read only in the step that needs them, through `env:`.
- Triggers: pull requests and pushes to the default branch. Use
  `workflow_dispatch` only for manual runs; no `schedule:` unless the issue
  asks for one.

## CI is the source of truth

- Every check you are told to run locally also runs in CI.
- A check that could not run must not look like a pass. A skipped test job
  fails or shows a visible warning, and a required check proves the tests
  ran.

# Code rules

<!-- agent-standards v0.8 — synced from agent-standards. Edit it there, not here. -->

Read before writing or changing code.

## Size and complexity

Large files and functions were the most repeated problem across these
repositories, and they grow one PR at a time. Keep new code within the limits
in `thresholds.md` (same folder). They are the defaults of each language's standard
linter. In short:

| Language | File | Function |
|---|---|---|
| Python | 1000 lines | 50 statements, cyclomatic complexity 10 |
| TypeScript / JavaScript | 300 lines | 50 lines, cyclomatic complexity 20 |
| C# | 1000 lines of code | 80 lines of code, cognitive complexity 15 |

- CI uses a ratchet: existing files over a limit may not grow, and new files
  must fit. You never have to fix old debt to land a change, but you may not
  add to it.
- If a change would push a file or function over its limit, split it first,
  in the same PR or a preceding one. Appending to the biggest file is not
  the default.
- A real exception (generated code, a data table, a Unity scene builder) goes
  in `size-exceptions.txt` with a one-line reason, so it gets reviewed.

## Names over literals

A value with meaning to the program is defined once, by name, and used by
that name everywhere. How many times it appears today does not matter.

- **What counts:** any value that is compared against, switched on, routed
  on, persisted, logged as a key, or sent across a boundary: URL paths, query
  keys and their values, status and state names, tab or mode keys, event
  names, HTTP status codes, file names, environment variable names, schema
  versions, error codes.
- **Such a value is never a bare literal**, even if it appears only once.
  `if status == "ok"` is not allowed; `if status is Status.OK` is. The single
  place a literal may appear is the line that defines the constant or enum
  member.
- **One value, one name.** Two constants with the same value are only
  allowed when they are genuinely different concepts that happen to coincide
  (and then say so in a comment). Renaming, splitting or re-declaring a value
  so it looks different is the same violation.
- **Related values live together** in one enum or constants module per
  domain, not scattered as module-level strings.
- Use the language's own facility: `StrEnum` or `HTTPStatus` in Python,
  `as const` objects or union types in TypeScript, `enum` or `const` in C#.
- **Not covered:** user-facing copy (it belongs to the i18n or copy layer of
  the project), log message text, test data, docstrings, and language idioms:
  `__name__ == "__main__"`, `typeof x === "string"`, `0`, `1`, the empty
  string, and platform checks (`sys.platform`, `os.name`,
  `process.platform`), which type checkers only narrow when written with the
  literal.
- This is a practice for whoever writes the code, checked in review. CI does
  not enforce it.

## Types

- New code is fully typed. No new `Any`, `@ts-nocheck`, `@ts-ignore` or
  `#pragma warning disable` unless it is deliberate and says why (below).
- Data that crosses a layer (database row to template, API response to
  domain) gets a typed model rather than a raw dict or row.
- The number of existing suppressions in a file may only go down. CI counts
  `Any`, `# type: ignore`, `# noqa`, `# pylint: disable`, `any`,
  `@ts-ignore`, `@ts-nocheck`, `@ts-expect-error`, `eslint-disable`,
  `#pragma warning disable` and `[SuppressMessage]`.
- A deliberate suppression carries a reason, in a comment on its line or on
  the line above: `allow-suppression: <why>`. For example
  `def load(raw: str) -> Any:  # allow-suppression: plugin payloads are untyped`.
  CI does not count it; the reviewer judges the reason. The marker without a
  reason exempts nothing.

## Errors

- Never swallow an error silently. Every `except`, `catch` or `.catch()`
  either handles the error, logs it with context, or re-raises. If swallowing
  is intended (best-effort cleanup), say so in a comment.
- Catch the narrowest exception type that fits. A broad catch needs a
  comment.
- Log enough to diagnose (operation, ids, file path), and never secrets,
  tokens, private keys or personal data.

## One source of truth

- Do not copy logic between modules. Shared mapping, validation or scoring
  lives in one place that both callers use.
- A contract defined in more than one language (schemas, enums, API types)
  is either generated from one source or covered by a parity test that runs
  in CI whenever any of the copies changes.

## Issue references

- Code, comments, tests, templates and docs never reference this project's
  own issues or PRs: no issue IDs (`KEY-12`), no links to the tracker or to
  this repository's issues and PRs, no "PR #<n>" or "issue #<n>".
- Why: they go stale, cause merge conflicts, and teach agents to preserve
  history instead of describing what the code does now. The PR description
  and the commit link each change to its issue; `git log` and `git blame`
  find them.
- Say what the code does and why. "Retry once: the service drops the first
  call after idle" belongs in a comment; "fix for KEY-12" does not.
- Citing another project's issue as an external source (an upstream bug, a
  library's changelog) is fine.
- Examples of the ID format use the made-up key `KEY`, never a real one.

## Tests

- Every behaviour change comes with tests. Every bug fix comes with a test
  that fails without the fix.
- Tests use no network and no real credentials. Fixtures hold no secrets and
  no real personal identifiers.
- A refactor that claims "no behaviour change" says how that was verified
  (same tests, same routes, same messages, and so on).

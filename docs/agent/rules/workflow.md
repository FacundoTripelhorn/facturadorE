# Workflow

<!-- agent-standards v0.8 — synced from agent-standards. Edit it there, not here. -->

Read before creating a branch, committing, opening a PR, writing in an issue
or making a release.

## Issues, branches and commits

- Work comes from an issue in the tracker set in `project.yml`, written with
  the issue template: Goal, Invariants, In scope, Out of scope, Done when,
  Verification.
- Each project numbers its own issues. The issue ID is `<key>-<number>`:
  - `tracker: linear`: the Linear issue ID as Linear shows it (`KEY-12`);
    the Linear team key is the project key.
  - `tracker: github`: the project key plus the GitHub issue number of that
    repository (`KEY-12` is that repository's GitHub issue number 12).
  The examples in these rules use the made-up key `KEY`.
- **Branch name:** exactly the issue ID, nothing else: `KEY-12`. If a
  platform requires its own prefix, keep the prefix and put only the ID
  after it: `claude/KEY-12`, `codex/KEY-12`, `cursor/KEY-12`.
- **PR title:** `<ID> <issue title>`, for example
  `KEY-12 Export invoices as CSV`. It becomes the commit on the default branch.
- **Commits:** one line, `<ID> <issue title>`, no body. PRs are merged with
  squash, so the default branch gets exactly one one-line commit per issue.
- **Closing the issue:** the PR body says `Closes #<number>` for GitHub
  issues, or `Closes KEY-12` for Linear issues (Linear also links the PR by the ID in
  the branch name).
- The default branch is whatever the repository uses (`main` or `master`).
  Scripts and workflows read it from the repository; never hard-code it.

## Pull requests

- One issue per PR, small enough to review in one sitting. Open it as a
  draft.
- Fill in the PR template. Report the checks you ran with their real results.
  Write "not run" and the reason for anything you did not run; never claim a
  pass you did not see.
- A project with `status: paused` in `project.yml` gets no new work unless
  the owner asks for it explicitly.

## Language

- Issues, PR titles and bodies, and commit messages are written in `language`
  from `project.yml`. Docs and UI copy use `docs_language` if it is set, and
  `language` otherwise.
- Code identifiers, code comments and agent instructions are in English,
  except domain terms that have no good English equivalent (for example
  `factura`, `emisor`, `CUIT`), which keep their original name.

## Versions and releases

- Versions follow SemVer (`MAJOR.MINOR.PATCH`). While the major version is
  0, a breaking change bumps the minor version.
- The version is written in one place: the file named by `version_file` in
  `project.yml`. Nothing else states it; code that needs it reads it from
  there or from the installed package metadata.
- A release is a PR whose issue asks for it: it changes only the version,
  and after it merges the owner tags that commit `v<version>`. CI checks that
  the tag matches `version_file`.
- There is no changelog file. Release notes are the one-line commits between
  two tags (`git log --oneline v0.2.0..v0.3.0`), which are the issue titles.

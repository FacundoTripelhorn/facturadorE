# Dependencies

<!-- agent-standards v0.8 — synced from agent-standards. Edit it there, not here. -->

Read before adding, removing or upgrading a dependency. Adding or removing
one needs the owner's approval first (shared rules, "Ask the owner first").

## One package manager, one lockfile

- Each repository uses only the package manager in `project.yml`
  (`package_manager`). Do not mix them: no `package-lock.json` in a pnpm
  repository, no `requirements.txt` next to `uv.lock`.
- The lockfile is committed and is the only source of installed versions.
  Install from it without changing it:

  | Package manager | Install |
  |---|---|
  | uv | `uv sync --locked` |
  | pnpm | `pnpm install --frozen-lockfile` |
  | npm | `npm ci` |
  | unity | `Packages/packages-lock.json`, resolved by the editor |

- Never resolve a lockfile merge conflict by hand: take either side, then
  regenerate it (`uv lock`, `pnpm install`, `npm install`).
- Lockfiles are marked generated in `.gitattributes`
  (`templates/.gitattributes`), so GitHub collapses them in PR diffs.
- A PR that changes the lockfile says why. A lockfile change with no
  matching manifest change is a mistake unless the PR is an upgrade.

## Before proposing a new dependency

In the PR or the issue, answer these:

1. Can the standard library, the framework already in use or code already in
   this repository do it? If so, use that.
2. Is the license compatible with the project's license? Copyleft licenses
   (GPL, AGPL) need the owner's explicit approval.
3. Is it maintained: a release in the last year and issues being answered?
4. What does it pull in? Prefer a small package over one with a large
   dependency tree for a small job.

Development-only tools go in the dev group (`[dependency-groups] dev`,
`devDependencies`), never in runtime dependencies.

## Upgrades

- Dependabot opens upgrade PRs weekly, with minor and patch updates grouped
  (`templates/.github/dependabot.yml`). It covers the package manager, GitHub
  Actions and, where present, Docker.
- A major upgrade is its own PR, with the changelog's breaking changes
  checked against this code.
- Remove a dependency in the same PR that removes its last use.

## GitHub Actions

- Every `uses:` is pinned to a full commit SHA, with the version in a
  comment: `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1`.
  A tag such as `@v4` can be moved to different code by whoever controls the
  action; a SHA cannot. Dependabot updates both the SHA and the comment.

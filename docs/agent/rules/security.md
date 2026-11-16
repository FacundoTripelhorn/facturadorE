# Security and user data

<!-- agent-standards v0.8 — synced from agent-standards. Edit it there, not here. -->

Read before handling input from outside the process, files, persistence or secrets.

## Input and output

**Every input from outside the process is untrusted**: HTTP requests (path,
query, form, headers, cookies, uploads), files read from disk or the user,
environment variables, command-line arguments, responses from external APIs,
data from the database that a user wrote, and LLM output.

- **Validate at the boundary**, once, with a schema (pydantic, zod, a typed
  C# model): type, length limits, allowed characters or an allowlist of
  values, and checksums or formats where the format has them (keys, ids,
  amounts). Reject what does not fit; do not try to repair it. Code past the
  boundary works with the validated type only.
- **Normalize before you check**: trim and Unicode-normalize text, resolve
  paths (`resolve()` / `realpath`) and confirm they stay inside the allowed
  directory.
- **Encode for where the value goes**, at the moment it goes there:
  parameterized SQL (never string-built queries), argument lists for
  subprocesses (never a shell string), template autoescaping on for HTML,
  URL-encoding for URLs and headers, JSON serializers for JSON.
- **Never reflect input back as a message.** Text from the request is never
  rendered as a notice or error; map keys to messages from a fixed set.
- **Redirect targets** are built from constants or from records that exist,
  never from raw request input.
- **Uploads** are checked for size and type by content, not by name, and are
  stored under generated names.
- **Secrets** live only in the environment or a secret store, never in code,
  logs, fixtures, test output, PR text or commits.
- **Environment variables:** if the project reads any, `.env.example` lists
  every one of them, each with a comment and no real value. It is the only
  list; add, rename or remove a variable there in the same PR.

## User data and persistence

- Never delete or overwrite user data because it could not be read or parsed.
  Move it aside (for example a timestamped `.bak`), then start fresh.
- A transient read error (lock, IO) never causes a delete or an overwrite.
- A change to a stored format bumps an explicit schema version and either
  migrates the old version or keeps a backup of it. Tests cover old, corrupt
  and newer-than-supported files.

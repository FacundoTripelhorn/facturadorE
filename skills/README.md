# Project skills

Portable [Agent Skills](https://agentskills.io) versioned with this repository.
They are **not** FacturadorE product docs (`docs/`) and are **not** tied to a
single agent vendor.

| Skill | Purpose |
|-------|---------|
| [`arca-kb/`](arca-kb/) | Distilled ARCA WSAA / WSFEXv1 knowledge (endpoints, auth, methods, observed errors, gotchas) |

## How to load

Any agent that implements the Agent Skills spec can discover the skill from its
directory (`SKILL.md` frontmatter). Examples:

- Point the agent/skills loader at `skills/arca-kb` (or the whole `skills/` tree).
- Symlink or copy into the loader path your environment uses (Cursor project
  skills, Claude Code skills dir, Codex skills dir, etc.).

Do **not** maintain divergent copies per vendor. Edit only under `skills/`.

## Validation

```bash
# From repo root (Agent Skills reference CLI; see arca-kb/VALIDATION.md)
uvx --from skills-ref agentskills validate skills/arca-kb
```

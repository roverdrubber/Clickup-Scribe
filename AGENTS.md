# clickup-scribe

Read and write ClickUp boards over the REST API, so bulk work does not burn the
ClickUp MCP server's small daily quota. Two runtimes, same behaviour: Python is
the default everywhere, PowerShell 5.1 the fallback for a Windows host with no
Python. No third-party packages.

This file orients an agent working *in* this repository. To use the tool
against a board, load the skills instead.

## Skills

| Skill | For |
|---|---|
| `skills/clickup-scribe/` | writing - one task, or a whole tree from a JSON spec |
| `skills/clickup-read/` | reading - hierarchy, tasks, search, export |
| `skills/clickup-setup/` | the one-time API token setup |

Each is a standard `SKILL.md` (YAML frontmatter with `name` and `description`,
markdown body, `references/` alongside). Codex and Claude Code both load them
as-is. Claude Code additionally reads `.claude-plugin/` and `commands/`; other
harnesses ignore those and lose nothing but the `/clickup-scribe:setup` alias.

Installing: `docs/claude-code-setup.md`, `docs/codex-setup.md`.

**The layout invariant:** `scripts/` must stay a sibling of the directory
holding the skill folders. Every skill reaches the client at `../../scripts/`,
which resolves to `<root>/scripts/` from `<root>/skills/<skill>/`. Moving
either one breaks all three skills wherever they are installed.

## Layout

```
scripts/     the actual tool - harness-agnostic, stdlib only
skills/      SKILL.md per skill, plus references/
docs/        per-harness setup guides
commands/    Claude Code slash command (a pointer to clickup-setup)
.claude-plugin/  Claude Code manifests
tests/       parity.py, mocks, fixtures
examples/    a worked spec
```

`scripts/` is shared by all three skills rather than copied into each. Two
copies of `clickup_client.py` would drift, which is the exact failure
`tests/parity.py` exists to catch.

## Rules for changing this repo

**Every change lands in both runtimes.** `scribe.py` and `scribe.ps1`,
`reader.py` and `reader.ps1`, `clickup_client.py` and `ClickUp.psm1` are twins.
A fix in one and not the other is the defect this project is most prone to.

**Run the parity suite before claiming a change works:**

```bash
python3 tests/parity.py       # 47 scenarios, both runtimes, diffed
python3 tests/parity.py -v    # show both outputs on failure
```

It needs `pwsh` or `powershell` on PATH and says so rather than reporting false
parity when neither is present. A new behaviour needs a new scenario; the mocks
in `tests/mocks/` are twins too.

**Never put a real token in this repo,** in a test, a fixture, or a commit
message. Tokens are read from `CLICKUP_TOKEN` or the stored file and nowhere
else - no command takes one as an argument. `.gitignore` excludes `.env` and
`/specs/`.

**Writes are dry-run first.** `apply` writes nothing without `--apply`; `add`
and `set` take `--dry-run`. Keep it that way - a half-applied batch against a
live board is expensive to reconcile.

**Idempotency is the contract.** Tasks are matched by name across the whole
list, following pagination to the end, before anything is created. A truncated
read means duplicates on someone's board.

**`commits` is a comment-based stand-in** for the integration API ClickUp does
not have: no endpoint attaches a commit, branch, or PR to a task. Its
idempotency key is the full 40-character SHA appearing anywhere in an existing
comment on the task. Changing the comment format so that SHA no longer appears
in full breaks dedupe against every comment already posted - each re-run would
post them all again.

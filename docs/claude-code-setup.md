# Setting up clickup-scribe in Claude Code

Claude Code loads this repository as a plugin: the three skills, the
`/clickup-scribe:setup` command, and the scripts they drive, from one install.
Unlike a bare skills install, you do not have to think about where `scripts/`
sits - the plugin keeps its own layout.

| Skill | Fires when | For |
|---|---|---|
| `clickup-scribe` | you ask to write to a board | one task, or a whole tree from a spec |
| `clickup-read` | you ask to read or export a board | hierarchy, tasks, search, export |
| `clickup-setup` | a command reports no token, or 401 | the one-time API token setup |

Plus `/clickup-scribe:setup`, a slash-command alias for the setup skill.

## Before you start

- **Claude Code**, any build with plugin support (`/plugin` opens the manager).
- **A runtime.** Python is the default - `python3` ships on Linux and macOS,
  and most Windows machines have it. PowerShell is the fallback for a Windows
  host without Python; 5.1 is part of the OS, so it needs no install. Detect
  Python by running it, not by whether the name resolves:
  `python3 --version || python --version || py -3 --version`. Neither
  implementation needs a third-party package.
- **A ClickUp personal token**, from ClickUp > Settings > Apps > API Token.
  Get one now; you will store it in step 3.

## 1. Install

### Option A: as a plugin (recommended)

In a session:

```
/plugin marketplace add /path/to/clickup-scribe
/plugin install clickup-scribe@clickup-scribe-marketplace
```

Or from a terminal:

```bash
claude plugin marketplace add /path/to/clickup-scribe
claude plugin install clickup-scribe@clickup-scribe-marketplace
```

The path is wherever you cloned the repo; `marketplace add` also takes a URL or
a GitHub `owner/repo`. Nothing in the plugin depends on that location - the
scripts resolve their own paths - so a clone, a marketplace install, or a
checkout on a network share all behave the same.

Restart the session, or start a new one, for the skills to load.

### Option B: as bare skills, no marketplace

If you want the skills without the plugin machinery, `~/.claude/skills/` is
auto-loaded. This is the same shape Codex uses, and it carries the same layout
rule: **`scripts/` must be a sibling of the directory holding the skill
folders.** Each skill reaches the shared client at `../../scripts/`, so from
`~/.claude/skills/<skill>/` that resolves to `~/.claude/scripts/`.

```bash
SRC=~/src/clickup-scribe

mkdir -p ~/.claude/skills
for s in clickup-scribe clickup-read clickup-setup; do
  ln -s "$SRC/skills/$s" ~/.claude/skills/$s
done
ln -s "$SRC/scripts" ~/.claude/scripts
```

You lose `/clickup-scribe:setup` - the `clickup-setup` skill covers the same
ground. See [`codex-setup.md`](codex-setup.md) for the same install explained
in more detail, including the Windows symlink form.

## 2. Verify the install

```bash
claude plugin validate /path/to/clickup-scribe   # manifests, skills, commands
claude plugin list                               # is it installed
claude plugin details clickup-scribe             # what it contributes
```

`details` prints the component inventory and the token cost, which is the
quickest way to confirm all three skills were picked up:

```
Component inventory
  Skills (4)  clickup-read, clickup-scribe, clickup-setup, setup

Projected token cost
  Always-on:   ~410 tok   added to every session
```

`/plugin` in a session shows the same thing interactively.

## 3. Store the token

Run `/clickup-scribe:setup` and let it walk you through, or do it directly.
Either way, **run the store command in your own terminal, not through Claude**:
the prompt is hidden, an agent's shell cannot type into it, and anything typed
in a session lands in the transcript.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" store
```

`${CLAUDE_PLUGIN_ROOT}` is set inside a Claude Code session but not in your own
shell, so ask Claude to print the resolved line rather than pasting the
variable. Simplest route: run any command with no token configured - the error
resolves its own absolute location and prints exactly what to run.

It prompts without echoing, verifies the token against the API before saving
anything, then writes it owner-only to `~/.config/clickup-scribe/token` (or
`%APPDATA%\clickup-scribe\token`). Every command reads it from there
afterwards, so no environment variable is needed.

`CLICKUP_TOKEN` in the environment overrides the stored file, for a one-off or
a CI run. If you would rather use the variable permanently, put it in
`~/.claude/settings.json` under `env` - the **user-level** file, never a
project `.claude/settings.json`, which usually gets committed.

## 4. Confirm end to end

Ask Claude for something that needs the board, or run the read side directly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reader.py" members
```

A list of workspace members means everything is wired: plugin installed, token
valid, scripts reachable. The numeric ids it prints are what a spec's
`assignees` field takes.

## 5. Use it

The skills fire on description match - ask for ClickUp work in plain language
and the right one loads. `clickup-scribe` covers writing, `clickup-read`
covers traversal and export, and both defer to the MCP server for single
interactive calls, which is what its daily quota is sized for.

Writes are dry-run first by design: `apply` writes nothing without `--apply`,
and `add` and `set` take `--dry-run`. Ask for the plan before the write.

## Working on the plugin itself

**Installing does not link the repo - it copies it.** The install lands in
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` and is pinned to
the commit it was taken from, recorded as `gitCommitSha` in
`~/.claude/plugins/installed_plugins.json`. Editing your clone therefore
changes nothing in a running session, and a skill you just added will not
appear.

To pick up your changes:

```bash
claude plugin marketplace update clickup-scribe-marketplace
claude plugin update clickup-scribe        # restart required to apply
```

Then restart the session. `claude plugin details clickup-scribe` reads from the
marketplace source rather than the installed copy, so it can list a skill the
running session cannot yet see - useful for confirming a change registered,
misleading if you read it as proof the session has it.

Before committing a change to the skills or manifests:

```bash
claude plugin validate .     # manifests, skills, agents, commands
python3 tests/parity.py      # both runtimes, diffed - 35 scenarios
```

`AGENTS.md` at the repo root carries the rest of the contributor rules.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Skills do not load after install | Session not restarted. Plugins load at session start |
| A skill you just wrote is missing | The install is a stale copy - see *Working on the plugin itself* |
| `No ClickUp token configured` | Not stored yet. The error prints the exact absolute command; use that line rather than rebuilding the path |
| `${CLAUDE_PLUGIN_ROOT}` is empty in your terminal | It is set only inside a session. Ask Claude to print the resolved path |
| `401` from any command | Token wrong or revoked. `setup.py clear`, then `setup.py store` |
| `python` opens the Microsoft Store | That is the Store stub, not a Python. Windows ships none by default; use the `.ps1` half, which needs nothing installed |
| First call fails like a network fault on Windows | PowerShell 5.1 does not negotiate TLS 1.2 by default. `ClickUp.psm1` forces it; make sure you are running the shipped module |

## What is Claude-specific here

`.claude-plugin/` (the plugin and marketplace manifests) and `commands/` (the
slash command). Everything else - the three skills and both runtimes - is
plain and portable; see [`codex-setup.md`](codex-setup.md) for the same tool
under Codex.

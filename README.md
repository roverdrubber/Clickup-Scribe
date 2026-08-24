# clickup-scribe

An agent plugin for writing to a ClickUp board — one task or a whole tree —
without spending the ClickUp MCP server's daily quota. Ships as a Claude Code
plugin and as three plain skills any agent that reads `SKILL.md` can load.

## The problem

ClickUp exposes two interfaces with separate quotas:

- **The MCP server** (`mcp.clickup.com`) meters tool calls against a small
  **daily** allowance, set by workspace plan and the Everything AI add-on.
  Reads count as well as writes. Exhausting it returns a `retryAfter` of around
  23 hours and blocks reads too.
- **The REST API** (`api.clickup.com/api/v2`) allows **100 requests per minute**
  on Free/Unlimited/Business plans, with no daily cap.

Populating a board over MCP therefore fails in a way that is both expensive and
easy to hit. One measured run exhausted the daily quota after 52 calls —
12 reads and 40 task creations — and left the board half-built.

This plugin routes scripted and bulk writes through REST, and keeps MCP for the
interactive calls it is sized for.

## Install

Full instructions per harness:

| Harness | Guide |
|---|---|
| Claude Code | [`docs/claude-code-setup.md`](docs/claude-code-setup.md) |
| Codex | [`docs/codex-setup.md`](docs/codex-setup.md) |

### Claude Code

Install it as a plugin; the layout takes care of itself.

```bash
/plugin marketplace add /path/to/clickup-scribe    # wherever you cloned it
/plugin install clickup-scribe@clickup-scribe-marketplace
```

Nothing in the plugin depends on that location: scripts resolve their own
paths, so it works from a clone, a marketplace install, or anywhere else.
Restart the session for the skills to load.

### Codex

Clone the repo, then link the three skill folders into a skills root and
`scripts/` one level above it.

```bash
SRC=~/src/clickup-scribe
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

mkdir -p "$CODEX_HOME/skills"
for s in clickup-scribe clickup-read clickup-setup; do
  ln -s "$SRC/skills/$s" "$CODEX_HOME/skills/$s"
done
ln -s "$SRC/scripts" "$CODEX_HOME/scripts"
```

### Any other agent

The three skills under `skills/` are plain `SKILL.md` files - YAML frontmatter
with `name` and `description`, a markdown body, `references/` alongside - so
any harness that loads skills in that shape reads them unmodified.

**One layout rule, wherever they go: `scripts/` must be a sibling of the
directory holding the skill folders.** All three share one copy of the client
and reach it at `../../scripts/`, so from `<root>/skills/<skill>/` it resolves
to `<root>/scripts/`. Clone the repo whole; a skill folder lifted out on its
own will load and have no tool to run.

`.claude-plugin/` and `commands/` are Claude Code's; other harnesses ignore
them and lose only the `/clickup-scribe:setup` alias, which is a pointer to the
`clickup-setup` skill everything else can load directly.

## Requirements

None to install. Two implementations ship side by side, and every host already
has one of them.

**Python is the default.** Linux and macOS ship `python3`, and most Windows
development machines have it too, so `scribe.py` and `reader.py` are the ones
to reach for.

**PowerShell is the fallback**, for a Windows host with no Python. Windows
Subsystem aside, Windows ships none by default, and typing `python` there
usually finds the Microsoft Store stub - a command that resolves and then
refuses to run rather than erroring like a missing one. So detect it by running
it, not by whether the name exists:

```
python3 --version || python --version || py -3 --version
```

The first that prints a version and exits 0 is your runtime. If none does and
the host is Windows, use the `.ps1` half: Windows PowerShell 5.1 is part of the
OS, so the fallback needs no install and no PowerShell 7. If none does
anywhere else, there is no runtime - see [no runtime at all](#no-runtime-at-all).

Neither implementation uses a third-party package: standard library and
built-in cmdlets only.

## Setup

Get a token from ClickUp > Settings > Apps > API Token. It starts with `pk_`,
carries your full permissions, and does not expire - so it is a password, not a
config value.

Store it once, in your own terminal:

```bash
python3 scripts/setup.py store
```
```powershell
.\scripts\setup.ps1 store
```

The prompt does not echo and the token is never printed back, so it stays out
of your shell history and out of any transcript. It is verified against the API
before anything is saved, then written to a user-only file:

| Host | Stored at |
|---|---|
| Linux, macOS | `~/.config/clickup-scribe/token` |
| Windows | `%APPDATA%\clickup-scribe\token` |

Every command reads it from there afterwards, so no environment variable is
needed. `setup.py show` reports where the token is coming from without ever
printing it, and `setup.py clear` removes it. In a Claude Code session,
`/clickup-scribe:setup` walks the same ground.

`CLICKUP_TOKEN` in the environment overrides the stored file, which is what
makes a one-off or a CI run possible without disturbing it:

```bash
CLICKUP_TOKEN=pk_xxxxxxxx python3 scripts/scribe.py show --list 901615936216
```

## Use

The examples below shorten the script path:

```bash
SCRIBE=scripts/scribe.py
```
```powershell
$SCRIBE = 'scripts\scribe.ps1'
```

### One task

```bash
python3 $SCRIBE add --list 901615936216 --name "Payroll Export" \
                    --parent 86d3qqd5f --description "Export a cutoff's payroll." \
                    --status completed --assignee 88978416
```
```powershell
& $SCRIBE add -List 901615936216 -Name "Payroll Export" `
              -Parent 86d3qqd5f -Description "Export a cutoff's payroll." `
              -Status completed -Assignee 88978416
```

Checks the name against the list first, so running it twice does not create two
tasks. `--dry-run` previews; `--allow-duplicate` skips the check.

Every later example is written for Python alone. The PowerShell form is
mechanical: both implementations take the same commands and the same arguments,
spelled to each host's convention.

| | Python | PowerShell |
|---|---|---|
| Invocation | `python3 scripts/scribe.py` | `.\scripts\scribe.ps1` |
| Flags | `--dry-run`, `--allow-duplicate` | `-DryRun`, `-AllowDuplicate` |
| Repeated values | `--tag a --tag b` | `-Tag a,b` |
| Line continuation | `\` | `` ` `` |

So `apply spec.json --apply` is `apply spec.json -Apply`, and
`export --list 901615936216 --as-spec` is `export -List 901615936216 -AsSpec`.
Every command in `scribe.ps1` and `reader.ps1` matches its Python twin; the
[`clickup-read`](skills/clickup-read/SKILL.md) skill lists both columns
side by side, and `tests/parity.py` proves they stay that way.

### Update one task

```bash
python3 $SCRIBE set 86d44avm2 --status completed
python3 $SCRIBE delete 86d44avm2 --yes
```

### A whole tree

```bash
python3 $SCRIBE apply spec.json           # plan only, writes nothing
python3 $SCRIBE apply spec.json --apply   # create what is missing
```

### Create containers

```bash
python3 $SCRIBE add-folder --space 90161372882 --name "Reporting"
python3 $SCRIBE add-list --folder 901610445259 --name "Frontend"
```

### Read the board

```bash
python3 $SCRIBE show --list 901615936216      # ids, statuses, parents
python3 $SCRIBE statuses --list 901615936216  # valid status strings
```

## Specs

A spec declares the tree you want to exist:

```json
{
  "list_id": "901615936216",
  "defaults": { "status": "completed", "assignees": [88978416] },
  "tasks": [
    {
      "name": "Attendance",
      "description": "Capture time in and time out.",
      "children": [
        { "name": "Attendance Devices", "description": "Register devices. Rotate the key." }
      ]
    },
    { "name": "Payroll Export", "parent": "86d3qqd5f", "description": "Export a cutoff's payroll." }
  ]
}
```

`children` nests new tasks under a task this spec creates. `parent` attaches to
a task already on the board. See
[`spec-format.md`](skills/clickup-scribe/references/spec-format.md) for the full
schema and [`examples/hris-api-list.json`](examples/hris-api-list.json) for a
worked one.

## Behaviour

- **Idempotent.** Tasks are matched by name across the whole list before
  writing. Re-running creates only what is missing, so an interrupted run
  resumes exactly where it stopped.
- **Dry run first.** `apply` writes nothing without `--apply`; `add` and `set`
  take `--dry-run`.
- **Parents before children.** A child's parent id comes from the create that
  produced it in the same run.
- **Paged reads.** The existence check follows pagination to the end; a
  truncated read would cause duplicates.
- **Validated up front.** Statuses are checked against the list's real status
  set before any write is attempted.
- **Converged specs write nothing.** Status fixes already satisfied are skipped.
- **Serialized.** No parallelism. Against a per-minute limit it buys nothing,
  and a partially applied batch costs more to reconcile than a slow run.

## When not to use it

For a single task added by hand while the MCP quota is healthy, use the MCP —
`clickup_create_task` is one call and needs no token. This plugin earns its
keep on scripted, repeated, or bulk writes, and when the quota is gone.

## Contents

```
AGENTS.md                orientation for an agent working in this repo
docs/
  claude-code-setup.md   installing and configuring under Claude Code
  codex-setup.md         installing and configuring under Codex
scripts/                 the tool - harness-agnostic, stdlib only
  clickup_client.py      REST client, Python standard library only
  ClickUp.psm1           REST client, PowerShell 5.1+
  setup.py / .ps1        store / show / clear the API token
  scribe.py / .ps1       add / set / apply / delete / add-folder / add-list /
                         show / statuses
  reader.py / .ps1       tree / tasks / get / find / members / export
skills/
  clickup-scribe/        writing: when REST vs MCP, commands, pitfalls
    references/
      spec-format.md     the JSON schema
      rest-endpoints.md  endpoints, auth, rate limits
      curl-recipes.md    raw curl, for hosts with no runtime
  clickup-read/          reading: hierarchy, tasks, search, export
  clickup-setup/         the one-time API token walkthrough
.claude-plugin/          Claude Code manifests
commands/
  setup.md               /clickup-scribe:setup, a pointer to clickup-setup
tests/
  parity.py              runs both implementations, diffs the output
  mocks/                 twin fake clients, one per runtime
  fixtures/              the shared workspace both mocks read
examples/
  hris-api-list.json     worked example
```

`scripts/` is shared by all three skills rather than copied into each: two
copies of the client would drift, which is what `tests/parity.py` exists to
catch.

## Reading

The `clickup-read` skill covers the other direction:

```bash
python3 scripts/reader.py tree                      # workspace structure
python3 scripts/reader.py tasks --list 901615936216 # tasks, subtasks nested
python3 scripts/reader.py find "payroll" --space 90161372882
python3 scripts/reader.py export --list 901615936216 --as-spec > board.json
```

`--as-spec` emits a spec `scribe apply` can consume, so a board can be read
from one list and replayed onto another.

Both clients are dependency-free and importable on their own for anything the
commands do not cover. The two implementations read the same spec files and
behave identically; they are verified against the same test matrix.

## No runtime at all

For a locked-down box, a container, or a single call not worth scripting,
[`curl-recipes.md`](skills/clickup-scribe/references/curl-recipes.md) has the
raw calls, with the shell-quoting differences that trip them up. curl covers
transport only - it cannot parse JSON, hold a name-to-id map, or page a result
set, so it is for inspecting and one-off changes, not for loops.

## Parity

Two implementations of one tool drift silently. `tests/parity.py` runs both
against the same fixture and diffs stdout and exit codes, so a fix that lands
in one and not the other fails loudly:

```bash
python3 tests/parity.py       # 35 scenarios
python3 tests/parity.py -v    # show both outputs on failure
```

It needs `pwsh` (or `powershell`) on PATH, and says so rather than reporting
false parity when neither is present.

## Token handling

The token comes from `CLICKUP_TOKEN` if that is set, otherwise from the file
`setup.py store` writes, and nowhere else. No command takes it as an argument,
so it cannot land in a shell history or a process list. Do not put it in a spec
file, commit it, or paste it into a chat transcript. `.gitignore` excludes
`.env` and `/specs/`.

## License

MIT

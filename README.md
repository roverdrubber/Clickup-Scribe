# clickup-scribe

A Claude Code plugin for writing to a ClickUp board — one task or a whole tree —
without spending the ClickUp MCP server's daily quota.

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

```bash
/plugin marketplace add ~/Projects/Personal/clickup-scribe
/plugin install clickup-scribe@clickup-scribe-marketplace
```

## Use

Get a token from ClickUp > Settings > Apps > API Token.

```bash
export CLICKUP_TOKEN=pk_xxxxxxxx
SCRIBE=skills/clickup-scribe/scripts/scribe.py
```

### One task

```bash
python3 $SCRIBE add --list 901615936216 --name "Payroll Export" \
                    --parent 86d3qqd5f --description "Export a cutoff's payroll." \
                    --status completed --assignee 88978416
```

Checks the name against the list first, so running it twice does not create two
tasks. `--dry-run` previews; `--allow-duplicate` skips the check.

### Update one task

```bash
python3 $SCRIBE set 86d44avm2 --status completed
```

### A whole tree

```bash
python3 $SCRIBE apply spec.json           # plan only, writes nothing
python3 $SCRIBE apply spec.json --apply   # create what is missing
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
.claude-plugin/          plugin and marketplace manifests
skills/clickup-scribe/
  SKILL.md               when REST vs MCP, commands, pitfalls
  references/
    spec-format.md       the JSON schema
    rest-endpoints.md    endpoints, auth, rate limits
  scripts/
    clickup_client.py    standard-library REST client
    scribe.py            add / set / apply / show / statuses
examples/
  hris-api-list.json     worked example
```

`clickup_client.py` has no dependencies beyond the standard library and is
importable on its own for anything the commands do not cover.

## Token handling

The token is read from `CLICKUP_TOKEN` and nowhere else. Do not put it in a
spec file, commit it, or paste it into a chat transcript. `.gitignore` excludes
`.env` and `/specs/`.

## License

MIT

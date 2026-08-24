# clickup-bulk

A Claude Code plugin for bulk ClickUp task work that does not spend the ClickUp
MCP server's daily quota.

## The problem

ClickUp exposes two interfaces with separate quotas:

- **The MCP server** (`mcp.clickup.com`) meters tool calls against a small
  **daily** allowance, set by workspace plan and the Everything AI add-on.
  Reads count as well as writes. Exhausting it returns a `retryAfter` of around
  23 hours and blocks reads too.
- **The REST API** (`api.clickup.com/api/v2`) allows **100 requests per minute**
  on Free/Unlimited/Business plans, with no daily cap.

Populating a board over MCP therefore fails in a way that is both expensive and
easy to hit. One measured run exhausted the daily quota after 52 calls -
12 reads and 40 task creations - and left the board half-built.

This plugin routes structural work through REST and keeps MCP for the
exploratory calls it is sized for.

## Install

```bash
/plugin marketplace add ~/Projects/Personal/clickup-bulk
/plugin install clickup-bulk@clickup-bulk-marketplace
```

## Use

Get a token from ClickUp > Settings > Apps > API Token.

```bash
export CLICKUP_TOKEN=pk_xxxxxxxx

# describe the board you want
$EDITOR spec.json

# see the plan; writes nothing
python3 skills/clickup-bulk/scripts/bulk_tasks.py spec.json

# create what is missing
python3 skills/clickup-bulk/scripts/bulk_tasks.py spec.json --apply

# read back what is on the list
python3 skills/clickup-bulk/scripts/bulk_tasks.py spec.json --verify
```

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
[`skills/clickup-bulk/references/spec-format.md`](skills/clickup-bulk/references/spec-format.md)
for the full schema and
[`examples/hris-api-list.json`](examples/hris-api-list.json) for a worked one.

## Behaviour

- **Idempotent.** Tasks are matched by name across the whole list before
  writing. Re-running creates only what is missing, so an interrupted run
  resumes exactly where it stopped.
- **Dry run by default.** Nothing is written without `--apply`.
- **Parents before children.** A child's parent id comes from the create that
  produced it in the same run.
- **Paged reads.** The existence check follows pagination to the end; a
  truncated read would cause duplicates.
- **Validated up front.** Statuses are checked against the list's real status
  set before any create is attempted.
- **Serialized.** No parallelism. Against a per-minute limit it buys nothing,
  and a partially applied batch costs more to reconcile than a slow run.

## Contents

```
.claude-plugin/          plugin and marketplace manifests
skills/clickup-bulk/
  SKILL.md               when to use REST vs MCP, workflow, pitfalls
  references/
    spec-format.md       the JSON schema
    rest-endpoints.md    endpoints, auth, rate limits
  scripts/
    clickup_client.py    standard-library REST client
    bulk_tasks.py        spec runner
examples/
  hris-api-list.json     worked example
```

`clickup_client.py` has no dependencies beyond the standard library and is
importable on its own for anything the runner does not cover.

## Token handling

The token is read from `CLICKUP_TOKEN` and nowhere else. Do not put it in a
spec file, commit it, or paste it into a chat transcript. `.gitignore` excludes
`.env` and `/specs/`.

## License

MIT

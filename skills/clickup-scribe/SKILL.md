---
name: clickup-scribe
description: Use when writing to a ClickUp board - creating a task, updating a task's status, populating a list, mirroring a roadmap or backlog onto a board, building parent/subtask trees - or when recovering from "Rate limit exceeded" on the ClickUp MCP server. Handles a single entry or a whole tree, over the REST API, without spending the MCP's small daily quota.
---

# Writing to a ClickUp board

ClickUp exposes two interfaces with **separate quotas**. Picking the wrong one
for the size of the job is what turns two minutes of work into a day of waiting.

| | ClickUp MCP (`mcp.clickup.com`) | REST API (`api.clickup.com/api/v2`) |
|---|---|---|
| Limit | Small **daily** quota, set by workspace plan and the Everything AI add-on | **100 req/min** (Free/Unlimited/Business), 1,000 (Business Plus), 10,000 (Enterprise) |
| Counts | Reads *and* writes | Reads and writes |
| Exhausted | `retryAfter` ~23h, blocks reads too | Resets within the minute |
| Auth | Session OAuth | `Authorization: pk_...` |

One observed exhaustion: **52 successful MCP calls** (12 reads + 40 creates)
before the 53rd was refused for 23 hours.

## Which interface

**One task, interactively, quota healthy? Use the MCP.** `clickup_create_task`
is a single call and needs no token. Do not reach for this skill to add one
task to a board by hand - that is what the daily quota is sized for.

**Use `scribe.py` when any of these is true:**

- More than ~5 writes, or any loop over a collection
- The MCP quota is exhausted and the work cannot wait
- The write is scripted, repeated, or needs to be re-runnable
- You want the name check that stops a re-run creating duplicates

Roughly: MCP for a keystroke, scribe for anything you would not want to redo by
hand.

## Commands

```bash
export CLICKUP_TOKEN=pk_xxxxxxxx     # Settings > Apps > API Token
```

**One task:**

```bash
scribe.py add --list 901615936216 --name "Payroll Export" \
              --parent 86d3qqd5f --description "Export a cutoff's payroll." \
              --status completed --assignee 88978416
```

Checks the name against the list first and does nothing if it is already there.
`--dry-run` to preview, `--allow-duplicate` to create unconditionally.

**Update one task:**

```bash
scribe.py set 86d44avm2 --status completed
```

**A whole tree:**

```bash
scribe.py apply spec.json              # plan only, writes nothing
scribe.py apply spec.json --apply      # create what is missing
```

**Read the board:**

```bash
scribe.py show --list 901615936216      # ids, statuses, parents
scribe.py statuses --list 901615936216  # valid status strings
```

Never write the token into a spec, the repo, or the conversation. If the user
must supply it, have them run the command in their own terminal.

## Workflow for a bulk job

1. **Locate the target once, over MCP.** `clickup_get_workspace_hierarchy` and
   `clickup_get_list` give the list id and its statuses. Two calls.
2. **Write those ids into the spec.** They are stable forever; never spend
   calls rediscovering them.
3. **Describe the whole tree** (`references/spec-format.md`).
4. **Dry run,** then `--apply`.

## What the runner guarantees

- **Idempotent.** Every write matches by name against the whole list first, so
  a re-run creates only what is missing and an interrupted run resumes cleanly.
- **Parents before children.** A child's `parent` is filled from the id its
  parent returned during this same run.
- **Paged reads.** The existence check follows pagination to the end.
- **Status validated up front** against the list's real statuses, before any
  write is attempted.
- **Already-satisfied status fixes skipped**, so a converged spec writes nothing.
- **Serialized and paced.** No parallelism.

## Pitfalls

**Never parallel-batch near a quota.** A 12-call batch that splits 7 succeeded /
5 refused leaves the board half-built and you reconciling by hand. Serialized
work fails at a known point.

**Page the existence check.** `GET /list/{id}/task` caps at 100 tasks per page,
and a short read looks exactly like "nothing else exists" - which makes an
idempotency check create duplicates.

**`filter_tasks` does not return `parent`.** To learn a board's tree over MCP,
call `get_task(include:["subtasks"])` on each top-level entry - one call per
parent. Never fetch children individually to discover their parent.

**Match the board's existing shape.** Read a few current entries before adding
to a populated list. Naming, granularity, and how deep the nesting goes are
conventions worth copying rather than reinventing.

**Names are the idempotency key.** Renaming in a spec creates a second copy
rather than renaming the original; use `scribe.py set --name` for that.

## Reference

- `references/spec-format.md` - the JSON schema, with a worked example
- `references/rest-endpoints.md` - endpoints, auth, and rate limits
- `scripts/clickup_client.py` - importable client for anything the commands
  do not cover

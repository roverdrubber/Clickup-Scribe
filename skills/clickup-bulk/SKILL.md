---
name: clickup-bulk
description: Use when creating, updating, or restructuring more than a handful of ClickUp tasks at once - populating a list, mirroring a roadmap or backlog onto a board, building parent/subtask trees, or recovering from "Rate limit exceeded" on the ClickUp MCP server. Routes bulk work through the REST API so it does not consume the MCP's small daily quota.
---

# Bulk ClickUp work

ClickUp exposes two interfaces with **separate quotas**. Picking the wrong one
is what turns a two-minute job into a one-day wait.

| | ClickUp MCP (`mcp.clickup.com`) | REST API (`api.clickup.com/api/v2`) |
|---|---|---|
| Limit | Small **daily** quota, set by workspace plan and the Everything AI add-on | **100 req/min** (Free/Unlimited/Business), 1,000 (Business Plus), 10,000 (Enterprise) |
| Counts | Reads *and* writes | Reads and writes |
| Exhausted | `retryAfter` ~23h, blocks reads too | Resets within the minute |
| Auth | Session OAuth | `Authorization: pk_...` |

One observed exhaustion: **52 successful MCP calls** (12 reads + 40 creates)
before the 53rd was refused for 23 hours.

## The rule

**Know the full shape of the change already? Use REST.** Creating N tasks,
restructuring a board, mirroring a plan onto a list, fixing statuses in bulk.

**Still exploring? Use MCP.** Finding a list, reading a task, checking what a
board looks like, answering a one-off question. Interactive work is what the
daily quota is sized for.

Roughly: more than ~5 writes, or any loop over a collection, means REST.

## Workflow

1. **Locate the target once, over MCP.** `clickup_get_workspace_hierarchy` and
   `clickup_get_list` give the list id and its valid statuses. Two calls.
2. **Write those ids down** in the spec file. They are stable forever; never
   spend calls rediscovering them.
3. **Write a spec** (`references/spec-format.md`) describing the whole tree.
4. **Dry run.** `python3 scripts/bulk_tasks.py spec.json`
5. **Apply.** `python3 scripts/bulk_tasks.py spec.json --apply`

```bash
export CLICKUP_TOKEN=pk_xxxxxxxx     # Settings > Apps > API Token
python3 scripts/bulk_tasks.py spec.json --apply
```

Never write the token into the spec, the repo, or the conversation. If the user
must supply it, have them run the command in their own terminal.

## What the runner guarantees

- **Idempotent.** Matches by name against the whole list before writing, so a
  re-run creates only what is missing. An interrupted run resumes cleanly.
- **Parents before children.** A child's `parent` is filled from the id its
  parent returned during this same run.
- **Paged reads.** The existence check follows pagination to the end.
- **Status validated up front** against the list's real statuses, before any
  create is attempted.
- **Serialized and paced.** No parallelism.

## Pitfalls

**Never parallel-batch near a quota.** A 12-call batch that splits 7 succeeded /
5 refused leaves the board half-built and you reconciling by hand. Serialized
work fails at a known point.

**Page the existence check.** `GET /list/{id}/task` caps at 100 tasks per page
and a short read looks exactly like "nothing else exists" - which makes an
idempotency check create duplicates.

**`filter_tasks` does not return `parent`.** To learn a board's tree over MCP,
call `get_task(include:["subtasks"])` on each top-level entry - one call per
parent. Never fetch children individually to discover their parent.

**Match the board's existing shape.** Read a few current entries before adding
to a populated list. Naming, granularity, and how deep the subtask nesting goes
are conventions worth copying rather than reinventing.

## Reference

- `references/spec-format.md` - the JSON schema, with a worked example
- `references/rest-endpoints.md` - the endpoints, auth, and rate-limit headers
- `scripts/clickup_client.py` - importable client for anything the runner
  does not cover

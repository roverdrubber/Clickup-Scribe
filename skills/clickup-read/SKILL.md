---
name: clickup-read
description: Use when reading ClickUp structure or content - listing spaces, folders and lists, walking a workspace hierarchy, pulling every task and subtask on a board, inspecting one task, searching task names, or exporting a board to JSON. Traverses over the REST API so a deep walk does not exhaust the MCP server's small daily quota.
---

# Reading a ClickUp workspace

The hierarchy, in ClickUp's API vocabulary - which the UI hides:

```
Workspace (called a "team" in the API)
  Space
    Folder
      List -> Task -> Subtask
    List            (folderless: lists can sit directly in a Space)
```

**Folderless lists are a separate endpoint.** Traverse only `folder/{id}/list`
and whole lists disappear from your picture with no error to warn you. In the
HRIS workspace, `General Activities` is exactly such a list.

## Which interface

**One lookup? Use the MCP.** `clickup_get_task`, `clickup_get_list`, or
`clickup_search` is a single call and needs no token.

**Use `reader` for traversal.** Walking a workspace is one call per space, per
folder, per list, plus one per page of tasks. That is dozens of calls - enough
to exhaust the MCP's daily quota in a single command, blocking reads *and*
writes for ~23 hours. The same walk over REST costs a minute of a 100/min
budget.

Also use it when the output feeds something else: a diff, a report, a spec.

## Commands

Token setup is one-time and shared with the other skills - see the
`clickup-setup` skill for the full walkthrough. In short: the user runs
`python3 scripts/setup.py store` in their own terminal, and every command picks
the token up from there. `CLICKUP_TOKEN` in the environment overrides it.

**Default to `reader.py`.** `reader.ps1` is the fallback for a Windows host
with no Python; the `clickup-scribe` skill's *Pick the runtime* section has the
probe and the rule.

| Goal | Python | PowerShell |
|---|---|---|
| Whole structure | `reader.py tree` | `.\reader.ps1 tree` |
| One space | `reader.py tree --space 90161372882` | `.\reader.ps1 tree -Space 90161372882` |
| Structure only, no lists | `reader.py tree --spaces-only` | `.\reader.ps1 tree -SpacesOnly` |
| Tasks, subtasks nested | `reader.py tasks --list 901615936216` | `.\reader.ps1 tasks -List 901615936216` |
| One task in full | `reader.py get 86d3qqa1e` | `.\reader.ps1 get 86d3qqa1e` |
| Search names | `reader.py find "payroll" --space 90161372882` | `.\reader.ps1 find payroll -Space 90161372882` |
| Dump a list | `reader.py export --list 901615936216` | `.\reader.ps1 export -List 901615936216` |

Add `--json` / `-Json` to any command for machine-readable output on stdout.

## Round-tripping into the write side

```bash
reader.py export --list 901615936216 --as-spec > board.json
scribe.py apply board.json --apply          # replay it onto another list
```

`--as-spec` emits a spec the `clickup-scribe` skill can apply directly: names,
descriptions, statuses, and the parent/child nesting. Edit `list_id` and you
have cloned a board's shape onto a new one. Useful for templating a board that
gets recreated per project, and for diffing what a board *is* against what a
spec says it should be.

## What the reader handles

- **Folderless lists**, which a naive traversal drops silently.
- **Subtask nesting.** ClickUp returns subtasks flat, each carrying a `parent`
  id; the reader rebuilds the tree. A subtask whose parent is not in the same
  response - filtered out, or living on another list - is surfaced at top level
  rather than dropped.
- **Pagination.** Task reads follow `last_page` to the end. A list of more than
  100 tasks is otherwise silently truncated.
- **Call accounting.** Every human-readable command prints how many API calls
  it spent, so the cost of a traversal is visible before you repeat it.

## Pitfalls

**`find` is a substring match, not full-text search.** REST v2 has no search
endpoint, so `find` walks the lists in scope and filters names client-side.
Narrow with `--list` or `--space`; an unscoped workspace walk is a lot of
calls. For real full-text search across docs and comments, the MCP's
`clickup_search` is the right tool - it is one call.

**Scope before you walk.** `tree --spaces-only` costs one call per workspace;
a full `tree` costs one per space plus one per folder plus two per space for
lists. Find the space id first, then traverse inside it.

**Ids are stable; record them.** A list id never changes. Write it into a spec
or a note rather than re-walking the hierarchy to find it again.

## Reference

- `../clickup-scribe/references/rest-endpoints.md` - endpoints, auth, limits
- `../clickup-scribe/references/curl-recipes.md` - raw curl for a single read
  when there is no runtime available
- `../clickup-scribe/references/spec-format.md` - what `--as-spec` produces
- `../../scripts/reader.py`, `reader.ps1` - the commands
- `../../scripts/clickup_client.py`, `ClickUp.psm1` - clients

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

**Use `scribe` when any of these is true:**

- More than ~5 writes, or any loop over a collection
- The MCP quota is exhausted and the work cannot wait
- The write is scripted, repeated, or needs to be re-runnable
- You want the name check that stops a re-run creating duplicates

Roughly: MCP for a keystroke, scribe for anything you would not want to redo by
hand.

## Pick the runtime

Two implementations, same behaviour, same spec files, neither needing anything
installed. **Default to `scribe.py`.** Fall back to `scribe.ps1` only where
Python is genuinely absent, which in practice means a Windows host without it.

Probe once, at the point you first need to run something, and reuse the answer
for the rest of the session:

```bash
python3 --version || python --version || py -3 --version
```

Use the first that **prints a version and exits 0**. Resolving as a command is
not enough: on Windows a bare `python` is usually the Microsoft Store stub,
which sits on PATH and then refuses to run rather than erroring like a missing
command. Treat a non-zero exit, or no version string, as absent.

| Probe | Use |
|---|---|
| A Python that runs | `scribe.py` |
| None, and the host is Windows | `scribe.ps1` |
| None, and the host is not Windows | No runtime - see `references/curl-recipes.md` |

Do not probe on a host you already know: Linux and macOS ship `python3`, so go
straight to `scribe.py` there and spend the probe only on Windows.

`scribe.ps1` targets Windows PowerShell 5.1, so the fallback needs no
PowerShell 7 install and no modules - 5.1 is part of the OS. On Windows it also
forces TLS 1.2, which 5.1 does not negotiate by default and without which the
first call fails looking like a network fault.

## Commands

**First run only.** Have the user do this in their own terminal - the prompt
is hidden, so an agent's shell cannot type into it:

```bash
python3 scripts/setup.py store
```
```powershell
.\setup.ps1 store
```

It prompts without echoing, verifies the token against the API before saving,
and stores it owner-only in `~/.config/clickup-scribe/token` (or
`%APPDATA%\clickup-scribe\token`). Every command reads it from there
afterwards. `setup.py show` reports where the token comes from without printing
it; `setup.py clear` removes it.

`CLICKUP_TOKEN` in the environment still overrides the stored file, for a
one-off or a CI run:

```bash
CLICKUP_TOKEN=pk_xxxxxxxx python3 scripts/reader.py tree
```

**One task:**

```bash
scribe.py add --list 901615936216 --name "Payroll Export" \
              --parent 86d3qqd5f --description "Export a cutoff's payroll." \
              --status completed --assignee 88978416
```
```powershell
.\scribe.ps1 add -List 901615936216 -Name "Payroll Export" `
                 -Parent 86d3qqd5f -Description "Export a cutoff's payroll." `
                 -Status completed -Assignee 88978416
```

Checks the name against the list first and does nothing if it is already there.
`--dry-run` to preview, `--allow-duplicate` to create unconditionally.

Fields beyond name and status: `--priority urgent|high|normal|low`,
`--due` / `--start` as `YYYY-MM-DD[ HH:MM]`, `--estimate` in minutes, `--tag`
(repeatable), `--assignee` taking an id, email, or username.

**Update one task:**

```bash
scribe.py set 86d44avm2 --status completed
```
```powershell
.\scribe.ps1 set 86d44avm2 -Status completed
```

`set` takes the same field flags as `add`.

**Remove a task:**

```bash
scribe.py delete 86d44avm2 --yes
```
```powershell
.\scribe.ps1 delete 86d44avm2 -Yes
```

Without `--yes` it prints what would go - the task and its subtasks - and
refuses. Deleted tasks sit in the workspace trash for 30 days. This is the only
command here that destroys anything.

**Create the container first,** when the list does not exist yet:

```bash
scribe.py add-folder --space 90161372882 --name "Reporting"
scribe.py add-list --folder 901610445259 --name "Frontend"
scribe.py add-list --space 90161372882 --name "Scratch"   # folderless
```
```powershell
.\scribe.ps1 add-folder -Space 90161372882 -Name "Reporting"
.\scribe.ps1 add-list -Folder 901610445259 -Name "Frontend"
```

A list needs exactly one container - `--folder` or `--space`, never both.
Both commands skip silently if the name already exists, so they are safe to
put at the top of a setup script. Feed the returned list id to a spec.

**A whole tree:**

```bash
scribe.py apply spec.json              # plan only, writes nothing
scribe.py apply spec.json --apply      # create what is missing
```
```powershell
.\scribe.ps1 apply spec.json           # plan only
.\scribe.ps1 apply spec.json -Apply    # create what is missing
```

**Converge an existing board** with `--reconcile` / `-Reconcile`:

```bash
scribe.py apply spec.json --reconcile              # show drift
scribe.py apply spec.json --apply --reconcile      # create AND update
```

Without it, `apply` only creates what is missing and leaves existing tasks
untouched - which is the safe default, since a task may have been edited in
the UI on purpose.

**Only fields the spec declares are compared.** A spec silent about priority
never changes a priority. `description` is deliberately excluded: the write
field is `markdown_content` and the read field is a normalised `description`,
so equality is unreliable and every run would re-send the same text. Assignees
are excluded too - updates use add/remove semantics rather than a flat list.

PowerShell uses `-Apply`, `-DryRun`, and `-Reconcile` where Python uses
`--apply`, `--dry-run`, and `--reconcile`; everything else maps one to one.

**Link shipped commits to a task:**

```bash
scribe.py commits 86d44avm2 --range v1.2.0..v1.3.0 --dry-run   # preview
scribe.py commits 86d44avm2 --range v1.2.0..v1.3.0             # post
scribe.py commits --spec links.json                             # plan only
scribe.py commits --spec links.json --apply                     # post
scribe.py commits --spec links.json --rollup --apply            # + parent summary
```
```powershell
.\scribe.ps1 commits 86d44avm2 -Range v1.2.0..v1.3.0 -DryRun
.\scribe.ps1 commits -Spec links.json -Apply
.\scribe.ps1 commits -Spec links.json -Rollup -Apply
```

Why a comment: ClickUp's public API (v2 and v3) has no endpoint for the
Bitbucket/GitHub integration panel. It cannot list connected repositories,
cannot "Add Bitbucket link", and cannot attach a commit or a branch. The panel
fills itself only when ClickUp receives a push webhook whose commit message,
branch name, or PR title contains `CU-<task-id>` - and history already pushed
cannot gain that without being rewritten. A comment is the only record the API
can write.

So `commits` posts one plain-text comment per task: a title, then each commit
as its 8-character short SHA, subject, and web link. Ranges are read with
`git log --reverse --no-merges` from `--repo` (default: the current directory)
and deduplicated; `--range` repeats (`-Range a..b,c..d` in PowerShell). The web
link comes from the `origin` remote - Bitbucket, GitHub, or any GitLab host -
or from `--web-url` for anything else.

**Idempotent by SHA.** Every comment on the task is read, past the first page,
and a commit whose full 40-character SHA appears in any of them is skipped. A
re-run posts nothing; a widened range posts only what is new. Dry run first:
a single task posts unless `--dry-run`, and `--spec` writes nothing without
`--apply`.

A links spec is a JSON array, or an object carrying shared `repo` / `web_url`
(command-line flags win):

```json
{
  "repo": "../my-service",
  "links": [
    { "task": "86d44avm2", "ranges": ["v1.2.0..v1.3.0"] },
    { "task": "86d44avm3", "ranges": ["a1b2c3d..e4f5a6b"], "title": "Rounding fix" }
  ]
}
```

Every entry is validated and every range resolved before the first comment is
read, so one bad range stops the run with the board untouched.

**`--rollup` (`-Rollup`): a summary on the parent.** The commit lists stay on
the subtasks; each parent (domain) task gets one comment summarising its
subtasks' work. Works in single-task and spec mode. For every task in the run -
new commits or not - the task is read (`GET /task/{id}`) for its `parent` and
`name`; a top-level task is skipped. Each subtask's line counts all commits of
its ranges, not only the new ones:

```
Subtask commits — through 2026-03-14

Payslips (CU-86d44avm2) — 4 commits, 2026-03-02 → 2026-03-14
latest a8a602b557b5ea7ed345a17b0d2b32bae22506df
```

Dates are author dates. Lines are ordered by last date, then name. Every
comment on the parent is read, past the first page, and a line is already
rolled up when one comment holds both `CU-<subtask id>` and that subtask's
latest full SHA. One comment per parent carries only the lines that are not;
when none are left, nothing is posted. So a subtask that gained commits gets a
fresh line, and an unchanged sibling is not repeated. Subtask comments are
posted first, rollups after, and every read happens before either. The dry
run shows each parent's lines (`+` new, `=` already rolled up) and the body.

A rollup comment carries full SHAs, so running plain `commits` against the
parent itself treats those commits as already linked there. Link commits to
leaf subtasks and let the parent take rollups.

**Convention going forward.** Put the task id in a commit footer,
`Refs: CU-<task-id>` - comma-separate several, `Refs: CU-abc, CU-def`. It
passes `@commitlint/config-conventional` and lets the integration panel link
the commit on push. Do not rename branches to `CU-...`: that breaks
`feature/<name>` conventions, and a branch can name only one task. Keep
`commits` for backfill and for commits that missed the footer. Link commits to
the leaf subtask that did the work, not to a parent or domain row.

A footer may name the parent as well as the subtask,
`Refs: CU-<subtask>, CU-<parent>`, so the parent's integration panel lists the
commit too. `commits --rollup` is the readable summary on top of that: one
line per subtask rather than every commit.

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
rather than renaming the original; use the `set` command for that.

**Tags must already exist** in the space. Creating a tag is not something this
covers - the MCP's `add_tag_to_task` handles that, or the UI.

**Descriptions are never reconciled**, even with `--reconcile`. Use `set` to
change one.

**`--estimate` and `time_estimate` are minutes.** The API counts milliseconds;
both are converted for you, so `90` means an hour and a half.

## Still not covered

Deliberately. These stay on the MCP, which handles them in one call each and
has no bulk story worth scripting: comments (other than `commits`' links),
documents, chat, time tracking,
attachments, tags creation, task dependencies and links, moving or merging
tasks, reminders, custom field values, and full-text search.

## Reference

- `references/spec-format.md` - the JSON schema, with a worked example
- `references/rest-endpoints.md` - endpoints, auth, and rate limits
- `references/curl-recipes.md` - one-off calls with no runtime at all, for a
  locked-down box or a single change. Not for loops: no idempotency, no
  pagination, no parent ordering
- `../../scripts/clickup_client.py` - importable Python client for anything the
  commands do not cover
- `../../scripts/ClickUp.psm1` - the same client in PowerShell

To **read** a board rather than write to it - hierarchy, tasks, search, export
- use the `clickup-read` skill.

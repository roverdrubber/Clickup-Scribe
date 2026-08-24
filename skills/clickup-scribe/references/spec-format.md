# Spec format

A spec is one JSON file describing everything a run should end with. It is
declarative: re-running against the same spec converges on the same board.

```json
{
  "list_id": "901615936216",
  "defaults": {
    "status": "completed",
    "assignees": [88978416]
  },
  "status_fixes": [
    { "task_id": "86d44avm2", "status": "completed" }
  ],
  "tasks": [
    {
      "name": "Attendance",
      "description": "Capture time in and time out.",
      "children": [
        { "name": "Attendance Devices", "description": "Register devices. Rotate the key." },
        { "name": "Biometric Intake",   "description": "Device pushes punches. Throttled and deduped." }
      ]
    },
    {
      "name": "Payroll Export",
      "parent": "86d3qqd5f",
      "description": "Export a cutoff period's payroll."
    }
  ]
}
```

## Keys

| Key | Where | Meaning |
|---|---|---|
| `list_id` | top level, required | The list every task is created in. Subtasks must share their parent's list. |
| `defaults` | top level, optional | `status` and `assignees` applied to any task that does not set its own. |
| `status_fixes` | top level, optional | `{task_id, status}` pairs applied before creates. For correcting tasks a previous run got wrong. |
| `tasks` | top level, required | The tree. |
| `name` | task, required | Also the idempotency key. Must be unique on the list. |
| `description` | task, optional | Sent as `markdown_content`. |
| `children` | task, optional | Created as subtasks, after their parent and using its id. |
| `parent` | task, optional | An **existing** task id to attach to. Use when extending a board you did not create in this run. |
| `status` | task, optional | Overrides `defaults.status`. Validated against the list before anything is written. |
| `assignees` | task, optional | User ids, emails, or usernames. Overrides `defaults.assignees`. |
| `priority` | task, optional | `urgent`, `high`, `normal`, `low`, or 1-4. |
| `due_date` | task, optional | `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`. Date alone becomes an all-day due date. |
| `start_date` | task, optional | Same formats as `due_date`. |
| `time_estimate` | task, optional | Minutes, as a number. |
| `tags` | task, optional | Array of tag names. They must already exist in the space. |

`children` and `parent` are the two ways to place a task. `children` nests
inside this spec; `parent` points at something already on the board. A task may
use either, not both.

## Names are the idempotency key

Existence is checked by name across the entire list, subtasks and closed tasks
included. Consequences:

- Two tasks on one list may not share a name, or the second is treated as
  already present and skipped.
- Renaming a task in the spec makes the next run create a second copy rather
  than rename the original. Rename on the board, or add a `status_fixes`-style
  correction, instead.
- Descriptions are **not** reconciled. Editing a description in the spec does
  not update an existing task; the runner only creates what is missing.

Anything in `defaults` applies to every task that does not set its own value,
so a spec whose tasks all share a priority or an assignee states it once.

## Getting user ids

`assignees` accepts an email or username as well as a numeric id, resolving it
against the workspace. That costs one extra call per run, so for a large spec
prefer ids:

```bash
reader.py members          # id, username, email
```

Record the id in the spec and the lookup never happens again.

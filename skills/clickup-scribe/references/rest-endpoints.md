# ClickUp REST v2, the parts bulk work needs

Base: `https://api.clickup.com/api/v2`

## Auth

```http
Authorization: pk_xxxxxxxx
```

A personal token from ClickUp > Settings > Apps > API Token. No `Bearer`
prefix - the raw `pk_` value is the header. OAuth tokens use
`Authorization: Bearer {access_token}` instead.

Keep it in `CLICKUP_TOKEN`. Never in a spec file, a repo, or a chat transcript.

## Rate limits

Per token, per minute, by workspace plan:

| Plan | Requests/min |
|---|---|
| Free Forever / Unlimited / Business | 100 |
| Business Plus | 1,000 |
| Enterprise | 10,000 |

A 429 carries `Retry-After` in seconds. The client waits it out automatically
when it is under two minutes and raises otherwise - a long value means a cap
no amount of waiting inside one run will clear.

These limits are **independent of the MCP server's daily quota**. Exhausting
the MCP does not affect REST, and vice versa.

## Endpoints

### List tasks

```
GET /list/{list_id}/task?include_closed=true&subtasks=true&page=0
```

Returns `{"tasks": [...], "last_page": bool}`. **100 tasks per page.** Follow
`last_page` to the end; a truncated read silently understates what exists.

Each task carries `id`, `name`, `status.status`, `parent`, `assignees`. Note
that this endpoint *does* return `parent`, unlike the MCP's `filter_tasks`.

### Create task

```
POST /list/{list_id}/task
{
  "name": "Attendance Devices",
  "markdown_content": "Register devices. Rotate the key.",
  "parent": "86d44avkf",
  "status": "completed",
  "assignees": [88978416]
}
```

Only `name` is required. `parent` makes it a subtask and **must be a task in
the same list**. `markdown_content` renders markdown; `description` is the
plain-text alternative. Returns the created task, including its `id`.

### Update task

```
PUT /task/{task_id}
{ "status": "completed" }
```

Send only the fields that change.

### List metadata

```
GET /list/{list_id}
```

Returns `statuses[]` - the only authoritative source for which status strings
this list accepts. Validate against it before a bulk create; an invalid status
fails every create in the run, one wasted call at a time.

### Members

```
GET /team
```

Members and their numeric ids, for `assignees`.

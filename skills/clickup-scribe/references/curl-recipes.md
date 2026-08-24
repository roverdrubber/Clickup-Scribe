# curl recipes

For when there is no runtime at all: a locked-down box, a container, or a
single call you do not want to write a script for. curl is present on Windows
10 1803+, Windows 11, macOS, and most Linux images.

**These are for one-off calls.** They deliberately do not attempt idempotency,
pagination, or parent-before-child ordering - that is what `scribe` and
`reader` are for. A loop of curl calls that creates duplicates on its second
run is worse than no automation.

## Auth

```bash
export CLICKUP_TOKEN=pk_xxxxxxxx
```
```powershell
$env:CLICKUP_TOKEN = 'pk_xxxxxxxx'
```
```cmd
set CLICKUP_TOKEN=pk_xxxxxxxx
```

The raw `pk_` value is the header. No `Bearer` prefix.

## Quoting, which is where this goes wrong

The same curl command is written three different ways depending on the shell.

| Shell | Variable | Quotes |
|---|---|---|
| bash, zsh | `$CLICKUP_TOKEN` | single quotes preserve literally |
| PowerShell | `$env:CLICKUP_TOKEN` | use `curl.exe`, not `curl` (see below) |
| cmd.exe | `%CLICKUP_TOKEN%` | double quotes only; no single quotes |

**In PowerShell, always write `curl.exe`.** Bare `curl` is an alias for
`Invoke-WebRequest`, which takes completely different arguments and will fail
confusingly.

**For any request with a body, put the JSON in a file.** Shell interpolation of
JSON is the single most common way these break - a description containing a
quote or a newline produces invalid JSON, and the error comes back from
ClickUp as a parse failure rather than anything that points at the quoting.

## Reads

Workspaces (the API calls these "teams"):

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  https://api.clickup.com/api/v2/team
```

Spaces in a workspace:

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  "https://api.clickup.com/api/v2/team/9016376012/space?archived=false"
```

Folders in a space:

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  "https://api.clickup.com/api/v2/space/90161372882/folder?archived=false"
```

Lists in a folder, then the folderless lists in the space - **both are needed**
for a complete picture:

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  "https://api.clickup.com/api/v2/folder/901610445259/list?archived=false"

curl -s -H "Authorization: $CLICKUP_TOKEN" \
  "https://api.clickup.com/api/v2/space/90161372882/list?archived=false"
```

Tasks on a list. **Caps at 100 per page**; bump `page` until the response has
`"last_page": true`:

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  "https://api.clickup.com/api/v2/list/901615936216/task?include_closed=true&subtasks=true&page=0"
```

One task, with its subtasks:

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  "https://api.clickup.com/api/v2/task/86d3qqa1e?include_subtasks=true"
```

A list's valid statuses, which you need before writing one:

```bash
curl -s -H "Authorization: $CLICKUP_TOKEN" \
  https://api.clickup.com/api/v2/list/901615936216
```

## Writes

Write the body to a file first. This works identically in every shell and is
immune to quoting.

`body.json`:

```json
{
  "name": "Payroll Export",
  "markdown_content": "Export a cutoff's payroll.",
  "parent": "86d3qqd5f",
  "status": "completed",
  "assignees": [88978416]
}
```

Create the task:

```bash
curl -s -X POST \
  -H "Authorization: $CLICKUP_TOKEN" \
  -H "Content-Type: application/json" \
  --data @body.json \
  https://api.clickup.com/api/v2/list/901615936216/task
```
```powershell
curl.exe -s -X POST `
  -H "Authorization: $env:CLICKUP_TOKEN" `
  -H "Content-Type: application/json" `
  --data '@body.json' `
  https://api.clickup.com/api/v2/list/901615936216/task
```

`parent` makes it a subtask and must be a task on the same list. Omit any field
you do not want to set; only `name` is required.

Update a task - send only what changes:

```bash
echo '{"status":"completed"}' > patch.json
curl -s -X PUT \
  -H "Authorization: $CLICKUP_TOKEN" \
  -H "Content-Type: application/json" \
  --data @patch.json \
  https://api.clickup.com/api/v2/task/86d44avm2
```

## Reading the response without jq

jq is not installed on Windows, macOS, or a default Ubuntu. These are:

```bash
... | python3 -m json.tool                    # pretty-print, Linux/macOS
```
```powershell
curl.exe -s ... | ConvertFrom-Json | Format-List        # Windows, native
(curl.exe -s ... | ConvertFrom-Json).tasks |
    Select-Object id, name, @{n='status';e={$_.status.status}}
```

With jq, if you do have it:

```bash
... | jq -r '.tasks[] | "\(.id)  \(.status.status)  \(.name)"'
```

## Rate limits

REST allows 100 requests/minute on Free, Unlimited, and Business; 1,000 on
Business Plus; 10,000 on Enterprise. This is a separate budget from the MCP
server's daily quota - exhausting one does not affect the other.

Show the status code and headers when diagnosing:

```bash
curl -s -D - -o /dev/null -H "Authorization: $CLICKUP_TOKEN" \
  https://api.clickup.com/api/v2/team
```

A `401` means the token was rejected. A `429` carries `Retry-After` in seconds.

## When to stop using these

The moment you are writing a loop. Pagination, an existence check before
creating, parent ids captured from one response and fed into the next - each is
a few lines in `scribe`/`reader` and a source of silent bugs in shell. Use the
recipes to inspect, diagnose, and make single changes; use the scripts for
anything repeated.

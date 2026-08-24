<#
.SYNOPSIS
    Minimal ClickUp REST v2 client for PowerShell. No modules to install.

.DESCRIPTION
    The PowerShell half of clickup-scribe, for machines with no Python. Targets
    Windows PowerShell 5.1 - the version present on a fresh Windows install -
    as well as PowerShell 7+, so nothing here uses 6+ only syntax.

    Exists because ClickUp's MCP server meters tool calls against a small daily
    AI quota, while the REST API allows 100 requests per minute on Free/
    Unlimited/Business plans and has no daily cap.

        $env:CLICKUP_TOKEN = 'pk_xxxxxxxx'   # Settings > Apps > API Token
#>

Set-StrictMode -Version 2.0

$script:Api = 'https://api.clickup.com/api/v2'

# ClickUp stores priority as 1-4. Nobody remembers which way round it runs, so
# both the CLI and specs accept the names and this maps them.
$script:Priorities = [ordered]@{ urgent = 1; high = 2; normal = 3; low = 4 }
$script:Calls = 0
$script:LastCall = [DateTime]::MinValue
$script:PaceMs = 150

# Windows PowerShell 5.1 negotiates SSL3/TLS1.0 by default, which ClickUp
# refuses. Without this the very first call dies with a connection error that
# looks like a network fault rather than a protocol mismatch.
if ($PSVersionTable.PSVersion.Major -lt 6) {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
}


function ConvertTo-Priority {
    <#.SYNOPSIS Accept 'high', 'HIGH', or 2. Returns $null for an empty value.#>
    param($Value)
    if ($null -eq $Value -or "$Value" -eq '') { return $null }
    $key = "$Value".Trim().ToLower()
    if ($key -match '^[0-9]+$') {
        $number = [int]$key
        if ($script:Priorities.Values -notcontains $number) {
            throw "priority must be 1-4, got $number"
        }
        return $number
    }
    if (-not $script:Priorities.Contains($key)) {
        throw ("unknown priority '{0}'. Use one of: {1}" -f $Value,
               ($script:Priorities.Keys -join ', '))
    }
    return $script:Priorities[$key]
}


function ConvertTo-EpochMs {
    <#
    .SYNOPSIS
        'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' -> @(milliseconds, hasTime).
    .DESCRIPTION
        ClickUp wants milliseconds since the epoch plus a separate flag saying
        whether the time half is meaningful; a date with no time renders as an
        all-day due date rather than midnight. Parsed as local time, matching
        the Python implementation.
    #>
    param($Value)
    if ($null -eq $Value -or "$Value" -eq '') { return $null }
    $text = "$Value".Trim()
    if ($text -match '^[0-9]{10,}$') { return @([long]$text, $true) }

    $formats = @(@('yyyy-MM-dd HH:mm', $true), @('yyyy-MM-dd', $false))
    foreach ($pair in $formats) {
        $parsed = [DateTime]::MinValue
        $ok = [DateTime]::TryParseExact($text, $pair[0],
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None, [ref]$parsed)
        if ($ok) {
            $local = [DateTime]::SpecifyKind($parsed, [DateTimeKind]::Local)
            $ms = [long]([DateTimeOffset]::new($local)).ToUnixTimeMilliseconds()
            return @($ms, $pair[1])
        }
    }
    throw "cannot read date '$Value'. Use YYYY-MM-DD or YYYY-MM-DD HH:MM."
}


function New-ClickUpTaskBody {
    <#
    .SYNOPSIS
        The fields shared by create and update, omitting anything unset.
    .DESCRIPTION
        Sending a field as null is not the same as omitting it - ClickUp reads
        an explicit null as "clear this" - so only what the caller actually
        supplied goes into the body.
    #>
    param(
        [string]$Name, [string]$Description, [string]$Parent, [string]$Status,
        [int[]]$Assignees, $Priority, $DueDate, $StartDate,
        $TimeEstimate, [string[]]$Tags
    )
    $body = @{}
    if ($Name)        { $body['name'] = $Name }
    if ($Description) { $body['markdown_content'] = $Description }
    if ($Parent)      { $body['parent'] = $Parent }
    if ($Status)      { $body['status'] = $Status }
    if ($Assignees -and $Assignees.Count -gt 0) { $body['assignees'] = @($Assignees) }

    $level = ConvertTo-Priority -Value $Priority
    if ($null -ne $level) { $body['priority'] = $level }

    $due = ConvertTo-EpochMs -Value $DueDate
    if ($null -ne $due) { $body['due_date'] = $due[0]; $body['due_date_time'] = $due[1] }

    $start = ConvertTo-EpochMs -Value $StartDate
    if ($null -ne $start) { $body['start_date'] = $start[0]; $body['start_date_time'] = $start[1] }

    # The API counts milliseconds; the CLI and specs take minutes, which is
    # what a person means and what the MCP also accepts.
    if ($TimeEstimate) { $body['time_estimate'] = [int]$TimeEstimate * 60000 }
    if ($Tags -and $Tags.Count -gt 0) { $body['tags'] = @($Tags) }
    return $body
}


function Get-ClickUpMembers {
    <#
    .SYNOPSIS
        Everyone across every visible workspace: id, username, email.
    .DESCRIPTION
        GET /team already embeds members, so this costs one call rather than
        one per workspace.
    #>
    param()
    $people = @{}
    foreach ($team in (Get-ClickUpTeams)) {
        $members = @()
        if ($team.PSObject.Properties.Name -contains 'members') { $members = @($team.members) }
        foreach ($member in $members) {
            $user = $member
            if ($member.PSObject.Properties.Name -contains 'user') { $user = $member.user }
            if ($null -eq $user.id) { continue }
            $name = ''
            if ($user.PSObject.Properties.Name -contains 'username' -and $user.username) { $name = $user.username }
            $mail = ''
            if ($user.PSObject.Properties.Name -contains 'email' -and $user.email) { $mail = $user.email }
            $teamName = ''
            if ($team.PSObject.Properties.Name -contains 'name') { $teamName = $team.name }
            $people[[string]$user.id] = [ordered]@{
                id = $user.id; username = $name; email = $mail; workspace = $teamName
            }
        }
    }
    return @($people.Values | Sort-Object { $_.username }, { $_.id })
}


function Resolve-ClickUpAssignees {
    <#
    .SYNOPSIS
        Turn ids, emails, or usernames into numeric ids.
    .DESCRIPTION
        Numeric values pass straight through, so the common case costs nothing.
        A lookup happens only when a name or email is actually present.
    #>
    param($Values)
    $wanted = @($Values | Where-Object { $null -ne $_ -and "$_" -ne '' })
    if ($wanted.Count -eq 0) { return @() }
    if (-not ($wanted | Where-Object { "$_" -notmatch '^[0-9]+$' })) {
        return @($wanted | ForEach-Object { [int]"$_" })
    }

    $people = Get-ClickUpMembers
    $resolved = @()
    foreach ($value in $wanted) {
        $text = "$value"
        if ($text -match '^[0-9]+$') { $resolved += [int]$text; continue }
        $match = @($people | Where-Object {
            $_.email.ToLower() -eq $text.ToLower() -or
            $_.username.ToLower() -eq $text.ToLower() })
        if ($match.Count -eq 0) {
            throw ("no workspace member matches '{0}'. Run the reader's ``members`` command to list them." -f $value)
        }
        if ($match.Count -gt 1) {
            throw ("'{0}' matches several members ({1}). Use the id." -f $value,
                   (($match | ForEach-Object { $_.id }) -join ', '))
        }
        $resolved += [int]$match[0].id
    }
    return $resolved
}


function Get-ClickUpCallCount {
    <#.SYNOPSIS Number of API calls made this session.#>
    [OutputType([int])]
    param()
    return $script:Calls
}


function Get-ClickUpToken {
    [OutputType([string])]
    param()
    $token = $env:CLICKUP_TOKEN
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw ('CLICKUP_TOKEN is not set. Get one from ClickUp > Settings > ' +
               'Apps > API Token, then: $env:CLICKUP_TOKEN = ''pk_...''')
    }
    return $token
}


function Get-ErrorDetail {
    <#.SYNOPSIS Response body from a failed call, on either PowerShell edition.#>
    param($ErrorRecord)

    # PowerShell 7 surfaces the body here.
    try {
        if ($ErrorRecord.ErrorDetails -and $ErrorRecord.ErrorDetails.Message) {
            return $ErrorRecord.ErrorDetails.Message
        }
    } catch { }

    # Windows PowerShell 5.1 requires reading the stream.
    try {
        $stream = $ErrorRecord.Exception.Response.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream)
        $body = $reader.ReadToEnd()
        $reader.Dispose()
        return $body
    } catch {
        return ''
    }
}


function Get-RetryAfter {
    <#.SYNOPSIS Retry-After in seconds, defaulting to 60 when absent.#>
    [OutputType([double])]
    param($ErrorRecord)
    try {
        $headers = $ErrorRecord.Exception.Response.Headers
        $value = $null
        # 5.1 indexes by name; 7 exposes GetValues.
        try { $value = $headers['Retry-After'] } catch { }
        if (-not $value) {
            try { $value = ($headers.GetValues('Retry-After'))[0] } catch { }
        }
        if ($value) { return [double]$value }
    } catch { }
    return 60.0
}


function Invoke-ClickUp {
    <#
    .SYNOPSIS
        One REST call, paced and with 429 handling.
    .DESCRIPTION
        Calls are serialized deliberately. Parallelism buys nothing against a
        per-minute limit and turns quota exhaustion into a partially applied
        change, which costs far more to reconcile than a slow run.
    #>
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PUT', 'DELETE')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        $Body = $null,
        [int]$Retries = 2
    )

    $elapsed = ([DateTime]::UtcNow - $script:LastCall).TotalMilliseconds
    if ($elapsed -lt $script:PaceMs) {
        Start-Sleep -Milliseconds ([int]($script:PaceMs - $elapsed))
    }
    $script:LastCall = [DateTime]::UtcNow

    $params = @{
        Uri         = $script:Api + $Path
        Method      = $Method
        Headers     = @{ Authorization = (Get-ClickUpToken) }
        ContentType = 'application/json'
        ErrorAction = 'Stop'
    }
    if ($null -ne $Body) {
        $params['Body'] = ($Body | ConvertTo-Json -Depth 12 -Compress)
    }

    try {
        $result = Invoke-RestMethod @params
        $script:Calls++
        return $result
    } catch {
        $status = 0
        try { $status = [int]$_.Exception.Response.StatusCode } catch { }

        if ($status -eq 429) {
            $wait = Get-RetryAfter -ErrorRecord $_
            # A short window is worth waiting out; a daily cap is not.
            if ($Retries -gt 0 -and $wait -le 120) {
                Start-Sleep -Seconds ([int]$wait + 1)
                return Invoke-ClickUp -Method $Method -Path $Path -Body $Body `
                                      -Retries ($Retries - 1)
            }
            throw ("CLICKUP_RATE_LIMIT:{0}" -f $wait)
        }

        $detail = Get-ErrorDetail -ErrorRecord $_
        if ($detail.Length -gt 500) { $detail = $detail.Substring(0, 500) }
        throw ("{0} {1} -> {2}: {3}" -f $Method, $Path, $status, $detail)
    }
}


function Get-ClickUpTasks {
    <#
    .SYNOPSIS
        Every task on a list, following pagination to the end.
    .DESCRIPTION
        Stopping at page 0 caps silently at 100 tasks, which reads exactly like
        "nothing else exists" and makes an idempotency check create duplicates.
    #>
    param(
        [Parameter(Mandatory)][string]$ListId,
        [bool]$IncludeClosed = $true,
        [bool]$Subtasks = $true
    )

    $tasks = New-Object System.Collections.ArrayList
    $page = 0
    while ($true) {
        $query = 'include_closed={0}&subtasks={1}&page={2}' -f
                 $IncludeClosed.ToString().ToLower(),
                 $Subtasks.ToString().ToLower(),
                 $page
        $payload = Invoke-ClickUp -Method GET -Path "/list/$ListId/task?$query"

        $batch = @()
        if ($payload.PSObject.Properties.Name -contains 'tasks' -and $payload.tasks) {
            $batch = @($payload.tasks)
        }
        foreach ($task in $batch) { [void]$tasks.Add($task) }

        $lastPage = $true
        if ($payload.PSObject.Properties.Name -contains 'last_page') {
            $lastPage = [bool]$payload.last_page
        }
        if ($lastPage -or $batch.Count -eq 0) { break }
        $page++
    }
    return $tasks.ToArray()
}


function Get-ClickUpList {
    param([Parameter(Mandatory)][string]$ListId)
    return Invoke-ClickUp -Method GET -Path "/list/$ListId"
}


function Get-ClickUpStatuses {
    <#.SYNOPSIS The status strings a list actually accepts.#>
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string]$ListId)
    $list = Get-ClickUpList -ListId $ListId
    if ($list.PSObject.Properties.Name -contains 'statuses' -and $list.statuses) {
        return @($list.statuses | ForEach-Object { $_.status })
    }
    return @()
}


function Get-ClickUpTask {
    param(
        [Parameter(Mandatory)][string]$TaskId,
        [bool]$Subtasks = $true
    )
    $query = ''
    if ($Subtasks) { $query = '?include_subtasks=true' }
    return Invoke-ClickUp -Method GET -Path "/task/$TaskId$query"
}


# -- hierarchy --------------------------------------------------------------
#
# ClickUp's own vocabulary, which the UI hides: a Workspace is a "team" in the
# API. Lists live either in a Folder or directly in a Space, and the folderless
# ones are a separate endpoint - miss it and whole lists vanish from a
# traversal without any error.

function Get-ClickUpTeams {
    <#.SYNOPSIS Workspaces the token can see.#>
    param()
    $payload = Invoke-ClickUp -Method GET -Path '/team'
    if ($payload.PSObject.Properties.Name -contains 'teams') { return @($payload.teams) }
    return @()
}


function Get-ClickUpSpaces {
    param([Parameter(Mandatory)][string]$TeamId, [bool]$Archived = $false)
    $flag = $Archived.ToString().ToLower()
    $payload = Invoke-ClickUp -Method GET -Path "/team/$TeamId/space?archived=$flag"
    if ($payload.PSObject.Properties.Name -contains 'spaces') { return @($payload.spaces) }
    return @()
}


function Get-ClickUpFolders {
    param([Parameter(Mandatory)][string]$SpaceId, [bool]$Archived = $false)
    $flag = $Archived.ToString().ToLower()
    $payload = Invoke-ClickUp -Method GET -Path "/space/$SpaceId/folder?archived=$flag"
    if ($payload.PSObject.Properties.Name -contains 'folders') { return @($payload.folders) }
    return @()
}


function Get-ClickUpFolderlessLists {
    <#.SYNOPSIS Lists sitting directly in a Space, with no Folder above them.#>
    param([Parameter(Mandatory)][string]$SpaceId, [bool]$Archived = $false)
    $flag = $Archived.ToString().ToLower()
    $payload = Invoke-ClickUp -Method GET -Path "/space/$SpaceId/list?archived=$flag"
    if ($payload.PSObject.Properties.Name -contains 'lists') { return @($payload.lists) }
    return @()
}


function Get-ClickUpFolderLists {
    param([Parameter(Mandatory)][string]$FolderId, [bool]$Archived = $false)
    $flag = $Archived.ToString().ToLower()
    $payload = Invoke-ClickUp -Method GET -Path "/folder/$FolderId/list?archived=$flag"
    if ($payload.PSObject.Properties.Name -contains 'lists') { return @($payload.lists) }
    return @()
}


function New-ClickUpTask {
    param(
        [Parameter(Mandatory)][string]$ListId,
        [Parameter(Mandatory)][string]$Name,
        [string]$Description = '',
        [string]$Parent = $null,
        [string]$Status = $null,
        [int[]]$Assignees = @(),
        $Priority = $null,
        $DueDate = $null,
        $StartDate = $null,
        $TimeEstimate = $null,
        [string[]]$Tags = @()
    )

    $body = New-ClickUpTaskBody -Name $Name -Description $Description `
        -Parent $Parent -Status $Status -Assignees $Assignees `
        -Priority $Priority -DueDate $DueDate -StartDate $StartDate `
        -TimeEstimate $TimeEstimate -Tags $Tags
    return Invoke-ClickUp -Method POST -Path "/list/$ListId/task" -Body $body
}


# -- structure --------------------------------------------------------------
#
# A List is created against whichever container holds it, so the endpoint
# differs: inside a Folder, or directly in a Space. There is no single
# "create list" call that takes a parent of either kind.

function New-ClickUpFolder {
    param([Parameter(Mandatory)][string]$SpaceId, [Parameter(Mandatory)][string]$Name)
    return Invoke-ClickUp -Method POST -Path "/space/$SpaceId/folder" -Body @{ name = $Name }
}


function New-ClickUpList {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$FolderId,
        [string]$SpaceId,
        [string]$Content
    )
    if ([bool]$FolderId -eq [bool]$SpaceId) {
        throw 'a list needs exactly one container: FolderId or SpaceId.'
    }
    $body = @{ name = $Name }
    if ($Content) { $body['content'] = $Content }
    if ($FolderId) { return Invoke-ClickUp -Method POST -Path "/folder/$FolderId/list" -Body $body }
    return Invoke-ClickUp -Method POST -Path "/space/$SpaceId/list" -Body $body
}


function Get-ClickUpDrift {
    <#
    .SYNOPSIS
        Fields in $Wanted that the live task does not already match.
    .DESCRIPTION
        Only what the spec declares is compared, so a field the spec is silent
        about is never touched - a priority set by hand in the UI survives a
        spec that says nothing about priority.

        `description` is deliberately not reconciled. We send markdown_content
        and ClickUp returns a normalised description, so equality is not
        reliable and every run would re-send the same text.
    #>
    param($Current, [hashtable]$Wanted)

    $skip = @('name', 'markdown_content', 'parent', 'assignees',
              'due_date_time', 'start_date_time')
    $changed = @{}

    function Get-Cur($obj, $key) {
        if ($null -eq $obj) { return $null }
        if ($obj.PSObject.Properties.Name -notcontains $key) { return $null }
        return $obj.$key
    }

    foreach ($key in @($Wanted.Keys | Sort-Object)) {
        if ($skip -contains $key) { continue }
        $value = $Wanted[$key]

        if ($key -eq 'status') {
            $live = $null
            $st = Get-Cur $Current 'status'
            if ($st) { $live = Get-Cur $st 'status' }
            if ($live -ne $value) { $changed['status'] = $value }
        }
        elseif ($key -eq 'priority') {
            $raw = Get-Cur $Current 'priority'
            if ($raw -and $raw.PSObject.Properties.Name -contains 'priority') {
                $raw = $raw.priority
            }
            $live = $null
            if ($null -ne $raw -and "$raw" -ne '') { $live = [int]"$raw" }
            if ($live -ne $value) { $changed['priority'] = $value }
        }
        elseif (@('due_date', 'start_date', 'time_estimate') -contains $key) {
            $raw = Get-Cur $Current $key
            $live = $null
            if ($null -ne $raw -and "$raw" -ne '') { $live = [long]"$raw" }
            if ($live -ne [long]$value) {
                $changed[$key] = $value
                $flag = "${key}_time"
                if ($Wanted.ContainsKey($flag)) { $changed[$flag] = $Wanted[$flag] }
            }
        }
        elseif ($key -eq 'tags') {
            $liveTags = @()
            foreach ($t in @(Get-Cur $Current 'tags')) {
                if ($t -is [string]) { $liveTags += $t }
                elseif ($t.PSObject.Properties.Name -contains 'name') { $liveTags += $t.name }
            }
            $a = (@($liveTags | Sort-Object) -join '|')
            $b = (@($value | Sort-Object) -join '|')
            if ($a -ne $b) { $changed['tags'] = $value }
        }
    }
    return $changed
}


function Remove-ClickUpTask {
    <#.SYNOPSIS Move a task to the workspace trash. Recoverable there for 30 days.#>
    param([Parameter(Mandatory)][string]$TaskId)
    return Invoke-ClickUp -Method DELETE -Path "/task/$TaskId"
}


function Set-ClickUpTask {
    param(
        [Parameter(Mandatory)][string]$TaskId,
        [Parameter(Mandatory)][hashtable]$Fields
    )
    return Invoke-ClickUp -Method PUT -Path "/task/$TaskId" -Body $Fields
}


Export-ModuleMember -Function Invoke-ClickUp, Get-ClickUpTasks, Get-ClickUpList,
    Get-ClickUpStatuses, New-ClickUpTask, Set-ClickUpTask, Get-ClickUpToken,
    Get-ClickUpCallCount, Get-ClickUpTask, Get-ClickUpTeams, Get-ClickUpSpaces,
    Get-ClickUpFolders, Get-ClickUpFolderlessLists, Get-ClickUpFolderLists,
    Remove-ClickUpTask, Get-ClickUpMembers, Resolve-ClickUpAssignees,
    New-ClickUpTaskBody, ConvertTo-Priority, ConvertTo-EpochMs,
    New-ClickUpFolder, New-ClickUpList, Get-ClickUpDrift

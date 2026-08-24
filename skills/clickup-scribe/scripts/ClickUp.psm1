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
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PUT')][string]$Method,
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


function New-ClickUpTask {
    param(
        [Parameter(Mandatory)][string]$ListId,
        [Parameter(Mandatory)][string]$Name,
        [string]$Description = '',
        [string]$Parent = $null,
        [string]$Status = $null,
        [int[]]$Assignees = @()
    )

    $body = @{ name = $Name }
    if ($Description) { $body['markdown_content'] = $Description }
    if ($Parent)      { $body['parent'] = $Parent }
    if ($Status)      { $body['status'] = $Status }
    if ($Assignees -and $Assignees.Count -gt 0) {
        # Force an array even for one element, or ConvertTo-Json emits a scalar.
        $body['assignees'] = @($Assignees)
    }
    return Invoke-ClickUp -Method POST -Path "/list/$ListId/task" -Body $body
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
    Get-ClickUpCallCount

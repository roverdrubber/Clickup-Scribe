<#
.SYNOPSIS
    Write to a ClickUp board over REST: one task, or a whole tree.

.DESCRIPTION
    The PowerShell half of clickup-scribe, behaviourally identical to scribe.py
    and reading the same spec files. Targets Windows PowerShell 5.1 and later.

    Every write is idempotent by task name: a task that already exists is
    reused, never duplicated. Re-running after an interruption resumes where it
    stopped.

.EXAMPLE
    $env:CLICKUP_TOKEN = 'pk_xxxxxxxx'
    .\scribe.ps1 add -List 901615936216 -Name 'Payroll Export' `
                     -Parent 86d3qqd5f -Status completed -Assignee 88978416

.EXAMPLE
    .\scribe.ps1 set 86d44avm2 -Status completed

.EXAMPLE
    .\scribe.ps1 apply spec.json            # plan only, writes nothing
    .\scribe.ps1 apply spec.json -Apply     # create what is missing

.EXAMPLE
    .\scribe.ps1 show -List 901615936216
    .\scribe.ps1 statuses -List 901615936216

.EXAMPLE
    .\scribe.ps1 commits 86d44avm2 -Range v1.2.0..v1.3.0 -DryRun
    .\scribe.ps1 commits -Spec links.json -Apply
    .\scribe.ps1 commits -Spec links.json -Rollup -Apply
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('add', 'set', 'apply', 'delete', 'add-folder', 'add-list',
                 'show', 'statuses', 'commits')]
    [string]$Command,

    # Task id for `set` and `commits`, spec path for `apply`.
    [Parameter(Position = 1)]
    [string]$Target,

    [string]$List,
    [string]$Space,
    [string]$Folder,
    [string]$Content,
    [string]$Name,
    [string]$Description,
    [string]$Parent,
    [string]$Status,
    [string[]]$Assignee,
    [string]$Priority,
    [string]$Due,
    [string]$Start,
    [int]$Estimate,
    [string[]]$Tag,
    # `commits`: revision ranges (repeat as -Range a..b,c..d), the repository,
    # its web address, the comment title, and a links spec.
    [string[]]$Range,
    [string]$Repo,
    [string]$WebUrl,
    [string]$Title,
    [string]$Spec,
    [switch]$AllowDuplicate,
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$Reconcile,
    # `commits`: also post one summary comment per parent task.
    [switch]$Rollup,
    [switch]$Yes
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Inside a function $PSBoundParameters is that function's, not the script's.
# Capture it here so `set -Description ''` can be told from -Description omitted.
$script:Bound = $PSBoundParameters

Import-Module (Join-Path $PSScriptRoot 'ClickUp.psm1') -Force


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

function Get-Prop {
    <#.SYNOPSIS Read a property that may be absent from a JSON object.#>
    param($Object, [string]$PropertyName, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object.PSObject.Properties.Name -contains $PropertyName) {
        $value = $Object.$PropertyName
        if ($null -ne $value) { return $value }
    }
    return $Default
}


function New-SpecNode {
    <#.SYNOPSIS One planned task, linked to its parent node.#>
    param($Spec, $Defaults, $ParentNode = $null)

    $node = @{
        Name           = [string](Get-Prop $Spec 'name')
        Description    = [string](Get-Prop $Spec 'description' '')
        Status         = [string](Get-Prop $Spec 'status' (Get-Prop $Defaults 'status' ''))
        Assignees      = @(Get-Prop $Spec 'assignees' (Get-Prop $Defaults 'assignees' @()))
        Priority       = (Get-Prop $Spec 'priority' (Get-Prop $Defaults 'priority'))
        DueDate        = (Get-Prop $Spec 'due_date' (Get-Prop $Defaults 'due_date'))
        StartDate      = (Get-Prop $Spec 'start_date' (Get-Prop $Defaults 'start_date'))
        TimeEstimate   = (Get-Prop $Spec 'time_estimate' (Get-Prop $Defaults 'time_estimate'))
        Tags           = @(Get-Prop $Spec 'tags' (Get-Prop $Defaults 'tags' @()))
        ExplicitParent = Get-Prop $Spec 'parent'
        ParentNode     = $ParentNode
        ExistingId     = $null
        CreatedId      = $null
        Children       = @()
    }
    if (-not $node.Name) { throw 'every task in a spec needs a name' }

    foreach ($child in @(Get-Prop $Spec 'children' @())) {
        $node.Children += ,(New-SpecNode -Spec $child -Defaults $Defaults -ParentNode $node)
    }
    return $node
}


function Get-NodeId {
    param($Node)
    if ($Node.ExistingId) { return $Node.ExistingId }
    return $Node.CreatedId
}


function Get-NodeParentId {
    param($Node)
    if ($null -ne $Node.ParentNode) { return (Get-NodeId $Node.ParentNode) }
    return $Node.ExplicitParent
}


function Get-NodeList {
    <#.SYNOPSIS Depth-first flatten: parents always precede their children.#>
    param($Nodes)
    $flat = New-Object System.Collections.ArrayList
    foreach ($node in $Nodes) {
        [void]$flat.Add($node)
        foreach ($descendant in (Get-NodeList -Nodes $node.Children)) {
            [void]$flat.Add($descendant)
        }
    }
    return $flat.ToArray()
}


function Format-FieldValue {
    <#.SYNOPSIS Render a value the same way both runtimes do.#>
    param($Value)
    if ($Value -is [array]) { return (@($Value) -join ',') }
    return "$Value"
}


function Write-Tree {
    param($Nodes, [int]$Indent = 0)
    foreach ($node in $Nodes) {
        $mark = if ($node.ExistingId) { '=' } else { '+' }
        $detail = if ($node.Status) { "  ($($node.Status))" } else { '' }
        Write-Host ("  {0}{1} {2}{3}" -f (' ' * $Indent), $mark, $node.Name, $detail)
        Write-Tree -Nodes $node.Children -Indent ($Indent + 2)
    }
}


function Read-Board {
    <#
    .SYNOPSIS
        name -> id, id -> status, id -> full task for a list. One paged read.
    .DESCRIPTION
        The full rows are kept because reconciliation compares against fields
        the name/status pair does not carry.
    #>
    param([string]$ListId)
    $names = @{}
    $statuses = @{}
    $byId = @{}
    foreach ($task in (Get-ClickUpTasks -ListId $ListId)) {
        if (-not $names.ContainsKey($task.name)) { $names[$task.name] = $task.id }
        $statuses[$task.id] = $task.status.status
        $byId[$task.id] = $task
    }
    return @{ Names = $names; Statuses = $statuses; ById = $byId }
}


function Test-Status {
    <#.SYNOPSIS Reject a bad status before spending calls on doomed writes.#>
    param([string]$ListId, [string[]]$Wanted)
    $wanted = @($Wanted | Where-Object { $_ })
    if ($wanted.Count -eq 0) { return $true }

    $valid = Get-ClickUpStatuses -ListId $ListId
    $unknown = @($wanted | Where-Object { $valid -notcontains $_ } | Sort-Object -Unique)
    if ($unknown.Count -gt 0) {
        Write-Host ("unknown status for this list: {0}" -f ($unknown -join ', '))
        Write-Host ("valid statuses: {0}" -f (($valid | Sort-Object) -join ', '))
        return $false
    }
    return $true
}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

function Invoke-Add {
    if (-not $List) { Write-Host 'add requires -List'; return 1 }
    if (-not $Name) { Write-Host 'add requires -Name'; return 1 }
    if (-not (Test-Status -ListId $List -Wanted @($Status))) { return 1 }

    $existingId = $null
    if (-not $AllowDuplicate) {
        $board = Read-Board -ListId $List
        if ($board.Names.ContainsKey($Name)) { $existingId = $board.Names[$Name] }
    }

    if ($existingId) {
        Write-Host ("exists   {0}  {1}" -f $existingId, $Name)
        Write-Host 'Nothing to do. Pass -AllowDuplicate to create a second copy.'
        return 0
    }

    $plan = $Name
    if ($Parent) { $plan += "  (subtask of $Parent)" }
    if ($Status) { $plan += "  [$Status]" }
    Write-Host "  + $plan"

    if ($DryRun) {
        Write-Host "`nDry run. Drop -DryRun to write."
        return 0
    }

    $created = New-ClickUpTask -ListId $List -Name $Name -Description $Description `
                               -Parent $Parent -Status $Status `
                               -Assignees (Resolve-ClickUpAssignees $Assignee) `
                               -Priority $Priority -DueDate $Due -StartDate $Start `
                               -TimeEstimate $Estimate -Tags $Tag
    Write-Host ("created  {0}  {1}" -f $created.id, $Name)
    Write-Host ("         {0}" -f (Get-Prop $created 'url' ''))
    return 0
}


function Invoke-Set {
    if (-not $Target) { Write-Host 'set requires a task id'; return 1 }

    $fields = New-ClickUpTaskBody -Name $Name -Status $Status `
        -Priority $Priority -DueDate $Due -StartDate $Start `
        -TimeEstimate $Estimate -Tags $Tag
    if ($script:Bound.ContainsKey('Description')) {
        $fields['markdown_content'] = $Description
    }
    if ($Assignee -and $Assignee.Count -gt 0) {
        # Update takes add/rem rather than a flat list.
        $fields['assignees'] = @{ add = @(Resolve-ClickUpAssignees $Assignee) }
    }
    if ($fields.Count -eq 0) {
        Write-Host ('Nothing to change. Pass -Status, -Name, -Description, ' +
                    '-Priority, -Due, -Start, -Estimate, -Tag, or -Assignee.')
        return 1
    }
    if ($List -and -not (Test-Status -ListId $List -Wanted @($Status))) { return 1 }

    Write-Host ("  ~ {0} -> {1}" -f $Target, ((@($fields.Keys | Sort-Object) |
        ForEach-Object { "$_=$(Format-FieldValue $fields[$_])" }) -join ', '))

    if ($DryRun) {
        Write-Host "`nDry run. Drop -DryRun to write."
        return 0
    }

    [void](Set-ClickUpTask -TaskId $Target -Fields $fields)
    Write-Host ("updated  {0}" -f $Target)
    return 0
}


function Invoke-Apply {
    if (-not $Target) { Write-Host 'apply requires a spec path'; return 1 }
    if (-not (Test-Path $Target)) { Write-Host "no such spec: $Target"; return 1 }

    $spec = Get-Content -Path $Target -Raw -Encoding UTF8 | ConvertFrom-Json
    $listId = [string](Get-Prop $spec 'list_id')
    if (-not $listId) { Write-Host 'spec is missing required key: list_id'; return 1 }
    $specTasks = @(Get-Prop $spec 'tasks' @())
    if ($specTasks.Count -eq 0) { Write-Host 'spec is missing required key: tasks'; return 1 }

    $defaults = Get-Prop $spec 'defaults'
    $statusFixes = @(Get-Prop $spec 'status_fixes' @())

    $roots = @()
    foreach ($task in $specTasks) {
        $roots += ,(New-SpecNode -Spec $task -Defaults $defaults)
    }
    # A single-node spec unrolls to a bare hashtable, whose .Count is its key
    # count, not 1. Force an array.
    $nodes = @(Get-NodeList -Nodes $roots)

    $wanted = @($nodes | ForEach-Object { $_.Status })
    $wanted += @($statusFixes | ForEach-Object { $_.status })
    if (-not (Test-Status -ListId $listId -Wanted $wanted)) { return 1 }

    $board = Read-Board -ListId $listId
    foreach ($node in $nodes) {
        if ($board.Names.ContainsKey($node.Name)) {
            $node.ExistingId = $board.Names[$node.Name]
        }
    }

    # A fix already satisfied is a wasted call on every future run.
    $pendingFixes = @($statusFixes | Where-Object {
        -not ($board.Statuses.ContainsKey($_.task_id) -and
              $board.Statuses[$_.task_id] -eq $_.status)
    })

    # Convergence: an existing task whose declared fields no longer match.
    $updates = @()
    if ($Reconcile) {
        foreach ($node in $nodes) {
            if (-not $node.ExistingId) { continue }
            $wantedBody = New-ClickUpTaskBody -Status $node.Status `
                -Priority $node.Priority -DueDate $node.DueDate `
                -StartDate $node.StartDate -TimeEstimate $node.TimeEstimate `
                -Tags $node.Tags
            $changed = Get-ClickUpDrift -Current $board.ById[$node.ExistingId] `
                                        -Wanted $wantedBody
            if ($changed.Count -gt 0) {
                $updates += ,@{ Node = $node; Changed = $changed }
            }
        }
    }

    $missing = @($nodes | Where-Object { -not $_.ExistingId })
    # Both kinds of write count as an update: a reconcile fixing drift, and a
    # status fix. They are applied by the same call and printed the same way.
    $updating = @($updates).Count + @($pendingFixes).Count
    Write-Host ("list {0}: {1} tasks present" -f $listId, $board.Names.Count)
    Write-Host ("spec: {0} entries, {1} to create, {2} to update`n" -f
                $nodes.Count, $missing.Count, $updating)
    Write-Tree -Nodes $roots
    foreach ($u in $updates) {
        $shown = (@($u.Changed.Keys | Sort-Object) |
                  ForEach-Object { "$_=$(Format-FieldValue $u.Changed[$_])" }) -join ', '
        Write-Host ("  ~ {0} -> {1}" -f $u.Node.Name, $shown)
    }
    foreach ($fix in $pendingFixes) {
        Write-Host ("  ~ {0} -> {1}" -f $fix.task_id, $fix.status)
    }
    $skipped = $statusFixes.Count - $pendingFixes.Count
    if ($skipped -gt 0) {
        Write-Host ("  ({0} status fix(es) already satisfied)" -f $skipped)
    }

    if (-not $Apply) {
        Write-Host "`nDry run. Re-run with -Apply to write."
        return 0
    }

    Write-Host ''
    foreach ($fix in $pendingFixes) {
        [void](Set-ClickUpTask -TaskId $fix.task_id -Fields @{ status = $fix.status })
        Write-Host ("updated  {0} -> {1}" -f $fix.task_id, $fix.status)
    }

    foreach ($u in $updates) {
        [void](Set-ClickUpTask -TaskId $u.Node.ExistingId -Fields $u.Changed)
        Write-Host ("updated  {0}  {1}" -f $u.Node.ExistingId, $u.Node.Name)
    }

    # Parents first: a child needs its parent's id, which may not exist until
    # this run creates it. Get-NodeList is depth-first, so order is guaranteed.
    foreach ($node in $nodes) {
        if ($node.ExistingId) { continue }
        $created = New-ClickUpTask -ListId $listId -Name $node.Name `
                                   -Description $node.Description `
                                   -Parent (Get-NodeParentId $node) `
                                   -Status $node.Status `
                                   -Assignees (Resolve-ClickUpAssignees $node.Assignees) `
                                   -Priority $node.Priority -DueDate $node.DueDate `
                                   -StartDate $node.StartDate `
                                   -TimeEstimate $node.TimeEstimate -Tags $node.Tags
        $node.CreatedId = $created.id
        Write-Host ("created  {0}  {1}" -f $created.id, $node.Name)
    }

    Write-Host ("`nDone. {0} created, {1} updated, {2} API calls." -f
                $missing.Count, $updating, (Get-ClickUpCallCount))
    return 0
}


function Invoke-AddFolder {
    <#.SYNOPSIS Create a folder in a space, unless one of that name is already there.#>
    if (-not $Space) { Write-Host 'add-folder requires -Space'; return 1 }
    if (-not $Name)  { Write-Host 'add-folder requires -Name';  return 1 }

    $existing = @(Get-ClickUpFolders -SpaceId $Space | Where-Object { $_.name -eq $Name })
    if ($existing.Count -gt 0) {
        Write-Host ("exists   {0}  {1}" -f $existing[0].id, $Name)
        return 0
    }

    Write-Host ("  + folder {0}  (in space {1})" -f $Name, $Space)
    if ($DryRun) { Write-Host "`nDry run. Drop -DryRun to write."; return 0 }

    $created = New-ClickUpFolder -SpaceId $Space -Name $Name
    Write-Host ("created  {0}  {1}" -f $created.id, $Name)
    return 0
}


function Invoke-AddList {
    <#.SYNOPSIS Create a list in a folder or directly in a space.#>
    if (-not $Name) { Write-Host 'add-list requires -Name'; return 1 }
    if ([bool]$Folder -eq [bool]$Space) {
        Write-Host 'add-list needs exactly one container: -Folder or -Space.'
        return 1
    }

    if ($Folder) {
        $siblings = @(Get-ClickUpFolderLists -FolderId $Folder)
        $where = "folder $Folder"
    } else {
        $siblings = @(Get-ClickUpFolderlessLists -SpaceId $Space)
        $where = "space $Space"
    }

    $existing = @($siblings | Where-Object { $_.name -eq $Name })
    if ($existing.Count -gt 0) {
        Write-Host ("exists   {0}  {1}" -f $existing[0].id, $Name)
        return 0
    }

    Write-Host ("  + list {0}  (in {1})" -f $Name, $where)
    if ($DryRun) { Write-Host "`nDry run. Drop -DryRun to write."; return 0 }

    $created = New-ClickUpList -Name $Name -FolderId $Folder -SpaceId $Space -Content $Content
    Write-Host ("created  {0}  {1}" -f $created.id, $Name)
    Write-Host 'Put this id in a spec''s list_id to fill it.'
    return 0
}


function Invoke-Delete {
    <#.SYNOPSIS Remove a task. Refuses without -Yes: nothing else here destroys data.#>
    if (-not $Target) { Write-Host 'delete requires a task id'; return 1 }
    $task = Get-ClickUpTask -TaskId $Target
    $kids = @(Get-Prop $task 'subtasks' @())
    Write-Host ("  - {0}  {1}" -f $task.id, $task.name)
    if ($kids.Count -gt 0) {
        Write-Host ("    and its {0} subtask(s):" -f $kids.Count)
        foreach ($kid in $kids) {
            Write-Host ("      {0,-12} {1}" -f $kid.id, $kid.name)
        }
    }

    if (-not $Yes) {
        Write-Host "`nRefusing to delete without -Yes."
        return 1
    }

    [void](Remove-ClickUpTask -TaskId $Target)
    Write-Host ("deleted  {0}" -f $Target)
    Write-Host 'ClickUp keeps it in the workspace trash for 30 days.'
    return 0
}


# ---------------------------------------------------------------------------
# commits: git history as task comments
# ---------------------------------------------------------------------------
#
# ClickUp's API has no endpoint for the integration panel's commit links, so
# commits are recorded as a comment of web links instead. The full SHA in the
# comment text is the idempotency key: a commit whose SHA already appears in
# any comment on the task is never posted again.
#
# A request that cannot go ahead throws 'COMMITS:<message>'; Invoke-Commits
# prints the message and exits 1, with nothing written.

$script:ShortSha = 8
# https://host/org/repo, ssh://git@host:22/org/repo, user@host:org/repo.
$script:UrlRemote = '^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::[0-9]+)?/(.+)$'
$script:ScpRemote = '^(?:[^@/]+@)?([^:/]+):(.+)$'


function Stop-Commits {
    param([string]$Message)
    throw ('COMMITS:' + $Message)
}


function Get-CommitsMessage {
    <#.SYNOPSIS The message of a Stop-Commits error, or $null for any other.#>
    param($ErrorRecord)
    $message = $ErrorRecord.Exception.Message
    if ($message -like 'COMMITS:*') { return $message.Substring(8) }
    return $null
}


function ConvertTo-ProcessArgument {
    <#.SYNOPSIS Quote one argument for ProcessStartInfo.Arguments.#>
    param([string]$Value)
    if ($Value -ne '' -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}


function Invoke-Git {
    <#
    .SYNOPSIS
        Run git against a repository; its first stderr line becomes the error.
    .DESCRIPTION
        A Process rather than `& git`: Windows PowerShell 5.1 turns a native
        command's stderr into an error record, which under
        ErrorActionPreference Stop throws before the exit code can be read,
        and decodes stdout with the console code page rather than UTF-8.
    #>
    param([string]$RepoPath, [string[]]$Arguments)
    $all = @('-C', $RepoPath) + $Arguments
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $psi.Arguments = (@($all | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' ')
    # Set-Location does not move the process directory; a relative -Repo
    # must resolve against the one the user is looking at.
    $psi.WorkingDirectory = (Get-Location).ProviderPath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8

    $process = $null
    try { $process = [System.Diagnostics.Process]::Start($psi) } catch { }
    if ($null -eq $process) { Stop-Commits 'git is not on PATH.' }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $out = $stdout.Result

    if ($process.ExitCode -ne 0) {
        $lines = @($stderr -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        if ($lines.Count -gt 0) { Stop-Commits $lines[0] }
        Stop-Commits ("git exited {0}" -f $process.ExitCode)
    }
    return $out
}


function Resolve-Commits {
    <#
    .SYNOPSIS
        @{Sha; Subject; Date} oldest first, ranges in the order given, no repeats.
    .DESCRIPTION
        Date is the author date, YYYY-MM-DD, read in the same git log call.
    #>
    param([string]$RepoPath, [string[]]$Ranges)
    $commits = New-Object System.Collections.ArrayList
    $seen = @{}
    foreach ($rangeText in $Ranges) {
        # A leading dash would reach git as an option, not a revision.
        if (-not $rangeText -or $rangeText.StartsWith('-')) {
            Stop-Commits ("'{0}' is not a revision range." -f $rangeText)
        }
        try {
            # The trailing -- stops git reading a range as a file path.
            $out = Invoke-Git -RepoPath $RepoPath -Arguments @(
                'log', '--reverse', '--no-merges', '--date=short',
                '--format=%H%x09%ad%x09%s', $rangeText, '--')
        } catch {
            $why = Get-CommitsMessage $_
            if ($null -eq $why) { throw }
            Stop-Commits ("git log failed for range '{0}': {1}" -f $rangeText, $why)
        }
        $found = 0
        foreach ($line in ($out -split "`n")) {
            $line = $line.TrimEnd([char]13)
            if (-not $line) { continue }
            $tab = $line.IndexOf("`t")
            $sha = $line
            $date = ''
            $subject = ''
            if ($tab -ge 0) {
                $sha = $line.Substring(0, $tab)
                $rest = $line.Substring($tab + 1)
                $tab = $rest.IndexOf("`t")
                $date = $rest
                if ($tab -ge 0) {
                    $date = $rest.Substring(0, $tab)
                    $subject = $rest.Substring($tab + 1)
                }
            }
            $found++
            if (-not $seen.ContainsKey($sha)) {
                $seen[$sha] = $true
                [void]$commits.Add(@{ Sha = $sha; Subject = $subject; Date = $date })
            }
        }
        if ($found -eq 0) { Stop-Commits ("range '{0}' contains no commits." -f $rangeText) }
    }
    return $commits.ToArray()
}


function ConvertFrom-Remote {
    <#.SYNOPSIS @{HostName; Path} from any remote spelling, or $null.#>
    param([string]$Url)
    $text = $Url.Trim()
    $match = [regex]::Match($text, $script:UrlRemote)
    if (-not $match.Success) { $match = [regex]::Match($text, $script:ScpRemote) }
    if (-not $match.Success) { return $null }
    $path = $match.Groups[2].Value.TrimEnd('/')
    if ($path.EndsWith('.git')) { $path = $path.Substring(0, $path.Length - 4) }
    return @{ HostName = $match.Groups[1].Value.ToLower(); Path = $path }
}


function Get-CommitPath {
    <#.SYNOPSIS The per-commit path segment each forge uses, or $null for an unknown one.#>
    param([string]$HostName)
    if ($HostName -eq 'bitbucket.org') { return '/commits/' }
    if ($HostName -eq 'github.com') { return '/commit/' }
    if ($HostName.Contains('gitlab')) { return '/-/commit/' }
    return $null
}


function Get-WebBase {
    <#.SYNOPSIS @{Base; Style} from -WebUrl or the origin remote.#>
    param([string]$RepoPath, [string]$Url)
    if ($Url) {
        $match = [regex]::Match($Url.Trim(), $script:UrlRemote)
        if (-not $match.Success) {
            Stop-Commits ("-WebUrl '{0}' is not a URL. Use the repository's web address, https://..." -f $Url)
        }
        $base = $Url.Trim().TrimEnd('/')
        if ($base.EndsWith('.git')) { $base = $base.Substring(0, $base.Length - 4) }
        $style = Get-CommitPath $match.Groups[1].Value.ToLower()
        if (-not $style) { $style = '/commit/' }
        return @{ Base = $base; Style = $style }
    }

    $remote = $null
    try {
        $remote = (Invoke-Git -RepoPath $RepoPath -Arguments @('remote', 'get-url', 'origin')).Trim()
    } catch {
        if ($null -eq (Get-CommitsMessage $_)) { throw }
        Stop-Commits "cannot read the 'origin' remote. Pass -WebUrl with the repository's web address."
    }
    $parsed = ConvertFrom-Remote $remote
    if ($null -eq $parsed) {
        Stop-Commits ("cannot read the 'origin' remote '{0}'. Pass -WebUrl with the repository's web address." -f $remote)
    }
    $style = Get-CommitPath $parsed.HostName
    if (-not $style) {
        Stop-Commits ("unknown git host '{0}'. Pass -WebUrl with the repository's web address." -f $parsed.HostName)
    }
    return @{ Base = ('https://{0}/{1}' -f $parsed.HostName, $parsed.Path); Style = $style }
}


function Get-ShortSha {
    param([string]$Sha)
    return $Sha.Substring(0, [Math]::Min($script:ShortSha, $Sha.Length))
}


function New-CommentBody {
    param([string]$CommentTitle, $Commits, $Web)
    $blocks = @($CommentTitle)
    foreach ($commit in $Commits) {
        $blocks += ("{0} {1}`n{2}{3}{4}" -f (Get-ShortSha $commit.Sha), $commit.Subject,
                    $Web.Base, $Web.Style, $commit.Sha)
    }
    return ($blocks -join "`n`n")
}


function Get-LinkPlan {
    <#.SYNOPSIS Read every comment on a task; keep only the commits none of them names.#>
    param([string]$TaskId, $Commits, [string]$CommentTitle, $Web)
    $comments = @(Get-ClickUpComments -TaskId $TaskId)
    $texts = @($comments | ForEach-Object { [string](Get-Prop $_ 'comment_text' '') })
    $new = @()
    foreach ($commit in $Commits) {
        $hit = $false
        foreach ($text in $texts) {
            if ($text.Contains($commit.Sha)) { $hit = $true; break }
        }
        if (-not $hit) { $new += ,$commit }
    }
    $body = ''
    if ($new.Count -gt 0) {
        $body = New-CommentBody -CommentTitle $CommentTitle -Commits $new -Web $Web
    }
    $total = @($Commits).Count
    return @{ Task = $TaskId; Read = $comments.Count; Total = $total; New = $new
              Linked = ($total - $new.Count); Body = $body }
}


function Get-DefaultTitle {
    param([string[]]$Ranges)
    return ('Commits ' + (@($Ranges) -join ', '))
}


function Read-LinkSpec {
    <#.SYNOPSIS @{Repo; WebUrl; Links} from a links spec. Rejects before any read.#>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Commits "no such spec: $Path" }
    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $parsed = $null
    $readable = $false
    if ($text -and $text.Trim()) {
        try { $parsed = $text | ConvertFrom-Json; $readable = $true } catch { }
    }
    if (-not $readable) { Stop-Commits ("cannot read spec: {0} is not valid JSON." -f $Path) }

    $options = @{ Repo = $null; WebUrl = $null }
    # Decided from the text, not the parsed value: PowerShell 7 unrolls a
    # one-element array into the bare object, and 5.1 wraps any array in one.
    $isArray = $text.TrimStart([char]0xFEFF).TrimStart().StartsWith('[')
    $isObject = (-not $isArray) -and
                ($parsed -is [System.Management.Automation.PSCustomObject]) -and
                ($parsed.PSObject.Properties.Name -contains 'links') -and
                ($parsed.links -is [array])
    if ($isObject) {
        foreach ($pair in @(@('repo', 'Repo'), @('web_url', 'WebUrl'))) {
            $value = $null
            if ($parsed.PSObject.Properties.Name -contains $pair[0]) { $value = $parsed.($pair[0]) }
            if ($null -ne $value -and -not ($value -is [string])) {
                Stop-Commits ('spec "{0}" must be text.' -f $pair[0])
            }
            $options[$pair[1]] = $value
        }
        $entries = @($parsed.links)
    } elseif ($isArray) {
        $entries = @()
        if ($null -ne $parsed) { $entries = @($parsed) }
    } else {
        Stop-Commits 'spec must be a JSON array of entries, or an object with a "links" array.'
    }
    if ($entries.Count -eq 0) { Stop-Commits 'spec has no entries.' }

    $links = @()
    # Ordinal: task ids are case-sensitive, and a hashtable's keys are not.
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $number = 0
    foreach ($entry in $entries) {
        $number++
        $where = "spec entry $number"
        if (-not ($entry -is [System.Management.Automation.PSCustomObject])) {
            Stop-Commits "${where}: must be an object."
        }
        $task = Get-Prop $entry 'task'
        if (-not ($task -is [string]) -or -not $task.Trim()) {
            Stop-Commits ('{0}: "task" must be a task id.' -f $where)
        }
        # Read directly: a function returning an array unrolls it, and a
        # one-range list would come back as a bare string.
        $ranges = $null
        if ($entry.PSObject.Properties.Name -contains 'ranges') { $ranges = $entry.ranges }
        $ok = ($ranges -is [array]) -and (@($ranges).Count -gt 0)
        if ($ok) {
            foreach ($r in $ranges) {
                if (-not ($r -is [string]) -or -not $r.Trim()) { $ok = $false }
            }
        }
        if (-not $ok) { Stop-Commits ('{0}: "ranges" must be a non-empty list of ranges.' -f $where) }
        $entryTitle = $null
        if ($entry.PSObject.Properties.Name -contains 'title') { $entryTitle = $entry.title }
        if ($null -ne $entryTitle -and -not ($entryTitle -is [string])) {
            Stop-Commits ('{0}: "title" must be text.' -f $where)
        }
        if (-not $seen.Add($task)) {
            Stop-Commits ("{0}: task {1} appears more than once." -f $where, $task)
        }
        $rangeList = [string[]]@($ranges)
        if (-not $entryTitle) { $entryTitle = Get-DefaultTitle $rangeList }
        $links += ,@{ Task = $task; Ranges = $rangeList; Title = $entryTitle; Commits = @() }
    }
    return @{ Repo = $options.Repo; WebUrl = $options.WebUrl; Links = $links }
}


function Write-Body {
    param([string]$Body)
    foreach ($line in ($Body -split "`n")) { Write-Host ('    ' + $line) }
}


function Get-RangeList {
    <#
    .SYNOPSIS
        -Range as a flat list.
    .DESCRIPTION
        `-File` hands `-Range a..b,c..d` over as one string, where an
        interactive session would make it an array. Splitting on commas makes
        both spellings mean the same thing.
    #>
    return @(@($Range) | ForEach-Object { "$_" -split ',' } | Where-Object { $_ })
}


# -- rollup: one summary comment per parent -----------------------------------
#
# The commit lists stay on the subtasks; the parent (domain) task gets one
# line per subtask instead. A line is keyed on CU-<subtask id> plus the
# subtask's latest full SHA: a parent comment holding both means that line is
# already rolled up. Changing the line format so either stops appearing
# breaks dedupe against every rollup already posted.
#
# The dash and the arrow are built from code points: Windows PowerShell 5.1
# reads a BOM-less script in the ANSI code page, so a literal would garble.

$script:Dash = [string][char]0x2014
$script:Arrow = [string][char]0x2192


function Get-RollupLine {
    param($Line)
    $noun = 'commits'
    if ($Line.Total -eq 1) { $noun = 'commit' }
    return ("{0} (CU-{1}) {2} {3} {4}, {5} {6} {7}`nlatest {8}" -f
            $Line.Name, $Line.Task, $script:Dash, $Line.Total, $noun,
            $Line.First, $script:Arrow, $Line.Last, $Line.Sha)
}


function New-RollupBody {
    param($Lines)
    $through = ''
    foreach ($line in $Lines) {
        if ([string]::CompareOrdinal($line.Last, $through) -gt 0) { $through = $line.Last }
    }
    $blocks = @(("Subtask commits {0} through {1}" -f $script:Dash, $through))
    foreach ($line in $Lines) { $blocks += (Get-RollupLine $line) }
    return ($blocks -join "`n`n")
}


function Test-RolledUp {
    <#
    .SYNOPSIS
        Whether one parent comment already carries this subtask's line.
    .DESCRIPTION
        The id must end where CU-<id> ends, or CU-abc would count as CU-abcd.
    #>
    param([string]$Text, $Line)
    if (-not $Text.Contains($Line.Sha)) { return $false }
    $marker = [regex]::Escape('CU-' + $Line.Task) + '(?![A-Za-z0-9])'
    return [regex]::IsMatch($Text, $marker)
}


function Get-RollupPlans {
    <#
    .SYNOPSIS
        @{Plans (one per parent, first-seen order); Skipped (top-level ids)}.
    .DESCRIPTION
        Every task is read for its parent and name, and every parent's
        comments to the end, before anything is written.
    #>
    param($Links)
    # Ordinal: task ids are case-sensitive, and a hashtable's keys are not.
    $tasks = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    $groups = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
    $order = New-Object System.Collections.ArrayList
    $skipped = @()
    foreach ($link in $Links) {
        $taskId = [string]$link.Task
        if (-not $tasks.ContainsKey($taskId)) {
            $tasks[$taskId] = Get-ClickUpTask -TaskId $taskId -Subtasks $false
        }
        $task = $tasks[$taskId]
        $parent = Get-Prop $task 'parent'
        if (-not $parent) { $skipped += $taskId; continue }
        $parent = [string]$parent
        if (-not $groups.ContainsKey($parent)) {
            $groups[$parent] = New-Object 'System.Collections.Generic.List[object]'
            [void]$order.Add($parent)
        }
        $commits = @($link.Commits)
        $latest = $commits[$commits.Count - 1]
        $groups[$parent].Add(@{
            Task = $taskId; Name = [string](Get-Prop $task 'name' '')
            Total = $commits.Count; First = $commits[0].Date
            Last = $latest.Date; Sha = $latest.Sha
        })
    }

    $plans = @()
    foreach ($parent in $order) {
        $lines = $groups[$parent]
        $lines.Sort([Comparison[object]]{
            param($x, $y)
            $c = [string]::CompareOrdinal($x.Last, $y.Last)
            if ($c -eq 0) { $c = [string]::CompareOrdinal($x.Name, $y.Name) }
            if ($c -eq 0) { $c = [string]::CompareOrdinal($x.Task, $y.Task) }
            return $c
        })
        $comments = @(Get-ClickUpComments -TaskId $parent)
        $texts = @($comments | ForEach-Object { [string](Get-Prop $_ 'comment_text' '') })
        $new = @()
        foreach ($line in $lines) {
            $hit = $false
            foreach ($text in $texts) {
                if (Test-RolledUp -Text $text -Line $line) { $hit = $true; break }
            }
            if (-not $hit) { $new += ,$line }
        }
        $body = ''
        if ($new.Count -gt 0) { $body = New-RollupBody $new }
        $plans += ,@{ Parent = $parent; Read = $comments.Count; Lines = $lines.ToArray()
                      New = $new; Body = $body }
    }
    return @{ Plans = $plans; Skipped = $skipped }
}


function Write-Rollups {
    param($Plan)
    foreach ($entry in $Plan.Plans) {
        $lineCount = @($entry.Lines).Count
        $newCount = @($entry.New).Count
        Write-Host ("`nrollup {0}: {1} comment(s) read, {2} subtask(s), {3} new, {4} already rolled up" -f
                    $entry.Parent, $entry.Read, $lineCount, $newCount, ($lineCount - $newCount))
        foreach ($line in $entry.Lines) {
            $mark = '='
            if (@($entry.New) -contains $line) { $mark = '+' }
            Write-Host ("  {0} {1}  {2}" -f $mark, $line.Task, $line.Name)
        }
        if ($newCount -gt 0) {
            Write-Host ''
            Write-Body $entry.Body
        }
    }
    foreach ($taskId in $Plan.Skipped) {
        Write-Host ("`nrollup skipped: {0} has no parent" -f $taskId)
    }
}


function Send-Rollups {
    <#.SYNOPSIS Post each parent's comment; Lead goes before the first line printed.#>
    param($Plan, [string]$Lead)
    $count = 0
    foreach ($entry in $Plan.Plans) {
        if (@($entry.New).Count -eq 0) { continue }
        $posted = Add-ClickUpComment -TaskId $entry.Parent -Text $entry.Body -NotifyAll $false
        Write-Host ("{0}rolled up  {1}  {2} subtask(s)  (comment {3})" -f
                    $Lead, $entry.Parent, @($entry.New).Count, (Get-Prop $posted 'id' ''))
        $Lead = ''
        $count++
    }
    return $count
}


function Invoke-CommitsSingle {
    $ranges = @(Get-RangeList)
    if ($ranges.Count -eq 0) {
        Write-Host 'commits needs at least one -Range, e.g. -Range v1.2.0..v1.3.0'
        return 1
    }
    if ($Apply) {
        Write-Host '-Apply is for -Spec. A single task posts unless -DryRun.'
        return 1
    }
    $repoPath = '.'
    if ($Repo) { $repoPath = $Repo }
    $commits = @(Resolve-Commits -RepoPath $repoPath -Ranges $ranges)
    $web = Get-WebBase -RepoPath $repoPath -Url $WebUrl
    $commentTitle = $Title
    if (-not $commentTitle) { $commentTitle = Get-DefaultTitle $ranges }
    $plan = Get-LinkPlan -TaskId $Target -Commits $commits -CommentTitle $commentTitle -Web $web
    $rollups = @{ Plans = @(); Skipped = @() }
    if ($Rollup) {
        $rollups = Get-RollupPlans -Links @(,@{ Task = $Target; Commits = $commits })
    }

    Write-Host ("task {0}: {1} comment(s) read, {2} commit(s), {3} new, {4} already linked" -f
                $plan.Task, $plan.Read, $plan.Total, $plan.New.Count, $plan.Linked)
    if ($plan.New.Count -eq 0 -and -not $Rollup) {
        Write-Host 'Nothing to do. Every commit is already linked.'
        return 0
    }
    if ($plan.New.Count -gt 0) {
        Write-Host ''
        Write-Body $plan.Body
    } else {
        Write-Host 'Every commit is already linked.'
    }
    if ($Rollup) {
        Write-Rollups -Plan $rollups
        $pending = @($rollups.Plans | Where-Object { @($_.New).Count -gt 0 })
        if ($plan.New.Count -eq 0 -and $pending.Count -eq 0) {
            Write-Host "`nNothing to do. Every commit is already linked and rolled up."
            return 0
        }
    }

    if ($DryRun) {
        Write-Host "`nDry run. Drop -DryRun to write."
        return 0
    }

    $lead = "`n"
    if ($plan.New.Count -gt 0) {
        $posted = Add-ClickUpComment -TaskId $plan.Task -Text $plan.Body -NotifyAll $false
        Write-Host ("`ncommented  {0}  {1} commit(s)  (comment {2})" -f
                    $plan.Task, $plan.New.Count, (Get-Prop $posted 'id' ''))
        $lead = ''
    }
    [void](Send-Rollups -Plan $rollups -Lead $lead)
    return 0
}


function Invoke-CommitsSpec {
    if ($Target -or @(Get-RangeList).Count -gt 0 -or $Title) {
        Write-Host ('-Spec takes the task, -Range and -Title from the file. ' +
                    'Pass a task id or -Spec, not both.')
        return 1
    }
    if ($DryRun) {
        Write-Host '-Spec is a dry run unless -Apply. Drop -DryRun.'
        return 1
    }
    $loaded = Read-LinkSpec -Path $Spec
    $repoPath = '.'
    if ($Repo) { $repoPath = $Repo } elseif ($loaded.Repo) { $repoPath = $loaded.Repo }
    $url = $WebUrl
    if (-not $url) { $url = $loaded.WebUrl }
    $web = Get-WebBase -RepoPath $repoPath -Url $url

    # Every range resolves before any read, and every read before any write:
    # a bad entry anywhere stops the run with the board untouched.
    foreach ($link in $loaded.Links) {
        try {
            $link.Commits = @(Resolve-Commits -RepoPath $repoPath -Ranges $link.Ranges)
        } catch {
            $why = Get-CommitsMessage $_
            if ($null -eq $why) { throw }
            Stop-Commits ("task {0}: {1}" -f $link.Task, $why)
        }
    }
    $plans = @()
    foreach ($link in $loaded.Links) {
        $plans += ,(Get-LinkPlan -TaskId $link.Task -Commits $link.Commits `
                                 -CommentTitle $link.Title -Web $web)
    }
    $rollups = @{ Plans = @(); Skipped = @() }
    if ($Rollup) { $rollups = Get-RollupPlans -Links $loaded.Links }
    $rolling = @($rollups.Plans | Where-Object { @($_.New).Count -gt 0 })

    $posting = @($plans | Where-Object { $_.New.Count -gt 0 })
    $tally = ("spec: {0} task(s), {1} comment(s) to post, {2} task(s) already linked" -f
              $plans.Count, $posting.Count, ($plans.Count - $posting.Count))
    if ($Rollup) { $tally += (", {0} rollup comment(s) to post" -f $rolling.Count) }
    Write-Host ($tally + "`n")
    foreach ($plan in $plans) {
        $mark = '='
        if ($plan.New.Count -gt 0) { $mark = '+' }
        Write-Host ("  {0} {1}  {2} new, {3} already linked" -f
                    $mark, $plan.Task, $plan.New.Count, $plan.Linked)
        foreach ($commit in $plan.New) {
            Write-Host ("      {0} {1}" -f (Get-ShortSha $commit.Sha), $commit.Subject)
        }
    }
    if ($Rollup) { Write-Rollups -Plan $rollups }

    if (-not $Apply) {
        Write-Host "`nDry run. Re-run with -Apply to write."
        return 0
    }

    if ($posting.Count -gt 0 -or $rolling.Count -gt 0) { Write-Host '' }
    foreach ($plan in $posting) {
        $posted = Add-ClickUpComment -TaskId $plan.Task -Text $plan.Body -NotifyAll $false
        Write-Host ("commented  {0}  {1} commit(s)  (comment {2})" -f
                    $plan.Task, $plan.New.Count, (Get-Prop $posted 'id' ''))
    }
    if (-not $Rollup) {
        Write-Host ("`nDone. {0} posted, {1} API calls." -f $posting.Count, (Get-ClickUpCallCount))
        return 0
    }
    $rolled = Send-Rollups -Plan $rollups -Lead ''
    Write-Host ("`nDone. {0} posted, {1} rollup comment(s) posted, {2} API calls." -f
                $posting.Count, $rolled, (Get-ClickUpCallCount))
    return 0
}


function Invoke-Commits {
    <#.SYNOPSIS Post one comment of commit links per task, skipping commits already linked.#>
    try {
        if ($Spec) { return (Invoke-CommitsSpec) }
        if (-not $Target) {
            Write-Host 'commits needs a task id, or -Spec for several tasks.'
            return 1
        }
        return (Invoke-CommitsSingle)
    } catch {
        $why = Get-CommitsMessage $_
        if ($null -eq $why) { throw }
        Write-Host $why
        return 1
    }
}


function Invoke-Show {
    if (-not $List) { Write-Host 'show requires -List'; return 1 }
    foreach ($task in (Get-ClickUpTasks -ListId $List | Sort-Object name)) {
        $parent = if (Get-Prop $task 'parent') { "  parent=$($task.parent)" } else { '' }
        Write-Host ("{0}  {1,-12} {2}{3}" -f $task.id, $task.status.status,
                    $task.name, $parent)
    }
    return 0
}


function Invoke-Statuses {
    if (-not $List) { Write-Host 'statuses requires -List'; return 1 }
    Get-ClickUpStatuses -ListId $List | ForEach-Object { Write-Host $_ }
    return 0
}


# ---------------------------------------------------------------------------

try {
    switch ($Command) {
        'add'      { exit (Invoke-Add) }
        'set'      { exit (Invoke-Set) }
        'apply'    { exit (Invoke-Apply) }
        'delete'   { exit (Invoke-Delete) }
        'add-folder' { exit (Invoke-AddFolder) }
        'add-list'   { exit (Invoke-AddList) }
        'show'     { exit (Invoke-Show) }
        'statuses' { exit (Invoke-Statuses) }
        'commits'  { exit (Invoke-Commits) }
    }
} catch {
    $message = $_.Exception.Message
    if ($message -like 'CLICKUP_RATE_LIMIT:*') {
        $seconds = [double]($message -replace '^CLICKUP_RATE_LIMIT:', '')
        Write-Host ("`nRate limited with work outstanding. Retry in {0:N0} minutes." -f
                    ($seconds / 60))
        Write-Host 'Re-running is safe: what already landed will be skipped.'
        exit 2
    }
    [Console]::Error.WriteLine($message)
    exit 1
}

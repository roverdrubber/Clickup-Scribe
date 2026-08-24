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
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('add', 'set', 'apply', 'delete', 'add-folder', 'add-list',
                 'show', 'statuses')]
    [string]$Command,

    # Task id for `set`, spec path for `apply`.
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
    [switch]$AllowDuplicate,
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$Reconcile,
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
    Write-Host ("list {0}: {1} tasks present" -f $listId, $board.Names.Count)
    Write-Host ("spec: {0} entries, {1} to create, {2} to update`n" -f
                $nodes.Count, $missing.Count, @($updates).Count)
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
                $missing.Count, @($updates).Count, (Get-ClickUpCallCount))
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

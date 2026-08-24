<#
.SYNOPSIS
    Read a ClickUp workspace over REST: spaces, folders, lists, tasks, subtasks.

.DESCRIPTION
    The PowerShell half of the clickup-read skill, behaviourally identical to
    reader.py. Targets Windows PowerShell 5.1 and later.

    A deep traversal is many calls. Over the MCP server that exhausts a daily
    quota in one go; over REST the same walk costs a minute of a per-minute
    budget.

.EXAMPLE
    .\reader.ps1 tree                              # whole workspace structure
    .\reader.ps1 tree -Space 90161372882           # one space
    .\reader.ps1 tasks -List 901615936216          # tasks, subtasks nested
    .\reader.ps1 get 86d3qqa1e                     # one task in full
    .\reader.ps1 find payroll -Space 90161372882
    .\reader.ps1 export -List 901615936216 -AsSpec > spec.json
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('tree', 'tasks', 'get', 'find', 'members', 'export')]
    [string]$Command,

    # Task id for `get`, keyword for `find`.
    [Parameter(Position = 1)]
    [string]$Target,

    [string]$List,
    [string]$Space,
    [string]$Team,
    [switch]$SpacesOnly,
    [switch]$OpenOnly,
    [switch]$AsSpec,
    [switch]$Json
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# Commands emit data on the pipeline (so `> file.json` captures it) and set
# this on failure. The dispatch must not wrap calls in `exit (...)`, which
# would consume that pipeline output as the exit code.
$script:Code = 0

Import-Module (Join-Path $PSScriptRoot 'ClickUp.psm1') -Force


function Get-Prop {
    <#
    .SYNOPSIS
        Read a property that may be absent, from JSON objects or hashtables.
    .DESCRIPTION
        PSObject.Properties works for the PSCustomObjects ConvertFrom-Json
        produces, but on a hashtable it reports Count/Keys/Values rather than
        the keys themselves. Both shapes flow through here, so both are handled.
    #>
    param($Object, [string]$PropertyName, $Default = $null)
    if ($null -eq $Object) { return $Default }

    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($PropertyName)) {
            $value = $Object[$PropertyName]
            if ($null -ne $value) { return $value }
        }
        return $Default
    }

    if ($Object.PSObject.Properties.Name -contains $PropertyName) {
        $value = $Object.$PropertyName
        if ($null -ne $value) { return $value }
    }
    return $Default
}


# ---------------------------------------------------------------------------
# hierarchy
# ---------------------------------------------------------------------------

function Get-SpaceNode {
    param($SpaceObject, [bool]$WithLists = $true)

    $node = [ordered]@{
        id = $SpaceObject.id; name = $SpaceObject.name; type = 'space'
        folders = @(); lists = @()
    }
    if (-not $WithLists) { return $node }

    foreach ($folder in (Get-ClickUpFolders -SpaceId $SpaceObject.id)) {
        $lists = @()
        foreach ($l in (Get-ClickUpFolderLists -FolderId $folder.id)) {
            $lists += ,([ordered]@{ id = $l.id; name = $l.name; type = 'list'
                                    task_count = (Get-Prop $l 'task_count') })
        }
        $node.folders += ,([ordered]@{ id = $folder.id; name = $folder.name
                                       type = 'folder'; lists = $lists })
    }

    # Easy to forget, and silently lossy when forgotten.
    foreach ($l in (Get-ClickUpFolderlessLists -SpaceId $SpaceObject.id)) {
        $node.lists += ,([ordered]@{ id = $l.id; name = $l.name; type = 'list'
                                     task_count = (Get-Prop $l 'task_count') })
    }
    return $node
}


function Get-Tree {
    param([string]$SpaceId, [string]$TeamId, [bool]$WithLists = $true)

    $teams = @()
    if ($TeamId) { $teams = @([pscustomobject]@{ id = $TeamId; name = $TeamId }) }
    else { $teams = Get-ClickUpTeams }

    if ($SpaceId) {
        foreach ($team in $teams) {
            foreach ($space in (Get-ClickUpSpaces -TeamId $team.id)) {
                if ($space.id -eq $SpaceId) {
                    return @((Get-SpaceNode -SpaceObject $space -WithLists $WithLists))
                }
            }
        }
        throw "space $SpaceId not found in any workspace"
    }

    $out = @()
    foreach ($team in $teams) {
        $spaces = @()
        foreach ($space in (Get-ClickUpSpaces -TeamId $team.id)) {
            $spaces += ,(Get-SpaceNode -SpaceObject $space -WithLists $WithLists)
        }
        $out += ,([ordered]@{ id = $team.id; name = (Get-Prop $team 'name' $team.id)
                              type = 'workspace'; spaces = $spaces })
    }
    return $out
}


function Write-TreeNode {
    param($Nodes, [int]$Indent = 0)
    foreach ($node in $Nodes) {
        $count = Get-Prop $node 'task_count'
        $suffix = ''
        if ($null -ne $count) { $suffix = "  [$count tasks]" }
        Write-Host ("{0}{1,-9} {2,-14} {3}{4}" -f (' ' * $Indent), $node.type,
                    $node.id, $node.name, $suffix)
        foreach ($key in @('spaces', 'folders', 'lists')) {
            $child = Get-Prop $node $key @()
            if ($child) { Write-TreeNode -Nodes $child -Indent ($Indent + 2) }
        }
    }
}


function Get-ListPairs {
    <#.SYNOPSIS Every list id under a tree, with a "space / folder / list" label.#>
    param($Nodes)
    $found = New-Object System.Collections.ArrayList

    function Visit-Space($space) {
        foreach ($l in (Get-Prop $space 'lists' @())) {
            [void]$found.Add(@{ Id = $l.id; Label = "$($space.name) / $($l.name)" })
        }
        foreach ($f in (Get-Prop $space 'folders' @())) {
            foreach ($l in (Get-Prop $f 'lists' @())) {
                [void]$found.Add(@{ Id = $l.id
                    Label = "$($space.name) / $($f.name) / $($l.name)" })
            }
        }
    }

    foreach ($node in $Nodes) {
        if ((Get-Prop $node 'type') -eq 'workspace') {
            foreach ($space in (Get-Prop $node 'spaces' @())) { Visit-Space $space }
        } else {
            Visit-Space $node
        }
    }
    return $found.ToArray()
}


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

function Get-NestedTasks {
    <#
    .SYNOPSIS
        Turn a flat task list into parent/child trees.
    .DESCRIPTION
        ClickUp returns subtasks flat, each carrying a `parent` id. A subtask
        whose parent is not in the same response is surfaced at top level
        rather than dropped.
    #>
    param($Tasks)

    $byId = @{}
    foreach ($task in $Tasks) {
        $byId[$task.id] = [ordered]@{
            id = $task.id; name = $task.name
            status = (Get-Prop (Get-Prop $task 'status') 'status' '?')
            parent = (Get-Prop $task 'parent')
            assignees = @((Get-Prop $task 'assignees' @()) |
                          ForEach-Object { Get-Prop $_ 'username' '' })
            description = (Get-Prop $task 'description' '')
            children = @()
        }
    }

    $roots = New-Object System.Collections.ArrayList
    foreach ($id in $byId.Keys) {
        $node = $byId[$id]
        $parentId = $node.parent
        if ($parentId -and $byId.ContainsKey($parentId)) {
            $byId[$parentId].children += ,$node
        } else {
            [void]$roots.Add($node)
        }
    }
    # Sort at every level: hash iteration order is not stable, and unordered
    # output would make the parity check flap.
    function Sort-Kids($nodes) {
        $sorted = @($nodes | Sort-Object { $_.name })
        foreach ($n in $sorted) {
            if ($n.children -and @($n.children).Count -gt 0) {
                $n.children = Sort-Kids $n.children
            }
        }
        return $sorted
    }
    return Sort-Kids $roots.ToArray()
}


function Write-TaskNode {
    param($Nodes, [int]$Indent = 0)
    foreach ($node in $Nodes) {
        $who = ''
        $names = @($node.assignees | Where-Object { $_ })
        if ($names.Count -gt 0) { $who = "  ($($names -join ', '))" }
        Write-Host ("  {0}{1,-12} {2,-12} {3}{4}" -f (' ' * $Indent), $node.id,
                    $node.status, $node.name, $who)
        if ($node.children) { Write-TaskNode -Nodes $node.children -Indent ($Indent + 2) }
    }
}


function ConvertTo-Spec {
    param([string]$ListId, $Roots)

    function Convert-Node($task) {
        $entry = [ordered]@{ name = $task.name }
        if ($task.description) { $entry['description'] = $task.description.Trim() }
        if ($task.status -and $task.status -ne '?') { $entry['status'] = $task.status }
        if ($task.children -and @($task.children).Count -gt 0) {
            $entry['children'] = @($task.children | ForEach-Object { Convert-Node $_ })
        }
        return $entry
    }

    return [ordered]@{
        list_id = $ListId
        tasks   = @($Roots | ForEach-Object { Convert-Node $_ })
    }
}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

function Invoke-Tree {
    $tree = Get-Tree -SpaceId $Space -TeamId $Team -WithLists (-not $SpacesOnly)
    if ($Json) { $tree | ConvertTo-Json -Depth 12; return }
    Write-TreeNode -Nodes $tree
    Write-Host ("`n{0} API calls." -f (Get-ClickUpCallCount))
}


function Invoke-Tasks {
    if (-not $List) { Write-Host 'tasks requires -List'; $script:Code = 1; return }
    $tasks = Get-ClickUpTasks -ListId $List -IncludeClosed (-not $OpenOnly)
    $roots = Get-NestedTasks -Tasks $tasks
    if ($Json) { $roots | ConvertTo-Json -Depth 12; return }
    Write-TaskNode -Nodes $roots
    Write-Host ("`n{0} tasks ({1} top level), {2} API calls." -f
                @($tasks).Count, @($roots).Count, (Get-ClickUpCallCount))
}


function Invoke-Get {
    if (-not $Target) { Write-Host 'get requires a task id'; $script:Code = 1; return }
    $task = Get-ClickUpTask -TaskId $Target
    if ($Json) { $task | ConvertTo-Json -Depth 12; return }

    Write-Host ("{0}  {1}" -f $task.id, $task.name)
    Write-Host ("  status    {0}" -f (Get-Prop (Get-Prop $task 'status') 'status'))
    $lst = Get-Prop $task 'list'
    Write-Host ("  list      {0} ({1})" -f (Get-Prop $lst 'name'), (Get-Prop $lst 'id'))
    if (Get-Prop $task 'parent') { Write-Host ("  parent    {0}" -f $task.parent) }
    $who = @((Get-Prop $task 'assignees' @()) | ForEach-Object { Get-Prop $_ 'username' '' })
    if ($who.Count -gt 0) { Write-Host ("  assignees {0}" -f ($who -join ', ')) }
    $desc = Get-Prop $task 'description' ''
    if ($desc) { Write-Host ("  description`n    {0}" -f $desc.Trim()) }
    $subs = @(Get-Prop $task 'subtasks' @())
    if ($subs.Count -gt 0) {
        Write-Host ("  subtasks  ({0})" -f $subs.Count)
        foreach ($sub in $subs) {
            Write-Host ("    {0,-12} {1,-12} {2}" -f $sub.id,
                        (Get-Prop (Get-Prop $sub 'status') 'status' '?'), $sub.name)
        }
    }
}


function Invoke-Find {
    <#.SYNOPSIS Substring match on task names. REST v2 has no full-text search.#>
    if (-not $Target) { Write-Host 'find requires a keyword'; $script:Code = 1; return }
    $needle = $Target.ToLower()

    $pairs = @()
    if ($List) {
        $name = Get-Prop (Get-ClickUpList -ListId $List) 'name' $List
        $pairs = @(@{ Id = $List; Label = $name })
    } else {
        $pairs = Get-ListPairs -Nodes (Get-Tree -SpaceId $Space -TeamId $Team)
    }

    $hits = New-Object System.Collections.ArrayList
    foreach ($pair in $pairs) {
        foreach ($task in (Get-ClickUpTasks -ListId $pair.Id)) {
            if ($task.name.ToLower().Contains($needle)) {
                [void]$hits.Add([ordered]@{ id = $task.id; name = $task.name
                    status = (Get-Prop (Get-Prop $task 'status') 'status')
                    location = $pair.Label })
            }
        }
    }

    if ($Json) { $hits.ToArray() | ConvertTo-Json -Depth 6; return }
    foreach ($hit in $hits) {
        Write-Host ("{0,-12} {1,-12} {2}" -f $hit.id, $hit.status, $hit.name)
        Write-Host ("{0,-12} {1,-12} {2}" -f '', '', $hit.location)
    }
    Write-Host ("`n{0} match(es) across {1} list(s), {2} API calls." -f
                $hits.Count, @($pairs).Count, (Get-ClickUpCallCount))
}


function Invoke-Members {
    <#.SYNOPSIS Numeric ids for `scribe -Assignee` and a spec's `assignees`.#>
    $people = Get-ClickUpMembers
    if ($Json) { $people | ConvertTo-Json -Depth 6; return }
    foreach ($person in $people) {
        Write-Host ("{0,-12} {1,-28} {2}" -f $person.id, $person.username, $person.email)
    }
    Write-Host ("`n{0} member(s), {1} API calls." -f @($people).Count, (Get-ClickUpCallCount))
}


function Invoke-Export {
    if (-not $List) { Write-Host 'export requires -List'; $script:Code = 1; return }
    $roots = Get-NestedTasks -Tasks (Get-ClickUpTasks -ListId $List)
    if ($AsSpec) {
        (ConvertTo-Spec -ListId $List -Roots $roots) | ConvertTo-Json -Depth 12
    } else {
        $roots | ConvertTo-Json -Depth 12
    }
}


# ---------------------------------------------------------------------------

try {
    switch ($Command) {
        'tree'   { Invoke-Tree }
        'tasks'  { Invoke-Tasks }
        'get'    { Invoke-Get }
        'find'   { Invoke-Find }
        'members' { Invoke-Members }
        'export' { Invoke-Export }
    }
    exit $script:Code
} catch {
    $message = $_.Exception.Message
    if ($message -like 'CLICKUP_RATE_LIMIT:*') {
        $seconds = [double]($message -replace '^CLICKUP_RATE_LIMIT:', '')
        Write-Host ("`nRate limited. Retry in {0:N0} minutes." -f ($seconds / 60))
        exit 2
    }
    [Console]::Error.WriteLine($message)
    exit 1
}

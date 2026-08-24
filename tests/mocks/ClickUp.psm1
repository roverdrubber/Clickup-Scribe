<#
.SYNOPSIS
    Mock ClickUp module for the parity harness.

.DESCRIPTION
    The PowerShell twin of tests/mocks/clickup_client.py. Reads the same
    fixture and keeps the same shape of state in state.json beside itself, so
    a difference in output between the two implementations means drift in the
    code under test rather than in the test data.

    Deliberately dumb, for the same reason as its Python twin.
#>

Set-StrictMode -Version 2.0

$script:Here = $PSScriptRoot
$script:FixturePath = $env:PARITY_FIXTURE
if (-not $script:FixturePath) { $script:FixturePath = Join-Path $script:Here 'board.json' }
$script:StatePath = Join-Path $script:Here 'state.json'
$script:Calls = 0


function Get-Fixture { return Get-Content $script:FixturePath -Raw | ConvertFrom-Json }

function Get-State {
    if (Test-Path $script:StatePath) {
        return Get-Content $script:StatePath -Raw | ConvertFrom-Json
    }
    return [pscustomobject]@{ created = @(); updated = @(); n = 0 }
}

function Save-State { param($State) $State | ConvertTo-Json -Depth 10 | Set-Content $script:StatePath }

function Get-Member2 {
    <#.SYNOPSIS Property lookup that tolerates absence, for both shapes.#>
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $Default
    }
    if ($Object.PSObject.Properties.Name -contains $Name) {
        $v = $Object.$Name
        if ($null -ne $v) { return $v }
    }
    return $Default
}


function Get-ClickUpCallCount { return $script:Calls }
function Get-ClickUpToken { return 'pk_mock' }
function Invoke-ClickUp { param($Method, $Path, $Body, $Retries) return @{} }


function Get-ClickUpTasks {
    param([string]$ListId, [bool]$IncludeClosed = $true, [bool]$Subtasks = $true)
    $script:Calls++

    $fixture = Get-Fixture
    $state = Get-State

    $rows = @(Get-Member2 $fixture.tasks $ListId @())
    $rows += @(@(Get-Member2 $state 'created' @()) | Where-Object { $_.list_id -eq $ListId })

    $overrides = @{}
    foreach ($u in @(Get-Member2 $state 'updated' @())) { $overrides[$u.id] = $u.status }

    $out = @()
    foreach ($row in $rows) {
        $status = $row.status
        if ($overrides.ContainsKey($row.id)) { $status = $overrides[$row.id] }
        $assignees = @()
        foreach ($a in @(Get-Member2 $row 'assignees' @())) {
            $assignees += ,([pscustomobject]@{ username = $a })
        }
        $out += ,([pscustomobject]@{
            id = $row.id; name = $row.name
            parent = (Get-Member2 $row 'parent')
            status = [pscustomobject]@{ status = $status }
            assignees = $assignees
            description = (Get-Member2 $row 'description' '')
        })
    }
    return $out
}


function Get-ClickUpList {
    param([string]$ListId)
    $script:Calls++
    $fixture = Get-Fixture
    $lst = Get-Member2 $fixture.lists $ListId
    $statuses = @()
    foreach ($s in $fixture.statuses) { $statuses += ,([pscustomobject]@{ status = $s }) }
    return [pscustomobject]@{ id = $lst.id; name = $lst.name; statuses = $statuses }
}


function Get-ClickUpStatuses {
    param([string]$ListId)
    return @((Get-ClickUpList -ListId $ListId).statuses | ForEach-Object { $_.status })
}


function Get-ClickUpTask {
    param([string]$TaskId, [bool]$Subtasks = $true)
    $script:Calls++
    $fixture = Get-Fixture
    foreach ($listId in $fixture.tasks.PSObject.Properties.Name) {
        $rows = @($fixture.tasks.$listId)
        foreach ($row in $rows) {
            if ($row.id -ne $TaskId) { continue }
            $kids = @()
            foreach ($k in $rows) {
                if ((Get-Member2 $k 'parent') -eq $TaskId) {
                    $kids += ,([pscustomobject]@{ id = $k.id; name = $k.name
                        status = [pscustomobject]@{ status = $k.status } })
                }
            }
            $assignees = @()
            foreach ($a in @(Get-Member2 $row 'assignees' @())) {
                $assignees += ,([pscustomobject]@{ username = $a })
            }
            $lst = Get-Member2 $fixture.lists $listId
            return [pscustomobject]@{
                id = $row.id; name = $row.name
                status = [pscustomobject]@{ status = $row.status }
                list = [pscustomobject]@{ id = $listId; name = $lst.name }
                parent = (Get-Member2 $row 'parent')
                assignees = $assignees
                description = (Get-Member2 $row 'description' '')
                subtasks = $kids
            }
        }
    }
    throw "task $TaskId not found"
}


function Get-ClickUpTeams { $script:Calls++; return @((Get-Fixture).teams) }

function Get-ClickUpSpaces {
    param([string]$TeamId, [bool]$Archived = $false)
    $script:Calls++
    return @(Get-Member2 (Get-Fixture).spaces $TeamId @())
}

function Get-ClickUpFolders {
    param([string]$SpaceId, [bool]$Archived = $false)
    $script:Calls++
    return @(Get-Member2 (Get-Fixture).folders $SpaceId @())
}

function Get-ClickUpFolderlessLists {
    param([string]$SpaceId, [bool]$Archived = $false)
    $script:Calls++
    return @(Get-Member2 (Get-Fixture).folderless_lists $SpaceId @())
}

function Get-ClickUpFolderLists {
    param([string]$FolderId, [bool]$Archived = $false)
    $script:Calls++
    return @(Get-Member2 (Get-Fixture).folder_lists $FolderId @())
}


function New-ClickUpTask {
    param([string]$ListId, [string]$Name, [string]$Description = '',
          [string]$Parent = $null, [string]$Status = $null, [int[]]$Assignees = @())
    $script:Calls++
    $state = Get-State
    $n = [int]$state.n + 1
    $taskId = 'new{0:D2}' -f $n
    $status2 = $Status
    if (-not $status2) { $status2 = 'completed' }
    $entry = [pscustomobject]@{
        id = $taskId; list_id = $ListId; name = $Name; parent = $Parent
        status = $status2; assignees = @(); description = $Description
    }
    $state.created = @(@(Get-Member2 $state 'created' @()) + @($entry))
    $state.n = $n
    Save-State -State $state
    return [pscustomobject]@{ id = $taskId; url = "https://app.clickup.com/t/$taskId" }
}


function Set-ClickUpTask {
    param([string]$TaskId, [hashtable]$Fields)
    $script:Calls++
    $state = Get-State
    $status = $null
    if ($Fields.ContainsKey('status')) { $status = $Fields['status'] }
    $state.updated = @(@(Get-Member2 $state 'updated' @()) +
                       @([pscustomobject]@{ id = $TaskId; status = $status }))
    Save-State -State $state
    return @{}
}


Export-ModuleMember -Function *

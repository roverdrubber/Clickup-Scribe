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

# Pull the real module in for the pure helpers - ConvertTo-Priority,
# ConvertTo-EpochMs, New-ClickUpTaskBody - so the harness exercises the real
# parsing rather than a second copy of it. Only the data-returning functions
# below are faked; they shadow the real ones in this module's export set.
if ($env:PARITY_REAL_MODULE) { Import-Module $env:PARITY_REAL_MODULE -Force }

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
    return [pscustomobject]@{ created = @(); updated = @(); deleted = @(); n = 0 }
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
    foreach ($u in @(Get-Member2 $state 'updated' @())) {
        $st = Get-Member2 $u 'status'
        if ($st) { $overrides[$u.id] = $st }
    }
    $gone = @(Get-Member2 $state 'deleted' @())

    $out = @()
    foreach ($row in $rows) {
        if ($gone -contains $row.id) { continue }
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
          [string]$Parent = $null, [string]$Status = $null, [int[]]$Assignees = @(),
          $Priority = $null, $DueDate = $null, $StartDate = $null,
          $TimeEstimate = $null, [string[]]$Tags = @())
    $script:Calls++
    # The real body builder runs, so parsing is under test.
    $body = New-ClickUpTaskBody -Name $Name -Description $Description `
        -Parent $Parent -Status $Status -Assignees $Assignees `
        -Priority $Priority -DueDate $DueDate -StartDate $StartDate `
        -TimeEstimate $TimeEstimate -Tags $Tags
    $state = Get-State
    $n = [int]$state.n + 1
    $taskId = 'new{0:D2}' -f $n
    $status2 = $Status
    if (-not $status2) { $status2 = 'completed' }
    $parentValue = $null
    if ($Parent) { $parentValue = $Parent }
    $entry = [pscustomobject]@{
        id = $taskId; list_id = $ListId; name = $Name; parent = $parentValue
        status = $status2; assignees = @(); description = $Description; body = $body
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
    $entry = [ordered]@{ id = $TaskId }
    foreach ($key in @($Fields.Keys | Sort-Object)) { $entry[$key] = $Fields[$key] }
    $state.updated = @(@(Get-Member2 $state 'updated' @()) + @([pscustomobject]$entry))
    Save-State -State $state
    return @{}
}


function New-ClickUpFolder {
    param([string]$SpaceId, [string]$Name)
    $script:Calls++
    $state = Get-State
    $n = [int]$state.n + 1
    $id = 'fold{0:D2}' -f $n
    $state | Add-Member -NotePropertyName folders -NotePropertyValue @() -Force `
        -ErrorAction SilentlyContinue
    $state.folders = @(@(Get-Member2 $state 'folders' @()) +
        @([pscustomobject]@{ id = $id; space_id = $SpaceId; name = $Name }))
    $state.n = $n
    Save-State -State $state
    return [pscustomobject]@{ id = $id; name = $Name }
}


function New-ClickUpList {
    param([string]$Name, [string]$FolderId, [string]$SpaceId, [string]$Content)
    if ([bool]$FolderId -eq [bool]$SpaceId) {
        throw 'a list needs exactly one container: FolderId or SpaceId.'
    }
    $script:Calls++
    $state = Get-State
    $n = [int]$state.n + 1
    $id = 'list{0:D2}' -f $n
    $folderValue = $null; if ($FolderId) { $folderValue = $FolderId }
    $spaceValue = $null;  if ($SpaceId)  { $spaceValue = $SpaceId }
    $contentValue = $null; if ($Content) { $contentValue = $Content }
    $state | Add-Member -NotePropertyName lists -NotePropertyValue @() -Force `
        -ErrorAction SilentlyContinue
    $state.lists = @(@(Get-Member2 $state 'lists' @()) +
        @([pscustomobject]@{ id = $id; folder_id = $folderValue
                             space_id = $spaceValue; name = $Name; content = $contentValue }))
    $state.n = $n
    Save-State -State $state
    return [pscustomobject]@{ id = $id; name = $Name }
}


function Remove-ClickUpTask {
    param([string]$TaskId)
    $script:Calls++
    $state = Get-State
    $state.deleted = @(@(Get-Member2 $state 'deleted' @()) + @($TaskId))
    Save-State -State $state
    return @{}
}


function Get-ClickUpMembers {
    param()
    $fixture = Get-Fixture
    $out = @()
    foreach ($m in @(Get-Member2 $fixture 'members' @())) {
        $out += ,([ordered]@{ id = $m.id; username = $m.username
                              email = $m.email; workspace = 'Workspace' })
    }
    return @($out | Sort-Object { $_.username }, { $_.id })
}


function Resolve-ClickUpAssignees {
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


Export-ModuleMember -Function *

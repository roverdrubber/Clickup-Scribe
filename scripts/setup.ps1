<#
.SYNOPSIS
    Store a ClickUp API token once, so no command needs an env var again.

.DESCRIPTION
    The PowerShell twin of setup.py. The prompt does not echo, and the token is
    never printed back, so it stays out of your shell history and out of any
    transcript. Run this in your own terminal: an agent's shell cannot type
    into a hidden prompt.

.EXAMPLE
    .\setup.ps1 store     # prompt for a token, check it, save it
    .\setup.ps1 show      # where the token comes from - never the token itself
    .\setup.ps1 clear     # remove the stored token
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('store', 'show', 'clear')]
    [string]$Command
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'ClickUp.psm1') -Force


function Invoke-Show {
    Write-Host ("token source: {0}" -f (Get-ClickUpTokenSource))
    $path = Get-ClickUpTokenPath
    $suffix = ''
    if (-not (Test-Path $path)) { $suffix = '  (absent)' }
    Write-Host ("stored file:  {0}{1}" -f $path, $suffix)
    if ($env:CLICKUP_TOKEN) {
        Write-Host ("`nThe environment wins over the stored file. Clear " +
                    '$env:CLICKUP_TOKEN to use the stored one.')
    }
    return 0
}


function Invoke-Clear {
    $path = Get-ClickUpTokenPath
    if (-not (Test-Path $path)) {
        Write-Host ("Nothing stored at {0}" -f $path)
        return 0
    }
    Remove-Item $path -Force
    Write-Host ("removed {0}" -f $path)
    if ($env:CLICKUP_TOKEN) {
        Write-Host 'CLICKUP_TOKEN is still set in this environment.'
    }
    return 0
}


function Invoke-Store {
    if ([Console]::IsInputRedirected) {
        [Console]::Error.WriteLine(
            'store needs a terminal: the prompt is hidden so the token is not echoed.')
        [Console]::Error.WriteLine(
            'Run it in your own shell, or set $env:CLICKUP_TOKEN for a one-off.')
        return 1
    }
    Write-Host 'ClickUp > Settings > Apps > API Token. Starts with pk_.'
    $secure = Read-Host -Prompt 'Token (not shown)' -AsSecureString
    $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()

    if (-not $token) {
        [Console]::Error.WriteLine('Nothing entered.')
        return 1
    }
    if (-not $token.StartsWith('pk_')) {
        [Console]::Error.WriteLine(
            "That does not look like a personal token - they start with 'pk_'. Storing it anyway.")
    }

    # Check it before storing, so a typo fails here rather than later.
    $previous = $env:CLICKUP_TOKEN
    $env:CLICKUP_TOKEN = $token
    try {
        $people = Get-ClickUpMembers
    } catch {
        $env:CLICKUP_TOKEN = $previous
        [Console]::Error.WriteLine("`nClickUp rejected it: $($_.Exception.Message)")
        [Console]::Error.WriteLine('Nothing was stored.')
        return 1
    }
    $env:CLICKUP_TOKEN = $previous

    $path = Get-ClickUpTokenPath
    $dir = Split-Path $path -Parent
    if (-not (Test-Path $dir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
    Set-Content -Path $path -Value $token -NoNewline

    Write-Host ("`nVerified: {0} workspace member(s) visible." -f @($people).Count)
    Write-Host ("Stored in {0}." -f $path)
    Write-Host 'Every command picks it up from there now - no env var needed.'
    return 0
}


try {
    switch ($Command) {
        'store' { exit (Invoke-Store) }
        'show'  { exit (Invoke-Show) }
        'clear' { exit (Invoke-Clear) }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}

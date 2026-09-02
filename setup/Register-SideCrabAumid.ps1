#Requires -Version 7.0
<#
.SYNOPSIS
    Registers (or removes) SideCrab's own Windows toast identity - the AppUserModelID
    'SideCrab.Notifier' - under HKCU.

.DESCRIPTION
    A Windows toast can only be raised through a REGISTERED AppUserModelID. With none of
    its own, notifier\sidecrab_toast.py borrows Windows PowerShell's, and the consequences
    are visible: Action Center groups SideCrab's toasts under "Windows PowerShell", and the
    per-app notification switch a user would flip for SideCrab is really PowerShell's - so
    silencing PowerShell silences SideCrab, and vice versa.

    This script closes that gap with the smallest write that does it:

        HKCU:\SOFTWARE\Classes\AppUserModelId\SideCrab.Notifier
            DisplayName = SideCrab
            IconUri     = <repo>\notifier\sidecrab.ico

    HKCU only - no elevation, no COM server, no Start-menu shortcut, no machine-wide state.
    Idempotent: values are compared before they are written, so a re-run reports 'unchanged'.
    -Remove deletes the key (and only that key - the shared parent is left alone).

    The notifier PROBES for this key at toast time and falls back to the borrowed AUMID when
    it is absent, so registering is an upgrade and removing is a downgrade - never a break.
    A notifier process caches the answer, so restart SideCrab-toast after either.

.EXAMPLE
    pwsh -File .\setup\Register-SideCrabAumid.ps1
.EXAMPLE
    pwsh -File .\setup\Register-SideCrabAumid.ps1 -WhatIf
.EXAMPLE
    pwsh -File .\setup\Register-SideCrabAumid.ps1 -Remove
.EXAMPLE
    pwsh -File .\setup\Register-SideCrabAumid.ps1 -Status
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch] $Remove,
    [switch] $Status
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

function Write-Step { param([string] $Message) Write-Host "  $Message" }

function Show-AumidStatus {
    <# Read-only by design: reads the key and the icon file, writes nothing. #>
    param([string] $RepoRoot)

    $state = Get-SideCrabAumidState -RepoRoot $RepoRoot
    Write-Host 'SideCrab toast identity'
    Write-Step "aumid:   $($state.Aumid)"
    Write-Step "key:     $($state.RegistryPath)"
    if ($state.Registered) {
        Write-Step "state:   registered$(if ($state.Current) { ' (current)' } else { ' (values differ - re-run to update)' })"
        Write-Step "display: $($state.DisplayName)"
        Write-Step "icon:    $($state.IconUri)"
    } else {
        Write-Step 'state:   not registered - toasts group under "Windows PowerShell"'
    }
    if (-not $state.IconPresent) {
        Write-Step "icon:    MISSING at $($state.IconPath) - run: python notifier\make_icon.py"
    }
    Write-Host 'Nothing was changed.'
}

# ------------------------------------------------------------------------------ run

if ($Status) {
    Show-AumidStatus -RepoRoot $RepoRoot
    return
}

if ($Remove) {
    Write-Host 'SideCrab toast identity - remove'
    $result = Remove-SideCrabAumid -RepoRoot $RepoRoot
    switch ($result.Action) {
        'removed' { Write-Step "aumid:   $($result.Aumid) removed ($($result.RegistryPath))" }
        'absent'  { Write-Step "aumid:   $($result.Aumid) was not registered" }
        default   { Write-Step "aumid:   $($result.Aumid) not removed ($($result.Action))" }
    }
    Write-Step 'note:    restart SideCrab-toast so the notifier re-probes and falls back.'
    return
}

Write-Host 'SideCrab toast identity - register'
$result = Set-SideCrabAumid -RepoRoot $RepoRoot
Write-Step "aumid:   $($result.Aumid) [$($result.Action)]"
Write-Step "key:     $($result.RegistryPath)"
Write-Step "display: $($result.DisplayName)"
if ($result.IconUri) { Write-Step "icon:    $($result.IconUri)" }
if ($result.Action -ne 'unchanged') {
    Write-Step 'note:    restart SideCrab-toast so the notifier re-probes and picks it up.'
}

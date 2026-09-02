#Requires -Version 7.0
<#
.SYNOPSIS
    Registers (or removes) the SideCrab toast URL protocols - the handlers behind the
    Acknowledge and Snooze 30m buttons on a SideCrab toast.

.DESCRIPTION
    A toast button can activate in three ways. Two of them (foreground/background
    activation) need a COM-registered application; the third, protocol activation, needs
    only a registered URL scheme - which is a pair of HKCU keys and no elevation.

    Protocol activation is also the only one that still works LATE. A toast sits in Action
    Center until it is dismissed, long after the notifier that raised it may have exited;
    the shell can still route a URI, where a callback into a dead process cannot.

    This registers the smallest thing that does it, ONE SCHEME PER BUTTON (the table is
    Get-SideCrabProtocolSpec in SideCrab.Common.ps1):

        HKCU:\SOFTWARE\Classes\sidecrab-ack
            (Default)      = URL:SideCrab Acknowledge
            URL Protocol   = ''                          <- the flag Windows actually gates on
        HKCU:\SOFTWARE\Classes\sidecrab-ack\shell\open\command
            (Default)      = "<pythonw.exe>" "<repo>\notifier\sidecrab_ack_handler.pyw" "%1"

        HKCU:\SOFTWARE\Classes\sidecrab-snooze
            (Default)      = URL:SideCrab Snooze
            URL Protocol   = ''
        HKCU:\SOFTWARE\Classes\sidecrab-snooze\shell\open\command
            (Default)      = "<pythonw.exe>" "<repo>\notifier\sidecrab_snooze_handler.pyw" "%1"

    Pressing a button starts its handler with '<scheme>:<sessionId>'. Both handlers validate
    the session id against ^[A-Za-z0-9-]{1,64}$ before using it for anything. The ack handler
    POSTs {"sessionId":...,"action":"ack"} to crabd's /v1/action; the snooze handler writes a
    30-minute mark to ~/.sidecrab/toast-state.json and deliberately never touches crabd - a
    snooze is a statement about notifications, not an answer to the question
    (notifier\README.md, "Snooze 30m"). Both exit silently on any failure.

    HKCU only - no elevation, no COM server, no machine-wide state. Idempotent: each command
    string is compared before it is written, so a re-run reports 'unchanged'. -Remove
    deletes the scheme keys and their subkeys (all of which are ours).

    Registering is an upgrade and removing a downgrade, never a break: the toast itself is
    unaffected either way. With no registration the button is present but the shell has
    nowhere to send it, so restart SideCrab-toast is NOT needed after this - unlike the
    AUMID, nothing about this is cached in the notifier process.

.EXAMPLE
    pwsh -File .\setup\Register-SideCrabProtocol.ps1
.EXAMPLE
    pwsh -File .\setup\Register-SideCrabProtocol.ps1 -WhatIf
.EXAMPLE
    pwsh -File .\setup\Register-SideCrabProtocol.ps1 -Remove
.EXAMPLE
    pwsh -File .\setup\Register-SideCrabProtocol.ps1 -Status
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $PythonExe,
    [switch] $Remove,
    [switch] $Status
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

function Write-Step { param([string] $Message) Write-Host "  $Message" }

function Show-ProtocolStatus {
    <# Read-only by design: reads the keys and the handler files, writes nothing. One block
       per scheme - each button is registered, or not, on its own. #>
    param([string] $RepoRoot, [string] $PythonExe)

    Write-Host 'SideCrab toast actions'
    foreach ($state in @(Get-SideCrabProtocolState -RepoRoot $RepoRoot -PythonExe $PythonExe)) {
        Write-Host "  --- $($state.Button)"
        Write-Step "scheme:  $($state.Scheme):"
        Write-Step "key:     $($state.RegistryPath)"
        if ($state.Registered) {
            Write-Step "state:   registered$(if ($state.Current) { ' (current)' } else { ' (command differs - re-run to update)' })"
            Write-Step "command: $($state.Command)"
            if (-not $state.CarriesArgument) {
                Write-Step 'command: MISSING "%1" - the handler would be started with no URI'
            }
            if (-not $state.Current -and $state.Expected) { Write-Step "expected: $($state.Expected)" }
        } elseif ($state.UrlProtocolFlag) {
            Write-Step 'state:   half-registered - URL Protocol set but no shell\open\command'
        } else {
            Write-Step "state:   not registered - the $($state.Button) button has nowhere to go"
        }
        if (-not $state.HandlerPresent) {
            Write-Step "handler: MISSING at $($state.HandlerPath)"
        } else {
            Write-Step "handler: $($state.HandlerPath)"
        }
    }
    Write-Host 'Nothing was changed.'
}

# ------------------------------------------------------------------------------ run

if ($Status) {
    Show-ProtocolStatus -RepoRoot $RepoRoot -PythonExe $PythonExe
    return
}

if ($Remove) {
    Write-Host 'SideCrab toast actions - remove'
    foreach ($result in @(Remove-SideCrabProtocol -RepoRoot $RepoRoot)) {
        switch ($result.Action) {
            'removed' { Write-Step "scheme:  $($result.Scheme): removed ($($result.RegistryPath))" }
            'absent'  { Write-Step "scheme:  $($result.Scheme): was not registered" }
            default   { Write-Step "scheme:  $($result.Scheme): not removed ($($result.Action))" }
        }
    }
    Write-Step 'note:    toasts already in Action Center keep their buttons; they will no longer resolve.'
    return
}

Write-Host 'SideCrab toast actions - register'
foreach ($result in @(Set-SideCrabProtocol -RepoRoot $RepoRoot -PythonExe $PythonExe)) {
    Write-Step "scheme:  $($result.Scheme): [$($result.Action)]"
    Write-Step "key:     $($result.RegistryPath)"
    Write-Step "command: $($result.Command)"
}

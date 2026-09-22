#Requires -Version 7.0
<#
.SYNOPSIS
    Registers the scheduled task that relaunches HWiNFO64, so its shared memory keeps
    publishing past the free build's twelve-hour limit.

.DESCRIPTION
    crabd reads this machine's temperatures, fan speeds and package power out of
    HWiNFO's shared memory (`Global\HWiNFO_SENS_SM2`). The FREE build stops publishing
    about twelve hours after launch and leaves the mapping in place with its last poll
    time frozen in it - which crabd reports honestly as `sensorsSource.stale` with a
    note, and the panel dims. This task is the fix for the cause rather than the
    symptom: once a day it stops HWiNFO and starts it again, and the twelve hours
    begin from there.

    THE PRO LICENCE REMOVES THE NEED for this task entirely - the paid build publishes
    for as long as it runs. The free build is also licensed for NON-COMMERCIAL use
    only (hwinfo.com). If either of those applies to you, buy the licence and do not
    register this.

    What the task does, in ONE PowerShell action:
      1. stop the running HWiNFO64 BY ITS PID, filtered on the exact executable path.
         Never by name alone: a name match would take down an unrelated HWiNFO64.exe
         from another install or a portable copy somebody is using.
      2. start that same executable again THROUGH ShellExecute (Start-Process), never
         as a second task action. HWiNFO64.EXE's manifest is requireAdministrator with
         uiAccess="true", and Task Scheduler launches actions with CreateProcessAsUser:
         a uiAccess binary refuses that launch with ERROR_ELEVATION_REQUIRED (740,
         event 203, task result 0x800702E4) even at RunLevel Highest. ShellExecute
         routes through the AppInfo service, which mints the UIAccess token; from an
         already-elevated action there is no prompt. Measured on 2026-09-21: the
         two-action shape failed every run, the one-action shape relaunches.
         Whether it comes back MINIMISED is HWiNFO's own setting, not a switch this
         task passes: MinimalizeMainWnd and MinimalizeSensors in HWiNFO64.INI. Set
         them once and every relaunch is quiet.

    Registered at RunLevel Highest because HWiNFO needs administrator rights for its
    kernel driver, so a limited task can neither stop nor start it. Two triggers: at
    logon (this build does not always create its own auto-start entry) and daily at
    04:00. MultipleInstances is Parallel: the task's own instance stays Running for as
    long as HWiNFO does, and the IgnoreNew default would silently swallow every
    relaunch after the first.

    THE SENSORS WINDOW HAS TO BE OPEN for the shared memory to exist at all -
    minimised counts, closed does not. Set HWiNFO to open it at startup (its INI keys
    SensorsOnly / OpenSensors / MinimalizeSensors) and leave Shared Memory Support on
    (SensorsSM), or the relaunch will bring back a HWiNFO that publishes nothing. This
    script READS those keys and warns; it does not write them, because the INI lives
    under Program Files and an operator's own settings are not this task's to change.

    Nothing else is touched. No file is written, no setting is changed, and the only
    state this creates is the one scheduled task.

.PARAMETER Remove
    Unregister the task and exit.

.EXAMPLE
    pwsh -File .\setup\Register-HwinfoRelaunch.ps1
.EXAMPLE
    pwsh -File .\setup\Register-HwinfoRelaunch.ps1 -WhatIf
.EXAMPLE
    pwsh -File .\setup\Register-HwinfoRelaunch.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskName = 'SideCrab-hwinfo',
    [string] $ExePath  = 'C:\Program Files\HWiNFO64\HWiNFO64.EXE',
    [string] $At       = '04:00',
    [switch] $Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsElevated {
    <# Registering at RunLevel Highest, and stopping a process that runs elevated, both
       need an elevated shell. Checked up front and said plainly: the failure without
       this check is an "Access is denied" from Register-ScheduledTask that reads like
       a bug in the script. #>
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal] $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-HwinfoIniState {
    <# The three INI keys that decide whether the mapping exists after a relaunch, read
       only. Absent keys are the product's defaults, which are OFF for all three, so an
       unreadable or key-less INI is reported as unknown rather than as fine. #>
    param([string] $IniPath)

    $out = [pscustomobject]@{ Present = $false; SharedMemory = $null; SensorsAtStart = $null }
    if (-not (Test-Path -LiteralPath $IniPath)) { return $out }
    $out.Present = $true
    try { $text = Get-Content -LiteralPath $IniPath -Raw -Encoding utf8 -ErrorAction Stop }
    catch { return $out }
    $out.SharedMemory = [bool] ([regex]::IsMatch($text, '(?im)^\s*SensorsSM\s*=\s*1\s*$'))
    $out.SensorsAtStart = [bool] (
        [regex]::IsMatch($text, '(?im)^\s*SensorsOnly\s*=\s*1\s*$') -or
        [regex]::IsMatch($text, '(?im)^\s*OpenSensors\s*=\s*1\s*$'))
    return $out
}

function Get-HwinfoStopCommand {
    <# The stop half of the action's command line. BY PID, FILTERED ON PATH, and that is
       the whole point of this function existing rather than the string being inline:
       `Stop-Process -Name HWiNFO64` would also kill a portable copy or a second install
       that has nothing to do with this task. Get-Process -Path is not a parameter, so
       the path filter is a Where-Object on the process object's own Path. #>
    param([string] $Path)
    $quoted = $Path.Replace("'", "''")
    "Get-Process -Name HWiNFO64 -ErrorAction SilentlyContinue | " +
    "Where-Object { `$_.Path -eq '$quoted' } | " +
    "Stop-Process -Force -ErrorAction SilentlyContinue"
}

function Get-HwinfoStartCommand {
    <# The start half. Start-Process with no redirection is ShellExecuteEx, which is the
       only launch a uiAccess="true" binary accepts from a scheduled task (see the
       DESCRIPTION). The three-second sleep before it lets the stopped instance release
       its driver and the Global\HWiNFO_SENS_SM2 mapping; a start on its heels comes up
       without sensors. #>
    param([string] $Path)
    $quoted = $Path.Replace("'", "''")
    $dir = (Split-Path -Parent $Path).Replace("'", "''")
    "Start-Sleep -Seconds 3; " +
    "Start-Process -FilePath '$quoted' -WorkingDirectory '$dir'"
}

# ------------------------------------------------------------------------------ run

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "  $TaskName is not registered - nothing to remove"
        return
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  $TaskName unregistered"
    }
    return
}

if (-not (Test-IsElevated)) {
    Write-Host 'HWiNFO runs elevated (its kernel driver needs it), so this task must be' `
               'registered at RunLevel Highest from an ELEVATED shell.' -ForegroundColor Yellow
    Write-Host 'Re-run this in a PowerShell started with "Run as administrator".' -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Host "HWiNFO64 not found at $ExePath" -ForegroundColor Yellow
    Write-Host 'Install it, or pass -ExePath with the location you used.' -ForegroundColor Yellow
    exit 1
}

$ini = Get-HwinfoIniState -IniPath (Join-Path (Split-Path -Parent $ExePath) 'HWiNFO64.INI')
if (-not $ini.Present) {
    Write-Host '  note: no HWiNFO64.INI beside the executable yet - run HWiNFO once and' `
               'turn on Shared Memory Support and the Sensors window at startup.' -ForegroundColor Yellow
} else {
    if ($ini.SharedMemory -ne $true) {
        Write-Host '  note: Shared Memory Support (SensorsSM) is not on in HWiNFO64.INI -' `
                   'crabd will read no sensors until it is.' -ForegroundColor Yellow
    }
    if ($ini.SensorsAtStart -ne $true) {
        Write-Host '  note: HWiNFO is not set to open its Sensors window at start -' `
                   'the shared memory exists only while that window is open.' -ForegroundColor Yellow
    }
}

# One action, not two: a second action of "$ExePath" is the shape Task Scheduler
# cannot launch for this binary (uiAccess, see the DESCRIPTION).
$relaunch = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -NonInteractive -WindowStyle Hidden -Command "' +
    ((Get-HwinfoStopCommand -Path $ExePath) + '; ' +
     (Get-HwinfoStartCommand -Path $ExePath)).Replace('"', '\"') + '"')

$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn
    New-ScheduledTaskTrigger -Daily -At $At
)

$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
                                        -LogonType Interactive -RunLevel Highest
# Parallel, not the IgnoreNew default: the action returns as soon as Start-Process has
# handed HWiNFO off, but a logon-trigger instance still sleeping through its three
# seconds must not make IgnoreNew swallow a 04:00 relaunch that lands beside it.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel `
                                         -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                                         -StartWhenAvailable

$task = New-ScheduledTask -Action $relaunch -Trigger $triggers `
                          -Principal $principal -Settings $settings `
                          -Description 'Relaunches HWiNFO64 so its shared memory keeps publishing past the free build''s 12-hour limit (SideCrab host sensors).'

if ($PSCmdlet.ShouldProcess($TaskName, 'Register scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Host "  $TaskName registered - at logon, and daily at $At"
    Write-Host "  stop:  $(Get-HwinfoStopCommand -Path $ExePath)"
    Write-Host "  start: $(Get-HwinfoStartCommand -Path $ExePath)"
}

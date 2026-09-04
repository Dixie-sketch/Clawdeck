#Requires -Version 7.0
<#
.SYNOPSIS
    Removes the SideCrab companion: every registered SideCrab-* Scheduled Task and
    the SideCrab hook entries in ~/.claude/settings.json.

.DESCRIPTION
    The reverse of Install-SideCrab.ps1 and equally safe to re-run. Tasks are removed
    for whichever SideCrab components are registered - crabd, glow, toast, and any
    other SideCrab-* task found on the machine, so a component installed by a newer
    version of the installer is still cleaned up by an older uninstaller.

    Only hook entries whose command (or, for the type-http hooks, url) contains the crabd
    URL are removed; every other hook survives - including one hand-merged INTO a SideCrab
    matcher, which keeps its place while the SideCrab entries beside it go (the matcher is
    dropped only once it is empty). The status line is restored to whatever the
    installer saved to ~/.sidecrab/statusline-chain.json - the operator's prior command, or
    nothing when none existed - and the chain file is dropped; -KeepStatusLine leaves it.
    RESTORED ONLY OVER OUR OWN: if the status line configured now is somebody else's - you
    installed one after SideCrab - it is left exactly where it is and the prior we would have
    restored is printed instead. The config.json panelApprovals key is cleared (-KeepApprovals
    leaves it). settings.json is backed up (timestamped) before any write.

    The toast component's HKCU registrations are removed too, so an uninstall leaves no
    registry residue: the app identity (AppUserModelId\SideCrab.Notifier) and every toast
    button protocol - 'sidecrab-ack:' (Acknowledge) and 'sidecrab-snooze:' (Snooze 30m).
    -KeepAumid and -KeepProtocol hold them - only useful when the tasks are being
    re-registered from another checkout.

    -TaskName IS SURGERY ON ONE COMPONENT, and removes only that component's own surface:
      -TaskName SideCrab-crabd   its task + the hooks, the status line and panelApprovals
      -TaskName SideCrab-toast   its task + the AUMID and the two button schemes
      -TaskName SideCrab-glow    its task, and nothing else
    A name the catalogue does not know removes that task and nothing else. Without -TaskName
    every surface goes. (The switch used to narrow the TASK deletion alone and then strip the
    hooks, status line and approvals regardless - the ownership table is
    Get-SideCrabUninstallScope in SideCrab.Common.ps1.)

    WHAT AN UNINSTALL REMOVES, AND WHAT IT DELIBERATELY LEAVES

    The rule, in one line: an uninstall removes WIRING and keeps DATA. Wiring exists only to
    make this machine run SideCrab and is meaningless once it is gone; data is the operator's,
    or is about the operator's work, and outlives the install. The per-file table lives in one
    place - Get-SideCrabResidueSpec in SideCrab.Common.ps1 - and this script reads it rather
    than carrying its own list, so the two can never disagree.

      REMOVED, always (wiring)
        the SideCrab-* scheduled tasks
        the SideCrab hook entries in settings.json          (other hooks untouched)
        our statusLine in settings.json, restored to the operator's prior
        ~/.sidecrab/statusline-chain.json                    the handoff that restore used
        HKCU AppUserModelId\SideCrab.Notifier                the toast identity
        HKCU sidecrab-ack, HKCU sidecrab-snooze              the toast buttons' schemes
        the panelApprovals KEY in config.json                (the rest of the file survives)

      KEPT unless -Purge is passed (data / cache / logs)
        ~/.sidecrab/config.json         quiet hours, recap repos, toast threshold - the
                                        operator's settings, not ours to delete
        ~/.sidecrab/history.jsonl       a record of the operator's own sessions
        ~/.sidecrab/toast-state.json    the digest + budget ledger
        ~/.sidecrab/limits-cache.json   derived, rebuildable, no secrets
        ~/.sidecrab/glow.log, logs/     the account of what the glow and the toasts did

      KEPT AT EVERY SWITCH, -Purge included
        ~/.claude/settings.json.sidecrab-bak-*   the backups. They are the way BACK from an
        install, and the moment you most need last week's settings.json is the moment after an
        uninstall went wrong. Prune them deliberately: Restore-SideCrab.ps1 -PruneOlderThan.

    A residue report prints at the end of every run, naming what was kept and the command that
    removes it - so "did that leave anything behind?" is answered without reading this header.

.EXAMPLE
    pwsh -File .\setup\Uninstall-SideCrab.ps1
.EXAMPLE
    pwsh -File .\setup\Uninstall-SideCrab.ps1 -Purge                   # also drop ~/.sidecrab data
.EXAMPLE
    pwsh -File .\setup\Uninstall-SideCrab.ps1 -TaskName SideCrab-glow   # just that one
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot     = (Split-Path -Parent $PSScriptRoot),
    [string] $SettingsPath = (Join-Path $HOME '.claude\settings.json'),
    [string] $ConfigPath   = (Join-Path $HOME '.sidecrab\config.json'),
    [string] $ChainPath    = (Join-Path $HOME '.sidecrab\statusline-chain.json'),
    [string] $TaskName,
    [switch] $KeepTask,
    [switch] $KeepHooks,
    [switch] $KeepStatusLine,
    [switch] $KeepAumid,
    [switch] $KeepProtocol,
    [switch] $KeepApprovals,
    [switch] $Purge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$HookUrlMarker = '127.0.0.1:9999/v1/hook'

function Write-Step { param([string] $Message) Write-Host "  $Message" }

function Backup-File {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Path.sidecrab-bak-$stamp"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    return $backup
}

function Remove-HookEntries {
    <# Removes SideCrab's hook ENTRIES, not the matchers that contain them.

       The installer writes each SideCrab hook as its own matcher, so entry and matcher are
       normally the same thing. They stop being the same the moment a human hand-merges a hook
       of their own into one of ours - and deleting the matcher would take their hook with it
       (docs\findings\QA-Audit-2026-08-27.md, SETUP MED). So each matcher is split on the crabd
       marker, our entries dropped, and the matcher itself removed only once it has nothing
       left in it. The count returned is entries removed. #>
    param([hashtable] $Settings)

    if (-not $Settings.ContainsKey('hooks') -or $Settings['hooks'] -isnot [hashtable]) {
        return 0
    }
    $removed = 0
    foreach ($eventName in @($Settings['hooks'].Keys)) {
        $kept = @()
        foreach ($matcher in @($Settings['hooks'][$eventName])) {
            $part = Split-SideCrabHookMatcher -Matcher $matcher -Marker $HookUrlMarker
            $removed += $part.OurCount
            # $part.Foreign is the original matcher when nothing of ours was in it, the
            # foreign-only half when it was shared, and $null when it was all ours.
            if ($null -ne $part.Foreign) { $kept += , $part.Foreign }
        }
        if ($kept.Count -eq 0) { $Settings['hooks'].Remove($eventName) }
        else                   { $Settings['hooks'][$eventName] = @($kept) }
    }
    # An empty hooks object is noise, not configuration.
    if ($Settings['hooks'].Count -eq 0) { $Settings.Remove('hooks') }
    return $removed
}

function Get-InstalledSideCrabTask {
    <# Every task this uninstaller is willing to remove: the catalogue's names plus
       any other registered SideCrab-* task. Discovery covers components added by a
       newer installer; the catalogue covers nothing on its own if none are present. #>
    param([string] $RepoRoot)

    $known = @(Get-SideCrabTaskName -Component (Get-SideCrabComponentSpec -RepoRoot $RepoRoot) -All)
    $found = @(Get-ScheduledTask -TaskName 'SideCrab-*' -ErrorAction SilentlyContinue |
               ForEach-Object { $_.TaskName })
    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($n in $known + $found) {
        if ($n -and -not $names.Contains($n)) { $names.Add($n) }
    }
    $names
}

# ------------------------------------------------------------------------------ run

Write-Host 'SideCrab uninstall'

# WHAT THIS RUN OWNS. A -TaskName run is surgery on ONE component and may remove only that
# component's surface - it used to narrow the task deletion alone and then strip the hooks,
# status line and approvals of an install nobody asked about. The table is
# Get-SideCrabUninstallScope; this script only reads it.
$componentSpec = @(Get-SideCrabComponentSpec -RepoRoot $RepoRoot)
$narrowName    = if ($PSBoundParameters.ContainsKey('TaskName')) { $TaskName } else { '' }
$scope         = Get-SideCrabUninstallScope -Spec $componentSpec -TaskName $narrowName
if ($scope.Narrowed) {
    $what = if ($scope.UnknownTask) {
                "not a component this catalogue knows - only that task is removed"
            } else {
                "the '$($scope.ComponentKey)' component only"
            }
    Write-Step "scope:   -TaskName $TaskName - $what"
}

if (-not $KeepTask) {
    # An explicit -TaskName narrows the sweep to that one task.
    $targets = if ($scope.Narrowed) {
                   @($TaskName)
               } else {
                   @(Get-InstalledSideCrabTask -RepoRoot $RepoRoot)
               }
    $removed = 0
    foreach ($name in $targets) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Step "task:    '$name' not present"
            continue
        }
        if ($PSCmdlet.ShouldProcess($name, 'Unregister scheduled task')) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Step "task:    '$name' removed"
            $removed++
        }
    }
    if ($removed -eq 0) { Write-Step 'task:    nothing to remove' }
} else {
    Write-Step 'task:    kept (-KeepTask)'
}

if (-not $KeepAumid) {
    if (-not $scope.Aumid) {
        Write-Step "aumid:   kept (-TaskName $TaskName does not own it)"
    } else {
        $result = Remove-SideCrabAumid -RepoRoot $RepoRoot
        switch ($result.Action) {
            'removed' { Write-Step "aumid:   $($result.Aumid) removed ($($result.RegistryPath))" }
            'absent'  { Write-Step "aumid:   $($result.Aumid) not registered" }
            default   { Write-Step "aumid:   $($result.Aumid) not removed ($($result.Action))" }
        }
    }
} else {
    Write-Step 'aumid:   kept (-KeepAumid)'
}

if (-not $KeepProtocol) {
    if (-not $scope.Protocol) {
        Write-Step "proto:   kept (-TaskName $TaskName does not own it)"
    } else {
        foreach ($result in @(Remove-SideCrabProtocol -RepoRoot $RepoRoot)) {
            switch ($result.Action) {
                'removed' { Write-Step "proto:   $($result.Scheme): removed ($($result.RegistryPath))" }
                'absent'  { Write-Step "proto:   $($result.Scheme): not registered" }
                default   { Write-Step "proto:   $($result.Scheme): not removed ($($result.Action))" }
            }
        }
    }
} else {
    Write-Step 'proto:   kept (-KeepProtocol)'
}

# settings.json: hook removal + status-line restore in one read / one backup / one write.
# Both are crabd's wiring, so a -TaskName run that is not crabd's leaves them alone entirely.
$doHooks      = (-not $KeepHooks)      -and $scope.Hooks
$doStatusLine = (-not $KeepStatusLine) -and $scope.StatusLine
if ($scope.Narrowed -and -not $scope.Hooks)      { Write-Step "hooks:   kept (-TaskName $TaskName does not own them)" }
if ($scope.Narrowed -and -not $scope.StatusLine) { Write-Step "statusln: kept (-TaskName $TaskName does not own it)" }

if (-not $doHooks -and -not $doStatusLine) {
    if (-not $scope.Narrowed -or $scope.Hooks)      { Write-Step 'hooks:   kept (-KeepHooks)' }
    if (-not $scope.Narrowed -or $scope.StatusLine) { Write-Step 'statusln: kept (-KeepStatusLine)' }
} elseif (-not (Test-Path -LiteralPath $SettingsPath)) {
    Write-Step "hooks:   $SettingsPath not present"
} else {
    $raw = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8
    if ($raw.Trim()) {
        $settings = $raw | ConvertFrom-Json -AsHashtable -Depth 40
        if ($settings -isnot [hashtable]) { throw "$SettingsPath is not a JSON object" }
        if ($PSCmdlet.ShouldProcess($SettingsPath, 'Remove SideCrab hooks / restore status line')) {
            $backup  = Backup-File -Path $SettingsPath
            $changed = $false

            if ($doHooks) {
                $removedHooks = Remove-HookEntries -Settings $settings
                if ($removedHooks -gt 0) {
                    Write-Step "hooks:   $removedHooks entr(ies) removed from $SettingsPath"
                    $changed = $true
                } else {
                    Write-Step 'hooks:   no SideCrab entries found'
                }
            } else {
                Write-Step 'hooks:   kept (-KeepHooks)'
            }

            if ($doStatusLine) {
                $saved      = Get-SideCrabSavedStatusLine -ChainPath $ChainPath
                $currentCmd = if ($settings.ContainsKey('statusLine') -and
                                  $settings['statusLine'] -is [hashtable]) {
                                  "$($settings['statusLine']['command'])"
                              } else { '' }
                # OWNERSHIP FIRST, then restore. Writing the saved prior over whatever is in
                # settings.json meant that installing a different status line after SideCrab
                # and then uninstalling replaced it with the one SideCrab had displaced -
                # and with no prior saved, deleted it outright. The decision is
                # Get-SideCrabStatusLineRestoreDecision; this only carries it out.
                $slDecision = Get-SideCrabStatusLineRestoreDecision `
                                  -CurrentCommand $currentCmd `
                                  -CurrentIsOurs (Test-SideCrabStatusLineIsOurs -Command $currentCmd) `
                                  -SavedPresent $saved.Present -SavedStatusLine $saved.StatusLine
                switch ($slDecision.Action) {
                    'restore' {
                        $settings['statusLine'] = $saved.StatusLine
                        Write-Step 'statusln: prior status line restored'
                    }
                    'remove' {
                        $settings.Remove('statusLine')
                        Write-Step "statusln: removed - $($slDecision.Reason)"
                    }
                    'preserve-foreign' {
                        # Loud, because the operator is entitled to know an uninstall found
                        # something it refused to touch - and what it did NOT put back.
                        Write-Host "    statusln: KEPT - the status line installed now is not SideCrab's, so it was left alone: $currentCmd" -ForegroundColor Yellow
                        if ($null -ne $saved.StatusLine) {
                            Write-Host "    statusln: the prior SideCrab saved and did NOT restore over it: $($saved.StatusLine['command'])" -ForegroundColor Yellow
                        }
                    }
                    default { Write-Step 'statusln: no SideCrab status line configured' }
                }
                if ($slDecision.Changed) {
                    $changed = $true
                    # The chain file is a transient handoff, not an SoR record - drop it once
                    # the prior it carried is back in settings.json. NOT dropped on
                    # preserve-foreign: the prior was never restored, so the tail block below
                    # still prints it before it goes.
                    Remove-Item -LiteralPath $ChainPath -Force -ErrorAction SilentlyContinue
                }
            } else {
                Write-Step 'statusln: kept (-KeepStatusLine)'
            }

            if ($changed) {
                $json = $settings | ConvertTo-Json -Depth 40
                Set-Content -LiteralPath $SettingsPath -Value $json -Encoding utf8NoBOM
                if ($backup) { Write-Step "backup:  $backup" }
            } elseif ($backup) {
                Remove-Item -LiteralPath $backup -Force   # nothing changed - no backup kept
            }
        }
    }
}

# The chain file is WIRING, not a record: the installer writes it so an uninstall can put the
# operator's prior status line back, and nothing reads it once that is done. The restore path
# above drops it on the way through - this catches the path that never RAN (no settings.json to
# restore into, or an unreadable one), where the file would otherwise be stranded forever with
# no install left to explain it. Whatever prior it still holds is printed before it goes, so
# nothing disappears silently.
if ($doStatusLine -and (Test-Path -LiteralPath $ChainPath)) {
    $stranded = Get-SideCrabSavedStatusLine -ChainPath $ChainPath
    if ($PSCmdlet.ShouldProcess($ChainPath, 'Remove the status-line chain file')) {
        if ($stranded.StatusLine -is [System.Collections.IDictionary]) {
            Write-Step "chain:   the prior status line it held, for your records: $($stranded.StatusLine['command'])"
        }
        Remove-Item -LiteralPath $ChainPath -Force
        Write-Step "chain:   $ChainPath removed"
    }
} elseif ($doStatusLine) {
    Write-Step 'chain:   no chain file left behind'
} elseif (-not $scope.StatusLine) {
    # Two different reasons not to touch it, and saying the wrong one sends the operator
    # looking for a switch they never passed.
    Write-Step "chain:   kept (-TaskName $TaskName does not own the status line)"
} else {
    Write-Step 'chain:   kept (-KeepStatusLine)'
}

# panel approval: clear the config.json key so no residue is left behind. crabd's gate, so a
# narrowed run that is not crabd's does not touch it.
if (-not $scope.Approvals) {
    Write-Step "approv:  kept (-TaskName $TaskName does not own it)"
} elseif (-not $KeepApprovals) {
    $res = Clear-SideCrabPanelApprovals -ConfigPath $ConfigPath
    switch ($res.Action) {
        'removed'     { Write-Step 'approv:  panelApprovals key removed from config.json' }
        'not-present' { Write-Step 'approv:  panelApprovals key not present' }
        'absent'      { Write-Step 'approv:  config.json not present' }
        default       { Write-Step "approv:  not removed ($($res.Action))" }
    }
} else {
    Write-Step 'approv:  kept (-KeepApprovals)'
}

# ------------------------------------------------- data files: kept, or dropped by -Purge
# One table for both branches (SideCrab.Common.ps1: Get-SideCrabResidueSpec) so what -Purge
# removes and what the report says are the same list by construction.
$residue = @(Get-SideCrabResidueSpec -SettingsPath $SettingsPath -ConfigPath $ConfigPath -ChainPath $ChainPath)

if ($Purge) {
    foreach ($r in @($residue | Where-Object { $_.Disposition -eq 'purge' })) {
        if (-not (Test-Path -LiteralPath $r.Path)) { Write-Step "purge:   $($r.Key) not present"; continue }
        if ($PSCmdlet.ShouldProcess($r.Path, "Remove SideCrab $($r.Kind)")) {
            Remove-Item -LiteralPath $r.Path -Recurse -Force
            Write-Step "purge:   $($r.Path) removed"
        }
    }
    foreach ($r in @($residue | Where-Object { $_.Disposition -eq 'purge-if-empty' })) {
        if (-not (Test-Path -LiteralPath $r.Path)) { continue }
        # Only when the purge emptied it: another tool may keep a file in ~/.sidecrab, and
        # removing a directory that is not ours alone is not an uninstall, it is a delete.
        $left = @(Get-ChildItem -LiteralPath $r.Path -Force -ErrorAction SilentlyContinue)
        if ($left.Count -gt 0) {
            Write-Step "purge:   $($r.Path) kept - $($left.Count) file(s) there are not ours"
            continue
        }
        if ($PSCmdlet.ShouldProcess($r.Path, 'Remove the now-empty SideCrab state directory')) {
            Remove-Item -LiteralPath $r.Path -Force
            Write-Step "purge:   $($r.Path) removed (empty)"
        }
    }
}

# ------------------------------------------------------------------ residue report (read-only)
# Always printed: "did that leave anything behind?" is the question every uninstall raises, and
# the honest answer here is "yes, on purpose, and here is what removes it".
Write-Host ''
Write-Host 'Left behind, deliberately:'
$kept = 0
foreach ($r in @($residue | Where-Object { $_.Disposition -eq 'purge' })) {
    if (-not (Test-Path -LiteralPath $r.Path)) { continue }
    $kept++
    Write-Step "$($r.Kind.PadRight(6)) $($r.Path)"
    Write-Host "           $($r.Why)" -ForegroundColor DarkGray
}
$backupDir  = Split-Path -Parent $SettingsPath
$backupGlob = Get-SideCrabBackupPattern -SettingsPath $SettingsPath
$backups = @(Get-ChildItem -LiteralPath $backupDir -Filter $backupGlob -File -ErrorAction SilentlyContinue)
if ($backups.Count -gt 0) {
    $kept++
    Write-Step "backup $($backups.Count) settings.json backup(s) in $backupDir"
    Write-Host '           the way back from this install - never removed by an uninstall, at any switch' -ForegroundColor DarkGray
}
if ($kept -eq 0) {
    Write-Step 'nothing - no data, cache, log or backup file remains'
} else {
    Write-Host ''
    if (-not $Purge) { Write-Step 'remove the data/cache/log files with: pwsh -File setup\Uninstall-SideCrab.ps1 -Purge' }
    if ($backups.Count -gt 0) { Write-Step 'prune the backups with:                pwsh -File setup\Restore-SideCrab.ps1 -PruneOlderThan 30' }
}

Write-Host ''
Write-Host 'Done.'

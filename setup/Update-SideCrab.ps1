#Requires -Version 7.0
<#
.SYNOPSIS
    Updates the SideCrab companion in place: fast-forward the repo, restart the
    registered SideCrab-* tasks, then wait for /v1/health to come back ok.

.DESCRIPTION
    Three steps, each of them reversible or read-only:
      1. git -C <repo> pull --ff-only   - a fast-forward or nothing, and only into a tree with
         no tracked modifications. The clean check is our own (git status --porcelain, read
         BEFORE the pull): --ff-only refuses a merge and refuses to clobber a file the incoming
         commits touch, but fast-forwards straight over local edits to any file they do not -
         so "dirty tree, no pull" was a promise nothing kept. A diverged tree still fails the
         pull itself. Untracked files warn and do not block.
      2. Restart ONLY the SideCrab-* tasks that are actually registered, through the
         shared Restart-SideCrabTask - which waits for the OLD process to release the
         port before starting the new one, and refuses to start at all if it does not
         come free. A component that was never installed is not started here.
      3. Verify BOTH that /v1/health answers ok (default timeout 30 s) AND that
         SideCrab-crabd is actually Running. An answer with the task not Running is a
         FAIL naming the PID that holds the port, not a pass: health-by-HTTP cannot
         tell who answered. Exits non-zero when that check does not stand.

    THE WIDGET IS NOT UPDATED BY THIS SCRIPT. The Xeneon Edge widget is installed
    into iCUE by importing the .icuewidget package; pulling the repo changes the
    source in widget\, never what iCUE is running.

.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1
.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1 -SkipPull      # restart + verify only
.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot   = (Split-Path -Parent $PSScriptRoot),
    [int]    $TimeoutSec = 30,
    [switch] $SkipPull,
    [switch] $SkipRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

function Write-Step { param([string] $Message) Write-Host "  $Message" }

function Invoke-Git {
    <# Native git: $ErrorActionPreference does not apply, so the exit code is the
       only failure signal and stderr must be folded in to report it. #>
    param([string[]] $GitArgs)
    $output = & git @GitArgs 2>&1
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = (@($output) | ForEach-Object { "$_" }) -join [Environment]::NewLine
    }
}

function Wait-SideCrabHealth {
    <# The post-restart STARTUP BUDGET - how long crabd is given to come up. The retry shape is
       not re-implemented here: each iteration is one Get-SideCrabHealthProbe (read, back off,
       read again - the 0.16.0 helper), and this only decides how many of them the budget pays
       for. Returns that helper's object, so .Ok and .Document read the same everywhere. #>
    param([int] $TimeoutSec = 30, [int] $RetryDelaySec = 1, [scriptblock] $Wait)

    # A hashtable, not the Get-SideCrabHealth verdict object: Test-SideCrabHealthOk then reads
    # it by its documented dictionary branch instead of by property-name luck.
    $probe = { $h = Get-SideCrabHealth -TimeoutSec 2; @{ ok = $h.Ok; version = $h.Version } }

    $perProbeSec = [math]::Max(1, $RetryDelaySec + 2)     # two reads at 2 s, one backoff
    $budget = [int] [math]::Max(1, [math]::Ceiling($TimeoutSec / $perProbeSec))
    $last = $null
    for ($i = 1; $i -le $budget; $i++) {
        $last = Get-SideCrabHealthProbe -Probe $probe -RetryDelaySec $RetryDelaySec -Wait $Wait
        if ($last.Ok) { break }
    }
    $last
}

# ------------------------------------------------------------------------------ run

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
    throw "$RepoRoot is not a git working tree - nothing to pull."
}

Write-Host 'SideCrab update'
Write-Step "repo:    $RepoRoot"

# ---- 1. fast-forward only
if ($SkipPull) {
    Write-Step 'pull:    skipped (-SkipPull)'
} elseif ($PSCmdlet.ShouldProcess($RepoRoot, 'git pull --ff-only')) {
    # THE CLEAN-TREE PREFLIGHT --ff-only does not give you. --ff-only refuses a MERGE and
    # refuses to clobber a modified file the incoming commits touch - it fast-forwards happily
    # over local edits to every other file. So the header's promise that "a dirty tree fails
    # the pull" was simply not true, and an update could move the repo out from under
    # uncommitted work and then restart the tasks onto it. Asked BEFORE the pull, so the
    # answer is "nothing was changed", not "here is what I did to your tree".
    $porcelain = Invoke-Git @('-C', $RepoRoot, 'status', '--porcelain')
    if ($porcelain.ExitCode -ne 0) {
        throw "git status --porcelain failed (exit $($porcelain.ExitCode)). No pull was attempted.`n$($porcelain.Output)"
    }
    $tree = Get-SideCrabPullPreflight -StatusPorcelain $porcelain.Output
    if ($tree.Blocked) {
        throw ("the working tree is DIRTY - $($tree.Reason). No pull was attempted and no task was restarted. " +
               "Commit or stash first (git -C `"$RepoRoot`" stash), or re-run with -SkipPull to restart on the code that is there now.")
    }
    if ($tree.Untracked.Count -gt 0) {
        # Not a block: untracked files survive a fast-forward untouched unless an incoming
        # commit adds that exact path, and git refuses by name when it does.
        Write-Step "tree:    clean of tracked changes; $($tree.Untracked.Count) untracked file(s) present, left alone"
    } else {
        Write-Step 'tree:    clean'
    }
    $before = (Invoke-Git @('-C', $RepoRoot, 'rev-parse', 'HEAD')).Output.Trim()
    $pull   = Invoke-Git @('-C', $RepoRoot, 'pull', '--ff-only')
    if ($pull.ExitCode -ne 0) {
        throw "git pull --ff-only failed (exit $($pull.ExitCode)). No task was restarted.`n$($pull.Output)"
    }
    $after = (Invoke-Git @('-C', $RepoRoot, 'rev-parse', 'HEAD')).Output.Trim()
    if ($before -eq $after) {
        Write-Step "pull:    already up to date ($($after.Substring(0, [Math]::Min(8, $after.Length))))"
    } else {
        $b = $before.Substring(0, [Math]::Min(8, $before.Length))
        $a = $after.Substring(0, [Math]::Min(8, $after.Length))
        Write-Step "pull:    $b -> $a"
    }
}

# ---- 2. restart what is registered, and only that
$spec  = @(Get-SideCrabComponentSpec -RepoRoot $RepoRoot)
$names = @(Get-SideCrabTaskName -Component $spec -All)
$states = @(foreach ($n in $names) { Get-SideCrabTaskState -TaskName $n })
$registered = @($states | Where-Object Registered)
# Task name -> the port that component binds (0 = none). Read off the catalogue, so the restart
# below never has to guess which task is racing a socket.
$portByTask = @{}
foreach ($c in $spec) { $portByTask[$c.TaskName] = [int] $c.Port }

if ($SkipRestart) {
    Write-Step 'tasks:   restart skipped (-SkipRestart)'
} elseif ($registered.Count -eq 0) {
    Write-Step 'tasks:   none registered - run Install-SideCrab.ps1 first'
} else {
    foreach ($s in $registered) {
        if ($s.State -eq 'Disabled') {
            # Same rule the installer follows: a disabled task is a decision (glow parked on
            # the headless SDK crash, docs/BACKLOG.md). Restarting it would start it.
            Write-Step "tasks:   '$($s.TaskName)' disabled - left alone (Enable-ScheduledTask to un-park)"
            continue
        }
        if ($PSCmdlet.ShouldProcess($s.TaskName, 'Restart scheduled task')) {
            # -Port is what makes this wait for the old process to let go of 2722 before the new
            # one tries to bind it; Restart-SideCrabTask THROWS rather than starting blind when
            # it does not come free, which is the whole fix. 0 for a component that owns no port.
            $port = [int] $portByTask[$s.TaskName]
            $r = Restart-SideCrabTask -TaskName $s.TaskName -Port $port
            $waited = if ($null -ne $r.PortWaitSec -and $r.PortWaitSec -gt 0) {
                          " (port $port free after ~$($r.PortWaitSec)s)"
                      } else { '' }
            Write-Step "tasks:   '$($s.TaskName)' restarted (was $($s.State))$waited"
        }
    }
}
foreach ($s in @($states | Where-Object { -not $_.Registered })) {
    Write-Step "tasks:   '$($s.TaskName)' not registered - left alone"
}

# ---- 3. verify: /v1/health AND the task, together
# BOTH, because either one alone lies. Health alone passed a run where a stray process held 2722
# and answered while SideCrab-crabd was dead in Ready (2026-08-27); the task state alone passes a
# Running process that never bound the port.
$verifyFailed = $false
if ($WhatIfPreference) {
    Write-Step 'health:  not polled (-WhatIf)'
} else {
    $crabd      = @($spec | Where-Object { $_.Key -eq 'crabd' })[0]
    $crabdPort  = [int] $crabd.Port
    $probe      = Wait-SideCrabHealth -TimeoutSec $TimeoutSec
    $version    = if ($probe.Document -is [System.Collections.IDictionary]) { "$($probe.Document['version'])" } else { '' }
    # Re-read the task AFTER the wait - the pre-restart reading is exactly the stale fact that
    # made the old check look green.
    $crabdState = Get-SideCrabTaskState -TaskName $crabd.TaskName
    $holder     = @(if ($crabdPort -gt 0) { Get-SideCrabPortHolder -Port $crabdPort })

    if (-not $crabdState.Registered) {
        Write-Step "health:  $($crabd.TaskName) not registered - nothing to verify (run Install-SideCrab.ps1)"
    } elseif ($crabdState.State -eq 'Disabled') {
        Write-Step "health:  $($crabd.TaskName) is DISABLED - not expected to answer"
    } else {
        $verdict = Get-SideCrabServiceVerdict -HealthOk $probe.Ok -TaskState "$($crabdState.State)" `
                                              -LastTaskResult $crabdState.LastTaskResult `
                                              -Holder $holder -Port $crabdPort
        if ($verdict.Ok) {
            Write-Step "health:  ok after restart - crabd $version, $($crabd.TaskName) Running"
        } else {
            $verifyFailed = $true
            Write-Host "  FAIL:    $($verdict.Reason)" -ForegroundColor Red
            if ($verdict.Verdict -eq 'foreign-answerer') {
                Write-Warning ("A HEALTH ANSWER DID NOT COME FROM THIS TASK. $($crabd.TaskName) is " +
                               "$($crabdState.State), so whatever is serving $crabdPort is a foreign process or an " +
                               'orphan of a failed restart. Until it is stopped the task cannot bind and will keep ' +
                               "exiting 1: Get-NetTCPConnection -LocalPort $crabdPort -State Listen  ->  Stop-Process -Id <pid>")
            } else {
                Write-Warning ("crabd did not come up within $TimeoutSec s. Diagnose with: " +
                               'pwsh -File setup\Repair-SideCrab.ps1')
            }
        }
    }
}

$widget = Get-SideCrabWidgetVersion -RepoRoot $RepoRoot
Write-Step "widget:  manifest $(if ($widget) { $widget } else { 'unknown' })"

# Read-only: a pull can move the repo or the icon out from under a registered IconUri, and
# a stale key is invisible until a toast renders with no icon. Re-registering is the
# installer's job, not this script's - this only says so.
# MISSING IS A STATE THIS REPORT MUST NAME. Both loops reported stale and current and said
# nothing at all when a registration was absent - so an AUMID or a scheme that was never
# written (or was removed) produced NO row, and a silent report reads as a clean one. Gated on
# the toast task being registered: a machine without the notifier is not missing anything.
$toastKey        = (Get-SideCrabAumidSpec -RepoRoot $RepoRoot).ComponentKey
$toastTaskName   = @($spec | Where-Object { $_.Key -eq $toastKey })[0].TaskName
$toastRegistered = [bool] @($states | Where-Object { $_.TaskName -eq $toastTaskName -and $_.Registered }).Count

$aumid = Get-SideCrabAumidState -RepoRoot $RepoRoot
if ($aumid.Registered -and -not $aumid.Current) {
    Write-Step "aumid:   $($aumid.Aumid) registered but stale - re-run Install-SideCrab.ps1 (or Register-SideCrabAumid.ps1)"
} elseif ($aumid.Registered) {
    Write-Step "aumid:   $($aumid.Aumid) current"
} elseif ($toastRegistered) {
    Write-Step "aumid:   $($aumid.Aumid) NOT REGISTERED - toasts will be filed under 'Windows PowerShell'; re-run Install-SideCrab.ps1"
}

# Same story, worse symptom: a pull that moves the repo leaves shell\open\command pointing
# at a handler path that no longer exists, and that button then raises a shell
# error instead of doing nothing. Read-only here for the same reason as the AUMID above.
foreach ($proto in @(Get-SideCrabProtocolState -RepoRoot $RepoRoot)) {
    if ($proto.Registered -and -not $proto.Current) {
        Write-Step "proto:   $($proto.Scheme): registered but stale - re-run Install-SideCrab.ps1 (or Register-SideCrabProtocol.ps1)"
    } elseif ($proto.Registered) {
        Write-Step "proto:   $($proto.Scheme): current"
    } elseif ($toastRegistered) {
        # An unregistered scheme is the QUIETEST failure the toast has: the shell no-ops the
        # button and nothing anywhere logs it. Omitting the row was how Snooze shipped inert.
        Write-Step "proto:   $($proto.Scheme): NOT REGISTERED - the $($proto.Button) button will do nothing; re-run Install-SideCrab.ps1"
    }
}

Write-Host ''
Write-Warning 'The WIDGET is not updated by this script. The Xeneon Edge widget updates only by importing the .icuewidget package into iCUE - a repo pull changes widget\ source, not what iCUE is running.'
Write-Host 'Verify with: pwsh -File setup\Test-SideCrab.ps1'
Write-Host 'Done.'

# Exit non-zero when the post-restart check did not stand up. A restart that left nothing
# serving used to end in "Done." and exit 0 - which is how ~6 minutes of dark panel went
# unnoticed on 2026-08-27. The row above says what; this makes it impossible to miss.
exit ([int] $verifyFailed)

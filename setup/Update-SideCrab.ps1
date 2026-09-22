#Requires -Version 7.0
<#
.SYNOPSIS
    Updates the SideCrab companion in place: fast-forward the repo, restart the
    registered SideCrab-* tasks, then wait for /v1/health to come back ok.

.DESCRIPTION
    Four steps, each of them reversible or read-only:
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

    THE PANEL HOST IS STAGED, VALIDATED AND SWAPPED (MF-006), and it is part of the verdict
    (SCA-003). It is a compiled exe, so a pull alone does not change it. When SideCrab-panel is
    registered and enabled, step 4:
      1. publishes the pulled source into panel-host\dist.staging - or, with -Package <zip>,
         unpacks that package's host into it - while the live host stays exactly where it is,
      2. validates the staged binary by running its own `--check`, which shows no window and
         writes no log. Exit 0 and exit 2 both prove it runs; anything else, a crash, or a
         report with no version line is a failed validation,
      3. keeps the host it is replacing as panel-host\dist.last-good (ONE generation) and swaps
         the staged one in by rename,
      4. starts the task and waits for it to be Running.
    Anything that fails AFTER the swap puts dist.last-good back, restarts it, and exits
    non-zero saying what was restored and what is running. Anything that fails BEFORE the swap
    leaves the live host untouched. Restore-SideCrab.ps1 -Host does the same rollback by hand.

    A PACKAGE UPGRADE IS AN INSTALLER RE-RUN. -Package here swaps the panel HOST out of a
    release zip and can roll it back; it does not replace the companion, the panel assets or
    the scripts. To take a whole new package: extract it and run setup\Install-SideCrab.ps1.

    The panel assets crabd serves at /panel/ come from the pulled tree and need no rebuild.

.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1
.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1 -SkipPull      # restart + verify only
.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1 -Package C:\Downloads\SideCrab-0.33.0-win-x64.zip
.EXAMPLE
    pwsh -File .\setup\Update-SideCrab.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot   = (Split-Path -Parent $PSScriptRoot),
    [int]    $TimeoutSec = 30,
    # Stage the panel host out of a release package instead of publishing it from source. The
    # PC then needs no .NET SDK for the update.
    [string] $Package,
    [switch] $SkipPull,
    [switch] $SkipRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$RetiredPath = Join-Path $HOME '.sidecrab\state\retired.json'

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

# A PACKAGE INSTALL IS NOT A CHECKOUT. An extracted release has no .git, so demanding one
# before anything else made this script unusable on exactly the install -Package exists for.
# The working tree is required only when this run is going to pull.
$willPull = (-not $SkipPull) -and (-not $Package)
if ($willPull -and -not (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
    throw "$RepoRoot is not a git working tree - nothing to pull. Pass -SkipPull to restart and verify what is there, or -Package <zip> to swap the panel host out of a release package."
}

Write-Host 'SideCrab update'
Write-Step "repo:    $RepoRoot"

# ---- 1. fast-forward only
if (-not $willPull) {
    Write-Step "pull:    skipped ($(if ($Package) { "-Package $Package" } else { '-SkipPull' }))"
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

# ---- what is registered on this PC (read once; every step below reads these)
$spec  = @(Get-SideCrabComponentSpec -RepoRoot $RepoRoot)
$names = @(Get-SideCrabTaskName -Component $spec -All)
$states = @(foreach ($n in $names) { Get-SideCrabTaskState -TaskName $n })
$registered = @($states | Where-Object Registered)
# Task name -> the port that component binds (0 = none). Read off the catalogue, so the restart
# below never has to guess which task is racing a socket.
$portByTask = @{}
foreach ($c in $spec) { $portByTask[$c.TaskName] = [int] $c.Port }

# ---- 1b. take the panel host down before anything touches its directory
# The python tasks re-read their scripts on restart; the panel task runs a COMPILED exe, so a
# restart alone would run last week's host. It is stopped here and started again in step 4,
# after the staged host has been validated and swapped in: nothing can publish over, rename or
# replace a directory whose executable is running.
$panelSpec  = @($spec | Where-Object { $_.Key -eq 'panel' })[0]
$panelState = Get-SideCrabTaskState -TaskName $panelSpec.TaskName
# The panel is IN SCOPE for this update when its task is registered and not parked. Said once
# here so the staging, the restart and the final verdict cannot disagree about it.
$panelInScope = [bool] ($panelState.Registered -and $panelState.State -ne 'Disabled')
# Read BEFORE anything is staged: on a failure this is the version still on disk, and the
# operator is owed the number rather than "the previous exe".
$hostBefore   = (Get-SideCrabComponentVersion -RepoRoot $RepoRoot -PanelExe $panelSpec.Script).Host
$hostFailed   = $false
$distPath     = Join-Path $RepoRoot 'panel-host\dist'
$stagingPath  = Join-Path $RepoRoot 'panel-host\dist.staging'
$lastGoodPath = Join-Path $RepoRoot 'panel-host\dist.last-good'

if ($SkipRestart) {
    Write-Step 'panel:   host update skipped (-SkipRestart)'
} elseif ($panelInScope) {
    if ($PSCmdlet.ShouldProcess($panelSpec.TaskName, 'Stop the panel host for a staged update')) {
        Stop-ScheduledTask -TaskName $panelSpec.TaskName -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline -and
               @(Get-Process -Name 'SideCrab.Panel' -ErrorAction SilentlyContinue |
                 Where-Object { $_.Path -eq $panelSpec.Script }).Count -gt 0) {
            Start-Sleep -Milliseconds 300
        }
        Write-Step "panel:   $($panelSpec.TaskName) stopped for the staged update (host $hostBefore)"
    }
} else {
    Write-Step "panel:   $($panelSpec.TaskName) not registered or disabled - not updated"
}

# ---- 2. restart what is registered, and only that
# The panel is NOT in this loop: it is started in step 4, after its new host has proved it runs.
# Starting it here would start the OLD exe and then take it down again a moment later.
$others = @($registered | Where-Object { $_.TaskName -ne $panelSpec.TaskName })
if ($SkipRestart) {
    Write-Step 'tasks:   restart skipped (-SkipRestart)'
} elseif ($registered.Count -eq 0) {
    Write-Step 'tasks:   none registered - run Install-SideCrab.ps1 first'
} elseif ($others.Count -eq 0) {
    # The panel is registered and nothing else is. Saying "none registered" here would send
    # someone to re-run the installer over a task that is sitting right there.
    Write-Step 'tasks:   only the panel is registered - it is updated in step 4, not restarted here'
} else {
    foreach ($s in $others) {
        if ($s.State -eq 'Disabled') {
            # Same rule the installer follows: a disabled task is a decision the operator made
            # with Disable-ScheduledTask. Restarting it would start it.
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

# ---- 4. the staged panel host update, with a one-generation rollback (MF-006)
# STAGE, VALIDATE, SWAP, START - in that order, because that order is the fix. The old path
# published straight over panel-host\dist and then started whatever came out: a publish that
# died half-way, or a binary that could not run on this PC, became the live host and the only
# way back was another successful build. Now a new host proves it runs (its own --check) while
# the live one is still on disk, the swap is a rename, and the host it replaced is kept for one
# generation at panel-host\dist.last-good. Anything that fails after the swap puts that back.
if (-not $WhatIfPreference -and -not $SkipRestart -and $panelInScope) {
    $stage = if ($Package) {
        {
            param([string] $StagingPath)
            # A package update replaces the HOST only. The rest of a package (crabd, the panel
            # assets, the scripts) is installed by extracting it and re-running the installer,
            # which is the documented upgrade; this path exists to swap a host and roll it back.
            if (-not (Test-Path -LiteralPath $Package)) { throw "$Package is not there" }
            $tmp = Join-Path ([IO.Path]::GetTempPath()) ('sidecrab-pkg-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
            try {
                Expand-Archive -LiteralPath $Package -DestinationPath $tmp -Force
                $found = @(Get-ChildItem -LiteralPath $tmp -Recurse -Directory -Filter 'dist' |
                           Where-Object { (Split-Path -Leaf (Split-Path -Parent $_.FullName)) -eq 'panel-host' -and
                                          (Test-Path -LiteralPath (Join-Path $_.FullName 'SideCrab.Panel.exe')) })
                if ($found.Count -eq 0) { throw "$Package carries no panel-host\dist\SideCrab.Panel.exe" }
                Copy-Item -LiteralPath $found[0].FullName -Destination $StagingPath -Recurse -Force
            } finally {
                if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
            }
        }
    } else {
        {
            param([string] $StagingPath)
            & (Join-Path $PSScriptRoot 'Build-SideCrabPanel.ps1') -RepoRoot $RepoRoot -OutDir $StagingPath
            if ($LASTEXITCODE -ne 0) { throw "dotnet publish exited $LASTEXITCODE" }
        }
    }

    $staged = Invoke-SideCrabStagedHostUpdate `
                  -DistPath $distPath -StagingPath $stagingPath -LastGoodPath $lastGoodPath `
                  -Stage $stage `
                  -Validate { param([string] $ExePath) Invoke-SideCrabHostCheck -ExePath $ExePath } `
                  -Activate {
                      Start-ScheduledTask -TaskName $panelSpec.TaskName
                      $deadline = (Get-Date).AddSeconds($TimeoutSec)
                      while ((Get-Date) -lt $deadline) {
                          $st = Get-SideCrabTaskState -TaskName $panelSpec.TaskName
                          $run = Get-SideCrabRunStateDecision -Registered ([bool] $st.Registered) -State "$($st.State)"
                          if ($run.Verdict -eq 'running') { return $true }
                          Start-Sleep -Milliseconds 300
                      }
                      $false
                  }

    if ($staged.Ok) {
        Write-Step "panel:   host $hostBefore -> $($staged.Version) staged, validated and live ($($panelSpec.TaskName) Running)"
    } else {
        $hostFailed = $true
        $running = (Get-SideCrabComponentVersion -RepoRoot $RepoRoot -PanelExe $panelSpec.Script).Host
        Write-Host "  FAIL:    the panel host update failed in the $($staged.Phase) step. $($staged.Reason)" -ForegroundColor Red
        Write-Host "           Host version on disk now: $running. Diagnose with: pwsh -File setup\Repair-SideCrab.ps1" -ForegroundColor Red
        if ($staged.RestoredLastGood) {
            # The rollback already put the kept host back; it still has to be STARTED, because
            # the run that failed is the one that stopped it.
            Start-ScheduledTask -TaskName $panelSpec.TaskName -ErrorAction SilentlyContinue
            $after = Get-SideCrabTaskState -TaskName $panelSpec.TaskName
            Write-Host "           Rolled back to the last-good host ($running) and restarted it: $($panelSpec.TaskName) is $($after.State)." -ForegroundColor Yellow
        } elseif ($staged.Swapped) {
            Write-Host "           The new host is live and did not come back, and there was no kept generation to restore. Put one back with: pwsh -File setup\Restore-SideCrab.ps1 -Host" -ForegroundColor Red
        } else {
            Start-ScheduledTask -TaskName $panelSpec.TaskName -ErrorAction SilentlyContinue
            Write-Host "           Nothing was swapped: the host that was running before this update is still in place and has been restarted." -ForegroundColor Yellow
        }
    }
} elseif (-not $WhatIfPreference -and -not $SkipRestart -and -not $panelInScope) {
    Write-Step "panel:   $($panelSpec.TaskName) not registered or disabled - no host update"
}

# The panel host's own outcome is part of THIS verdict, not a separate warning stream: an
# update that left the glass showing something it did not ship has not succeeded (SCA-003).
if ($hostFailed) { $verifyFailed = $true }

# ---- retire the tasks of components this product no longer ships (CLEAN-06)
foreach ($r in @(Invoke-SideCrabRetirement -RepoRoot $RepoRoot -RetiredPath $RetiredPath -WhatIf:$WhatIfPreference)) {
    if ($r.Verdict -eq 'already-retired') { continue }
    Write-Step "retired: $($r.TaskName) - $($r.Detail)"
}

$ver = Get-SideCrabComponentVersion -RepoRoot $RepoRoot -PanelExe $panelSpec.Script
Write-Step "version: crabd $($ver.Crabd)  |  widget $($ver.Widget)  |  host $($ver.Host)"
foreach ($n in @($ver.Notes)) { Write-Host "           $n" -ForegroundColor DarkGray }

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
Write-Host 'Verify with: pwsh -File setup\Test-SideCrab.ps1'
if ($verifyFailed) { Write-Host 'FAILED - see the FAIL line(s) above.' -ForegroundColor Red } else { Write-Host 'Done.' }

# Exit non-zero when ANY of the post-restart checks did not stand up: crabd's health, the
# panel host's build, and the panel task coming back. A restart that left nothing serving used
# to end in "Done." and exit 0 - which is how ~6 minutes of dark panel went unnoticed on
# 2026-08-27 - and a failed host build did the same until SCA-003.
exit ([int] $verifyFailed)

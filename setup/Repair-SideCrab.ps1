#Requires -Version 7.0
<#
.SYNOPSIS
    The SideCrab doctor: diagnoses the failure modes this install has actually hit, in plain
    English, and optionally applies the safe subset of the fixes. REPORT-ONLY by default.

.DESCRIPTION
    Test-SideCrab.ps1 answers "is it working?" with PASS/FAIL rows. This answers the next
    question - "then what is wrong, and what do I type?" - for the specific ways SideCrab has
    broken before. Every check prints what it saw, why that matters, and the exact command.

    THE CHECKS, and the real incident behind each
      crabd health        crabd not answering on 2722 at all. Probed TWICE, a few seconds apart,
                          before it is called a failure: a single GET reads a healthy crabd as
                          dead right after a task restart, and on this host whenever a loopback
                          SYN-ACK is dropped (docs/BACKLOG.md). A reading that only the retry
                          answered is reported as such - recovered, not hidden.
      crabd owns its port an answer on 2722 with SideCrab-crabd NOT Running. Health-by-HTTP
                          cannot tell WHO replied: a stray non-task process held the port and
                          answered convincingly while the task was dead in Ready with
                          LastTaskResult=1. FAIL, with the holding PID named - and while it
                          holds the port the real task cannot bind at all.
      is running          a registered, ENABLED helper task (glow, toast) that is not Running.
                          Every SideCrab task is a logon daemon, so Ready means the process is
                          gone - and nothing used to ask: crabd had the health probe and the
                          other two had only the freshness row, which reported "nothing is
                          executing" as an OK. crabd is deliberately absent here; the two rows
                          above are its liveness.
      stale code          a task saying Running while the process is older than the code it
                          runs. This is the class nothing else catches: the task is Running,
                          /v1/health answers ok, and the fix you just shipped is not in the
                          process. Caught by comparing the NEWEST mtime across the component's
                          watched files (glow's entry point is a launcher that never changes -
                          watching it alone hid every edit to the glow itself) against the
                          task's LastRunTime, and - decisively - crabd's served version against
                          the VERSION in companion\crabd.py.
      wiring paths        hooks, status line, task actions or a toast handler naming a SideCrab
                          path OUTSIDE this checkout. They still run; they run the other copy.
      statusline invoked  our command installed in settings.json but never actually invoked -
                          /v1/health's lastStatuslineAgeSec is null. "Installed" and "arriving"
                          are different questions and only the first one was askable before.
      consumer schemas    the notifier and the glow pin the schemas they accept. crabd moved to
                          4, then 5; each time a consumer stayed behind it kept running, kept
                          polling and never toasted (or lit) again.
      toast identity      the AUMID missing, so toasts group under "Windows PowerShell".
      toast action        a toast scheme missing or stale, so its button does nothing or raises
                          a shell error. One row per scheme: sidecrab-ack: (Acknowledge) and
                          sidecrab-snooze: (Snooze 30m).
      hook allow-list     allowedHttpHookUrls set without our URL - the CLI then never calls the
                          http hooks at all, and panel approvals silently never arm.
      config parse        ~/.sidecrab/config.json unparseable; every consumer falls back to
                          defaults without saying so.

    -Fix APPLIES ONLY THE SAFE SUBSET, each one ShouldProcess-gated:
      * start a registered, ENABLED, not-running task
      * restart a task that is Running on stale code
      * (re-)register the toast AUMID and the toast schemes (sidecrab-ack:, sidecrab-snooze:) -
        HKCU, idempotent, exactly what the installer writes

    EVERY START GOES THROUGH Restart-SideCrabTask, never a bare Start-ScheduledTask: it waits
    for the port to be released and refuses to start rather than losing the bind race. And
    while a foreign process holds 2722 the start fix is not offered at ALL - the port-owner row
    carries that story, and starting into a held port is the 2026-08-27 incident itself.

    A FIX COUNTS ONLY WHEN IT IS VERIFIED. Each fixable row states what "worked" means and that
    is re-measured after the action runs; not throwing is not fixed. Start-ScheduledTask returns
    the moment the scheduler accepts the request, so a process that started and immediately died
    used to be marked fixed, leave the standing count, and exit 0 under a printed
    "still not answering".

    -Fix NEVER:
      * writes settings.json - hooks, statusLine and allowedHttpHookUrls are the operator's
        file; the report prints the command that changes them and stops there
      * writes config.json, and never enables panel approvals - arming approvals is a security
        posture, taken deliberately by Install-SideCrab.ps1 -WithApprovals
      * enables a DISABLED task - a disabled task is a decision (the glow is parked on the
        headless SDK crash, docs/BACKLOG.md), and re-enabling it here would start it into that
        crash. Enable-ScheduledTask is the deliberate override.

    Exit code is 0 when no FAIL row stands (a row fixed in this run counts as handled), 1
    otherwise - so this is CI-safe.

.EXAMPLE
    pwsh -File .\setup\Repair-SideCrab.ps1
.EXAMPLE
    pwsh -File .\setup\Repair-SideCrab.ps1 -Fix -WhatIf     # rehearse the safe fixes
.EXAMPLE
    pwsh -File .\setup\Repair-SideCrab.ps1 -Fix
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot     = (Split-Path -Parent $PSScriptRoot),
    [string] $BaseUri      = 'http://127.0.0.1:2722',
    [string] $SettingsPath = (Join-Path $HOME '.claude\settings.json'),
    [string] $ConfigPath   = (Join-Path $HOME '.sidecrab\config.json'),
    [string] $ChainPath    = (Join-Path $HOME '.sidecrab\statusline-chain.json'),
    # The backoff before the health check's one retry. A single GET can read a healthy crabd as
    # dead right after a restart, or during this host's loopback SYN-ACK drops (docs/BACKLOG.md).
    [int]    $HealthRetryDelaySec = 3,
    [switch] $Fix
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$HookUrlMarker = '127.0.0.1:2722/v1/hook'

$script:Checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    <# One diagnosis. Status drives the colour and the exit code; Why and Command are what
       make the row actionable without reading this script. FixLabel/FixAction are set only
       on the rows -Fix is allowed to touch. #>
    param(
        [Parameter(Mandatory)][string] $Id,
        [Parameter(Mandatory)][string] $Title,
        [Parameter(Mandatory)][ValidateSet('ok', 'warn', 'fail', 'info', 'unknown')][string] $Status,
        [string] $Detail = '',
        [string] $Why = '',
        [string] $Command = '',
        [string] $FixLabel,
        [scriptblock] $FixAction,
        # DID THE FIX WORK? Run after FixAction; its truthiness - not the absence of an
        # exception - is what sets Fixed, and Fixed is what the exit code is computed from.
        # Start-ScheduledTask returns the instant the scheduler accepts the request, so a
        # process that starts and immediately dies "succeeded" every time: the row was marked
        # fixed, the trailing line said "still not answering", and the script exited 0.
        [scriptblock] $FixVerify
    )
    $script:Checks.Add([pscustomobject]@{
        Id = $Id; Title = $Title; Status = $Status; Detail = $Detail; Why = $Why
        Command = $Command; FixLabel = $FixLabel; FixAction = $FixAction
        FixVerify = $FixVerify; Fixed = $false
    })
}

function Get-FileVersionString {
    <# The VERSION literal a python module declares, or $null. Read-only text scan - importing
       crabd to ask it would start a second one. #>
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $text = Get-Content -LiteralPath $Path -Raw -Encoding utf8
    if ($text -match '(?m)^VERSION\s*=\s*"([^"]+)"') { return $Matches[1] }
    $null
}

function Get-HealthDocument {
    <# The raw /v1/health object, or $null. Kept separate from Get-SideCrabHealth because the
       counters this doctor reads (lastStatuslineAgeSec, hooksSeen) are v0.14.0-and-later and
       must be probed for, not assumed. #>
    param([int] $TimeoutSec = 3)
    try { Invoke-RestMethod -Uri "$BaseUri/v1/health" -TimeoutSec $TimeoutSec -ErrorAction Stop }
    catch { $null }
}

function Get-StateDocument {
    param([int] $TimeoutSec = 4)
    try { Invoke-RestMethod -Uri "$BaseUri/v1/state" -TimeoutSec $TimeoutSec -ErrorAction Stop }
    catch { $null }
}

function Get-PermissionRouteState {
    <# Is crabd's /v1/hook/permission route actually there? {Reachable, Status, Is404}. Read-only,
       never throws. Ported from Verify-PanelApproval.ps1's preflight so the doctor asks the SAME
       question: an EMPTY POST carries no session id, so crabd's permission handler returns the
       pass-through {} before it registers, approves or denies anything (companion\crabd.py) - the
       probe is inert. A 404 means this crabd predates panel approval (contract v0.12.0 §4), so an
       approvals-ON posture pointing at it can never be served. Only ever called when approvals are
       ON, so an OFF (mitigated) host is never poked. #>
    param([int] $TimeoutSec = 5)
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUri/v1/hook/permission" -Method Post -Body '{}' `
                                  -ContentType 'application/json' -TimeoutSec $TimeoutSec `
                                  -SkipHttpErrorCheck -ErrorAction Stop
        $status = [int] $resp.StatusCode
        [pscustomobject]@{ Reachable = $true; Status = $status; Is404 = ($status -eq 404) }
    } catch {
        [pscustomobject]@{ Reachable = $false; Status = $null; Is404 = $false }
    }
}

function Test-CrabdIsServing {
    <# Is crabd ACTUALLY serving, re-measured from scratch? The -Fix verification for the
       start row: both halves, because either alone lies - a health answer can come from a
       foreign process and a Running task may never have bound the port. Freshly probed on
       every call; nothing here reads a value measured earlier in the run. #>
    param([int] $Port = 2722, [string] $TaskName = 'SideCrab-crabd')

    $probe = Get-SideCrabHealthProbe -Probe { Get-HealthDocument } -RetryDelaySec $HealthRetryDelaySec
    $st    = Get-SideCrabTaskState -TaskName $TaskName
    [bool] (Get-SideCrabServiceVerdict -HealthOk $probe.Ok -TaskState "$($st.State)" `
                                       -LastTaskResult $st.LastTaskResult `
                                       -Holder @(Get-SideCrabPortHolder -Port $Port) -Port $Port).Ok
}

function Test-HasProperty {
    param($Object, [string] $Name)
    [bool] ($Object -and (@($Object.PSObject.Properties.Name) -contains $Name))
}

function Get-HookWiringPath {
    <# The filesystem paths SideCrab's own hook entries name in settings.json. Pure.

       ENTRY-LEVEL, NOT MATCHER-LEVEL: a matcher-level ownership test ("this matcher carries
       a crabd hook") also sweeps in every OTHER hook in that matcher, so a hook a human
       hand-merged into one of ours got its paths attributed to SideCrab - and a doctor that
       reports someone else's checkout as our stray wiring sends the operator to re-run the
       installer over a hook the installer does not own. See Split-SideCrabHookMatcher. #>
    param($Settings, [string] $Marker = '127.0.0.1:2722/v1/hook')

    $found = @()
    if ($null -eq $Settings -or $Settings -isnot [System.Collections.IDictionary]) { return $found }
    if (-not $Settings.Contains('hooks') -or $Settings['hooks'] -isnot [System.Collections.IDictionary]) { return $found }

    foreach ($ev in @($Settings['hooks'].Keys)) {
        foreach ($m in @($Settings['hooks'][$ev])) {
            $ours = (Split-SideCrabHookMatcher -Matcher $m -Marker $Marker).Ours
            if ($null -eq $ours) { continue }
            foreach ($h in @($ours['hooks'])) {
                foreach ($p in @(Get-SideCrabCommandPath -Command "$($h['command'])")) {
                    $found += [pscustomobject]@{ Source = "settings.json hooks/$ev"; Path = $p }
                }
            }
        }
    }
    $found      # unrolled - see Get-SideCrabCommandPath; every caller wraps in @()
}

# The restart used to live here as a second copy of Update-SideCrab.ps1's. It is now the shared
# Restart-SideCrabTask in SideCrab.Common.ps1 - one path, so a port-race fix cannot land in one
# script and miss the other (which is what happened).

# ------------------------------------------------------------------------------ diagnose

Write-Host 'SideCrab doctor'
Write-Host "  repo:    $RepoRoot"
Write-Host "  crabd:   $BaseUri"
Write-Host ''

$spec     = @(Get-SideCrabComponentSpec -RepoRoot $RepoRoot)
$states   = @{}
foreach ($c in $spec) { $states[$c.Key] = Get-SideCrabTaskState -TaskName $c.TaskName }
# One reading, one retry: see Get-SideCrabHealthProbe. $health is the freshest document either
# attempt produced, so the checks below (crabd version, statusline age) read the recovered one
# rather than the dropped handshake.
$healthProbe = Get-SideCrabHealthProbe -Probe { Get-HealthDocument } -RetryDelaySec $HealthRetryDelaySec
$health   = $healthProbe.Document
$state    = Get-StateDocument
$settings = $null
$settingsError = $null
try { $settings = Read-SideCrabSettings -SettingsPath $SettingsPath } catch { $settingsError = $_.Exception.Message }

# WHO HOLDS 2722, read BEFORE the health row is built. The start fix below is gated on it:
# starting the task while a foreign process holds the port is exactly the 2026-08-27 incident -
# the new instance loses the bind, exits 1, and the task parks in Ready looking freshly broken.
$crabdPort   = [int] ([uri] $BaseUri).Port
$crabdHolder = @(Get-SideCrabPortHolder -Port $crabdPort)

# -- 1. is crabd answering -------------------------------------------------------------
$crabdTask = $states['crabd']
# "no answer" now means BOTH attempts went unanswered - said out loud, because "I ran the doctor
# and it said FAIL" has to mean something different from a single dropped handshake.
$noAnswer = if ($healthProbe.Attempts -gt 1) { "no answer on $BaseUri after $($healthProbe.Attempts) attempts $($healthProbe.DelaySec)s apart" }
            else                             { "no answer on $BaseUri" }
if ($healthProbe.Ok) {
    # A recovery is reported, never swallowed: two attempts to answer is a symptom in its own
    # right (a restarting task, or the loopback drops in docs/BACKLOG.md), and hiding it would
    # make this row a worse test than the single GET it replaced.
    $recovered = if ($healthProbe.RecoveredOnRetry) {
                     " - RECOVERED ON RETRY: the first probe got no answer, the retry $($healthProbe.DelaySec)s later did (restarting task, or this host's loopback SYN-ACK drops - docs/BACKLOG.md)"
                 } else { '' }
    Add-Check -Id 'health' -Title 'crabd answering' -Status 'ok' `
              -Detail "$BaseUri ok, version $(if (Test-HasProperty $health 'version') { $health.version } else { 'unknown' })$recovered"
} elseif (-not $crabdTask.Registered) {
    Add-Check -Id 'health' -Title 'crabd answering' -Status 'fail' `
              -Detail "$noAnswer and SideCrab-crabd is not registered" `
              -Why 'nothing is running crabd - the widget, the notifier and the glow all read it, so every one of them is dark.' `
              -Command 'pwsh -File setup\Install-SideCrab.ps1'
} elseif ($crabdTask.State -eq 'Disabled') {
    Add-Check -Id 'health' -Title 'crabd answering' -Status 'fail' `
              -Detail 'SideCrab-crabd is DISABLED' `
              -Why 'a disabled task is a decision this script will not overturn - enabling it is yours to do.' `
              -Command 'Enable-ScheduledTask -TaskName SideCrab-crabd; Start-ScheduledTask -TaskName SideCrab-crabd'
} else {
    $lastResult = if ($null -ne $crabdTask.LastTaskResult) {
                      '0x{0:X8}' -f ([int64] $crabdTask.LastTaskResult -band 0xFFFFFFFFL)
                  } else { 'n/a' }
    # A BARE Start-ScheduledTask IS THE INCIDENT. If something else is on 2722 - answering or
    # not - the started process cannot bind, exits 1 and parks the task back in Ready, and the
    # doctor has "fixed" nothing. Two guards: the fix is not offered at all while the port is
    # held (the port-owner row below carries that story and its command), and the fix that IS
    # offered goes through Restart-SideCrabTask, which waits for the port and THROWS rather
    # than starting blind.
    $portHeld = ($crabdHolder.Count -gt 0)
    $crabdName = $crabdTask.TaskName
    $fixLabel  = if ($portHeld) { $null } else { "start $crabdName (waits for port $crabdPort)" }
    $fixAction = if ($portHeld) { $null } else {
                     [scriptblock]::Create("Restart-SideCrabTask -TaskName '$crabdName' -Port $crabdPort | Out-Null")
                 }
    # Health AND the task, the same pair the updater verifies on - a start that crashed on its
    # way up must not read as a fix. A named function called from a plain block, NOT a
    # here-string through [scriptblock]::Create (which only fails to parse once -Fix runs) and
    # NOT .GetNewClosure() (which rebinds the block to a module scope where this script's own
    # Get-HealthDocument does not resolve).
    $fixVerify = if ($portHeld) { $null } else {
                     { Test-CrabdIsServing -Port $crabdPort -TaskName $crabdTask.TaskName }
                 }
    $detail = "$noAnswer; task is $($crabdTask.State), last result $lastResult"
    if ($portHeld) { $detail += " - and $(Format-SideCrabPortHolder -Holder $crabdHolder -Port $crabdPort) still holds the port" }
    Add-Check -Id 'health' -Title 'crabd answering' -Status 'fail' `
              -Detail $detail `
              -Why "the task exists but nothing is serving $BaseUri - the process died, or it never bound the port." `
              -Command $(if ($portHeld) {
                             "Get-NetTCPConnection -LocalPort $crabdPort -State Listen | Select-Object OwningProcess   # then: Stop-Process -Id <pid>; Start-ScheduledTask -TaskName $crabdName"
                         } else {
                             "pwsh -File setup\Update-SideCrab.ps1 -SkipPull   # restarts the registered tasks"
                         }) `
              -FixLabel $fixLabel -FixAction $fixAction -FixVerify $fixVerify
}

# -- 1b. WHO is answering: the health answer and the task, together --------------------
# Health-by-HTTP cannot tell who replied. Measured 2026-08-27: a stray non-task process held
# 2722 and answered /v1/health convincingly while SideCrab-crabd was dead in Ready with
# LastTaskResult=1 - and every check that asked only "does it answer?" reported green.
$owner = Get-SideCrabServiceVerdict -HealthOk $healthProbe.Ok -TaskState "$($crabdTask.State)" `
                                    -LastTaskResult $crabdTask.LastTaskResult `
                                    -Holder $crabdHolder -Port $crabdPort
if (-not $crabdTask.Registered) {
    Add-Check -Id 'port-owner' -Title 'crabd owns its port' -Status 'info' `
              -Detail 'SideCrab-crabd not registered - no task can own the port'
} elseif ($owner.Verdict -eq 'ok') {
    Add-Check -Id 'port-owner' -Title 'crabd owns its port' -Status 'ok' `
              -Detail "$($owner.Reason) - $(Format-SideCrabPortHolder -Holder $crabdHolder -Port $crabdPort)"
} elseif ($owner.Verdict -eq 'foreign-answerer') {
    # The only row here that is its own FAIL. The other two are the 'crabd answering' row's
    # story, and repeating them as a second FAIL would make one fault read as two.
    Add-Check -Id 'port-owner' -Title 'crabd owns its port' -Status 'fail' `
              -Detail $owner.Reason `
              -Why ('a health answer is not proof SideCrab is up - it is proof SOMETHING is on the port. While that ' +
                    'process holds it the real task cannot bind, so every restart exits 1 and parks the task in ' +
                    'Ready. -Fix will not kill it: stopping an unidentified process is the operator''s call.') `
              -Command ("Get-NetTCPConnection -LocalPort $crabdPort -State Listen | Select-Object OwningProcess   # then: " +
                        "Stop-Process -Id <pid>; Start-ScheduledTask -TaskName $($crabdTask.TaskName)")
} else {
    Add-Check -Id 'port-owner' -Title 'crabd owns its port' -Status 'warn' `
              -Detail "$($owner.Reason) - see the 'crabd answering' row above for the fix" `
              -Why 'reported separately from the health row so "nothing answers" and "the wrong thing answers" can never be confused.'
}

# -- 2. Running, but on stale code -----------------------------------------------------
# The one class that hides behind a green task and a green health check.
$fileVersion = Get-FileVersionString -Path (Join-Path $RepoRoot 'companion\crabd.py')
foreach ($c in $spec) {
    $st = $states[$c.Key]
    if (-not $st.Registered) {
        Add-Check -Id "stale-$($c.Key)" -Title "$($c.Key) code freshness" -Status 'info' `
                  -Detail "$($c.TaskName) not registered"
        continue
    }
    if ($st.State -eq 'Disabled') {
        Add-Check -Id "stale-$($c.Key)" -Title "$($c.Key) code freshness" -Status 'info' `
                  -Detail "$($c.TaskName) disabled on purpose - nothing to be stale" `
                  -Why 'a disabled task is a stated decision (the glow is parked on the headless SDK crash, docs/BACKLOG.md); -Fix never enables one.'
        continue
    }
    # The NEWEST of the component's watched files, not its entry point. glow's entry point is a
    # 26-line launcher that never changes, so watching it alone made every edit to
    # sidecrab_glow.py / icue.py / decision.py invisible to this check.
    $watched     = Get-SideCrabWatchedWriteTime -Path $c.WatchFiles
    $scriptWrite = $watched.WriteTime
    $reported = if ($c.Key -eq 'crabd' -and $health -and (Test-HasProperty $health 'version')) { "$($health.version)" } else { '' }
    $onDisk   = if ($c.Key -eq 'crabd') { "$fileVersion" } else { '' }

    $verdict = Get-SideCrabStaleCodeDecision -State "$($st.State)" -LastRunTime $st.LastRunTime `
                                             -ScriptWriteTime $scriptWrite `
                                             -ReportedVersion $reported -FileVersion $onDisk
    if (-not $verdict.Stale) {
        # 'not-running' is NOT an ok: nothing is executing, so nothing can be stale, and
        # printing that green is how a dead toast task read healthy. The run-state row below
        # owns that fault - this one just declines to claim it is fine.
        $status = if ($verdict.Verdict -eq 'not-running') { 'info' } else { 'ok' }
        $seen   = if ($watched.Path) { " (newest of $($watched.Checked): $(Split-Path -Leaf $watched.Path))" } else { '' }
        Add-Check -Id "stale-$($c.Key)" -Title "$($c.Key) code freshness" -Status $status `
                  -Detail "$($c.TaskName) $($st.State) - $($verdict.Reason)$seen"
        continue
    }
    $taskName = $c.TaskName
    $taskPort = [int] $c.Port
    Add-Check -Id "stale-$($c.Key)" -Title "$($c.Key) code freshness" -Status 'fail' `
              -Detail "$taskName is Running on STALE code - $($verdict.Reason)$(if ($watched.Path) { " (newest: $(Split-Path -Leaf $watched.Path))" })" `
              -Why ('the task reads Running and health reads ok, but the process started before the current file was written. ' +
                    'Whatever was changed since is not in the running process - including any fix you shipped to close this very symptom.') `
              -Command "pwsh -File setup\Update-SideCrab.ps1 -SkipPull   # restarts the registered tasks" `
              -FixLabel "restart $taskName" `
              -FixAction ([scriptblock]::Create("Restart-SideCrabTask -TaskName '$taskName' -Port $taskPort | Out-Null")) `
              -FixVerify ([scriptblock]::Create("(Get-SideCrabTaskState -TaskName '$taskName').State -eq 'Running'"))
}

# -- 2b. registered, enabled, and NOT RUNNING ------------------------------------------
# Every SideCrab task is a logon daemon - AtLogOn, no execution time limit, restart x3 - so
# Ready means the process is gone. Nothing asked: crabd had the health probe, and glow and
# toast had only the freshness row, which answered "task is Ready - nothing is executing" as
# an OK. A toast task that died at logon read GREEN and only crabd was ever offered a start.
# crabd is excluded ON PURPOSE: the health and port-owner rows above are its liveness, and a
# third row for the same fault would make one fault read as three.
foreach ($c in @($spec | Where-Object { $_.Key -ne 'crabd' })) {
    $st  = $states[$c.Key]
    $run = Get-SideCrabRunStateDecision -Registered $st.Registered -State "$($st.State)"
    if (-not $run.Fault) {
        Add-Check -Id "running-$($c.Key)" -Title "$($c.Key) is running" `
                  -Status $(if ($run.Verdict -eq 'running') { 'ok' } else { 'info' }) `
                  -Detail "$($c.TaskName) - $($run.Reason)"
        continue
    }
    $taskName = $c.TaskName
    $taskPort = [int] $c.Port
    Add-Check -Id "running-$($c.Key)" -Title "$($c.Key) is running" -Status 'fail' `
              -Detail "$taskName - $($run.Reason)" `
              -Why ("this task is registered and enabled, so it is meant to be up: it starts at logon and is never " +
                    "stopped on purpose. Registered is not running - $($c.Key) does nothing at all in this state, and " +
                    'nothing else in this report would have said so.') `
              -Command "Start-ScheduledTask -TaskName $taskName" `
              -FixLabel "start $taskName" `
              -FixAction ([scriptblock]::Create("Restart-SideCrabTask -TaskName '$taskName' -Port $taskPort | Out-Null")) `
              -FixVerify ([scriptblock]::Create("(Get-SideCrabTaskState -TaskName '$taskName').State -eq 'Running'"))
}

# -- 3. wiring that names another checkout ---------------------------------------------
$wiring = @()
foreach ($c in $spec) {
    $t = Get-ScheduledTask -TaskName $c.TaskName -ErrorAction SilentlyContinue
    if (-not $t) { continue }
    foreach ($a in @($t.Actions)) {
        foreach ($p in @(Get-SideCrabCommandPath -Command "$($a.Arguments)")) {
            $wiring += [pscustomobject]@{ Source = "task $($c.TaskName)"; Path = $p }
        }
    }
}
$slSpec = Get-SideCrabStatusLineSpec -RepoRoot $RepoRoot
$slCmd  = if ($null -ne $settings -and $settings.Contains('statusLine') -and
               $settings['statusLine'] -is [System.Collections.IDictionary]) {
              "$($settings['statusLine']['command'])"
          } else { '' }
foreach ($p in @(Get-SideCrabCommandPath -Command $slCmd)) {
    $wiring += [pscustomobject]@{ Source = 'settings.json statusLine'; Path = $p }
}
$wiring += @(Get-HookWiringPath -Settings $settings -Marker $HookUrlMarker)
$protoStates = @(Get-SideCrabProtocolState -RepoRoot $RepoRoot)
foreach ($ps in $protoStates) {
    if (-not $ps.Command) { continue }
    foreach ($p in @(Get-SideCrabCommandPath -Command $ps.Command)) {
        $wiring += [pscustomobject]@{ Source = "$($ps.Scheme): command"; Path = $p }
    }
}
$strays = @($wiring | Where-Object { (Get-SideCrabPathOwnership -Path $_.Path -RepoRoot $RepoRoot) -eq 'foreign-checkout' })
if ($strays.Count -eq 0) {
    Add-Check -Id 'wiring-paths' -Title 'wiring points here' -Status 'ok' `
              -Detail "$(@($wiring).Count) path(s) checked, all inside this checkout or unrelated"
} else {
    Add-Check -Id 'wiring-paths' -Title 'wiring points here' -Status 'fail' `
              -Detail (($strays | ForEach-Object { "$($_.Source) -> $($_.Path)" }) -join '; ') `
              -Why ('this wiring names a SideCrab path in ANOTHER checkout. It is not broken - it runs the other copy, so a change ' +
                    'shipped here never takes effect and an uninstall here leaves the other one running.') `
              -Command "pwsh -File setup\Install-SideCrab.ps1 -RepoRoot `"$RepoRoot`"   # re-points task actions and the status line"
}

# -- 4. the status line is installed AND arriving --------------------------------------
if (-not (Test-SideCrabStatusLineIsOurs -Command $slCmd)) {
    $why = if ($slCmd) { 'a non-SideCrab status line is configured' } else { 'no status line is configured' }
    Add-Check -Id 'statusline' -Title 'status line arriving' -Status 'fail' `
              -Detail $why `
              -Why 'without it crabd never receives the usage document, so limits fall back to the OAuth read-around.' `
              -Command 'pwsh -File setup\Install-SideCrab.ps1'
} elseif ($health -and (Test-HasProperty $health 'lastStatuslineAgeSec')) {
    if ($null -eq $health.lastStatuslineAgeSec) {
        Add-Check -Id 'statusline' -Title 'status line arriving' -Status 'warn' `
                  -Detail 'installed, but crabd has NEVER received a status-line document' `
                  -Why ('null is "never posted", not "posted a while ago" - the difference between misconfigured and idle. ' +
                        'Known on this project: the status line appears to render only in an interactive terminal session, ' +
                        'not an app-hosted one (docs/BACKLOG.md), so limits stay on the OAuth path.') `
                  -Command 'run a plain `claude` session in a terminal, then re-check'
    } else {
        Add-Check -Id 'statusline' -Title 'status line arriving' -Status 'ok' `
                  -Detail "last document $($health.lastStatuslineAgeSec)s ago"
    }
} else {
    # /v1/health gained the counters in crabd 0.14.0. An older crabd cannot answer this, and
    # guessing from limits.source would report "fine" for a feed that never arrived.
    $corroborate = if ($state -and (Test-HasProperty $state 'limits') -and (Test-HasProperty $state.limits 'source')) {
                       " (limits.source is currently '$($state.limits.source)')"
                   } else { '' }
    Add-Check -Id 'statusline' -Title 'status line arriving' -Status 'unknown' `
              -Detail "installed; this crabd's /v1/health does not report lastStatuslineAgeSec$corroborate" `
              -Why 'the health counters landed in crabd 0.14.0 - an older running process cannot answer whether the feed arrives.' `
              -Command 'pwsh -File setup\Update-SideCrab.ps1 -SkipPull   # restart crabd onto the current code, then re-run'
}

# -- 5. consumer schema pins vs what crabd serves --------------------------------------
$liveSchema = if ($state -and (Test-HasProperty $state 'schema')) { [int] $state.schema } else { $null }
foreach ($consumer in @(
    [pscustomobject]@{ Key = 'notifier'; File = 'notifier\sidecrab_toast.py'; Pin = 'SUPPORTED_SCHEMAS'; Symptom = 'it will never toast again' }
    [pscustomobject]@{ Key = 'glow';     File = 'lighting\decision.py';       Pin = 'ACCEPTED_SCHEMAS';  Symptom = 'it will never light again' }
)) {
    $path = Join-Path $RepoRoot $consumer.File
    if (-not (Test-Path -LiteralPath $path)) {
        Add-Check -Id "schema-$($consumer.Key)" -Title "$($consumer.Key) schema pin" -Status 'info' -Detail 'component not present'
        continue
    }
    if ($null -eq $liveSchema) {
        Add-Check -Id "schema-$($consumer.Key)" -Title "$($consumer.Key) schema pin" -Status 'unknown' `
                  -Detail 'no /v1/state to compare against'
        continue
    }
    $text = Get-Content -LiteralPath $path -Raw -Encoding utf8
    if ($text -notmatch "$($consumer.Pin)\s*=\s*frozenset\(\{([0-9,\s]+)\}\)") {
        Add-Check -Id "schema-$($consumer.Key)" -Title "$($consumer.Key) schema pin" -Status 'fail' `
                  -Detail "could not read $($consumer.Pin) from $($consumer.File)" `
                  -Why 'the pin is the only thing standing between a schema bump and a silently dead consumer; if it cannot be read it cannot be checked.'
        continue
    }
    $accepted = @($Matches[1] -split ',' | Where-Object { $_.Trim() } | ForEach-Object { [int] $_.Trim() })
    if ($accepted -contains $liveSchema) {
        Add-Check -Id "schema-$($consumer.Key)" -Title "$($consumer.Key) schema pin" -Status 'ok' `
                  -Detail "crabd serves $liveSchema; accepts $($accepted -join ',')"
    } else {
        Add-Check -Id "schema-$($consumer.Key)" -Title "$($consumer.Key) schema pin" -Status 'fail' `
                  -Detail "crabd serves $liveSchema; $($consumer.Key) accepts $($accepted -join ',') - $($consumer.Symptom)" `
                  -Why ('the consumer keeps running and keeps polling; it just discards every document. A Running task is not a test ' +
                        'that notifications work.') `
                  -Command "add $liveSchema to $($consumer.Pin) in $($consumer.File), then restart the task"
    }
}

# -- 6/7. the two HKCU registrations ---------------------------------------------------
$toastInstalled = $states['toast'].Registered
$aumid = Get-SideCrabAumidState -RepoRoot $RepoRoot
if (-not $toastInstalled) {
    Add-Check -Id 'aumid' -Title 'toast identity' -Status 'info' -Detail 'toast component not installed'
} elseif ($aumid.Current) {
    Add-Check -Id 'aumid' -Title 'toast identity' -Status 'ok' -Detail "$($aumid.Aumid) registered and current"
} else {
    $detail = if (-not $aumid.Registered)      { "$($aumid.Aumid) not registered" }
              elseif (-not $aumid.IconPresent) { "$($aumid.Aumid) registered but the icon FILE is missing ($($aumid.IconPath))$(if ($aumid.IconUri) { ' - the registered IconUri is a dead pointer' })" }
              else                             { "$($aumid.Aumid) registered but its values differ from this repo's" }
    Add-Check -Id 'aumid' -Title 'toast identity' -Status 'warn' -Detail $detail `
              -Why 'without our own AppUserModelID the notifier borrows Windows PowerShell''s, so Action Center files SideCrab''s toasts under "Windows PowerShell" - and Windows'' per-app notification switch for SideCrab is really PowerShell''s.' `
              -Command 'pwsh -File setup\Register-SideCrabAumid.ps1' `
              -FixLabel 'register the toast AUMID (HKCU)' -FixAction {
                  Set-SideCrabAumid -RepoRoot $RepoRoot | Out-Null
              } -FixVerify {
                  (Get-SideCrabAumidState -RepoRoot $RepoRoot).Current
              }
}

# One row per scheme: Acknowledge and Snooze are separate handlers behind separate schemes, and
# a single row would report the first one's state as if it were both.
foreach ($ps in $protoStates) {
    $id = "protocol-$($ps.Key)"
    if (-not $toastInstalled) {
        Add-Check -Id $id -Title "toast action ($($ps.Key))" -Status 'info' -Detail 'toast component not installed'
        continue
    }
    if ($ps.Current -and $ps.HandlerPresent -and $ps.CarriesArgument) {
        Add-Check -Id $id -Title "toast action ($($ps.Key))" -Status 'ok' -Detail "$($ps.Scheme): registered and current"
        continue
    }
    $why = @()
    if (-not $ps.Registered)      { $why += 'not registered' }
    if (-not $ps.HandlerPresent)  { $why += "handler missing at $($ps.HandlerPath)" }
    if ($ps.Registered -and -not $ps.CarriesArgument) { $why += 'command has no "%1" - the handler is launched with no URI' }
    if ($ps.Registered -and -not $ps.Current) { $why += 'command differs from this repo''s' }
    $canFix = $ps.HandlerPresent
    $scheme = $ps.Scheme
    Add-Check -Id $id -Title "toast action ($($ps.Key))" -Status 'warn' `
              -Detail "$($ps.Scheme): $($why -join '; ')" `
              -Why "the toast's $($ps.Button) button activates this scheme; unregistered it does nothing, stale it raises a shell error." `
              -Command 'pwsh -File setup\Register-SideCrabProtocol.ps1' `
              -FixLabel $(if ($canFix) { "register the $($ps.Scheme): scheme (HKCU)" } else { $null }) `
              -FixAction $(if ($canFix) { [scriptblock]::Create("Set-SideCrabProtocol -RepoRoot `$RepoRoot -Scheme '$scheme' | Out-Null") } else { $null }) `
              -FixVerify $(if ($canFix) { [scriptblock]::Create("[bool] @(Get-SideCrabProtocolState -RepoRoot `$RepoRoot | Where-Object { `$_.Scheme -eq '$scheme' -and `$_.Current -and `$_.CarriesArgument }).Count") } else { $null })
}

# -- 8. allowedHttpHookUrls ------------------------------------------------------------
$permUrl  = "$BaseUri/v1/hook/permission"
$stopUrl  = "$BaseUri/v1/hook/stop"
$patterns = if ($null -ne $settings -and $settings.Contains('allowedHttpHookUrls')) { @($settings['allowedHttpHookUrls']) } else { $null }
if ($null -eq $patterns) {
    Add-Check -Id 'allowlist' -Title 'http hook allow-list' -Status 'ok' -Detail 'allowedHttpHookUrls unset - every URL is allowed'
} else {
    $blocked = @(@($permUrl, $stopUrl) | Where-Object { -not (Test-SideCrabHookUrlAllowed -Url $_ -Patterns $patterns) })
    if ($blocked.Count -eq 0) {
        Add-Check -Id 'allowlist' -Title 'http hook allow-list' -Status 'ok' `
                  -Detail "set ($($patterns.Count) pattern(s)) and admits our URLs"
    } else {
        Add-Check -Id 'allowlist' -Title 'http hook allow-list' -Status 'fail' `
                  -Detail "allowedHttpHookUrls is set and does NOT admit: $($blocked -join ', ')" `
                  -Why ('an allow-list that misses our URL means the CLI never calls the http hook at all - the Stop nudge and panel ' +
                        'approvals silently never arm, with nothing in any log to say so.') `
                  -Command "add `"$BaseUri/*`" to allowedHttpHookUrls in $SettingsPath   # settings.json is yours - this script does not write it"
    }
}

# -- 8b. panel-approval posture (security-relevant when ON) ----------------------------
# panelApprovals is OFF by default and OFF is a valid, safe state - reported so the operator can
# always SEE the posture, never judged. ON is different: taps on the on-glass widget then decide
# REAL tool-call permissions, so an ON posture is security-relevant AND is only armed when the
# PermissionRequest hook actually reaches crabd. Config saying "taps decide" while the hook is
# absent, blocked, or its route 404s is the silent failure worth catching - no prompt ever reaches
# the panel and NOTHING says so, while the operator believes it is armed. Verified the way
# Test-SideCrab step 10 and Verify-PanelApproval.ps1 do: hook wired, admitted by the allow-list,
# and the route present. -Fix NEVER changes the posture - enabling/disabling approvals is the
# operator's call (Install-SideCrab.ps1 -WithApprovals), never the doctor's.
$pa          = Get-SideCrabPanelApprovalsState -ConfigPath $ConfigPath
$permWired   = [bool] @(@(Get-SideCrabHookEvent -Settings $settings -Marker $HookUrlMarker) |
                        Where-Object { $_.Event -eq 'PermissionRequest' })
$permAllowed = Test-SideCrabHookUrlAllowed -Url $permUrl -Patterns $patterns
$wiringMsg   = "PermissionRequest hook $(if ($permWired) { 'wired' } else { 'NOT wired' })" +
               $(if (-not $permAllowed) { '; BLOCKED by allowedHttpHookUrls' } else { '' })
if (-not $pa.Enabled) {
    # OFF (or the key absent = default OFF). Named, not a bare word: the wiring is reported too, so
    # the row reads the same whether approvals are off by choice or off as today's SEC-a mitigation.
    $paMsg = if ($null -eq $pa.Enabled) { 'OFF (default; key absent)' } else { 'OFF' }
    Add-Check -Id 'panel-approvals' -Title 'panel approvals' -Status 'info' `
              -Detail "$paMsg - widget taps CANNOT decide tool permissions; $wiringMsg"
} elseif (-not ($permWired -and $permAllowed)) {
    # Armed in config, but the prompt cannot reach the panel. The one FAIL this row exists for.
    Add-Check -Id 'panel-approvals' -Title 'panel approvals' -Status 'fail' `
              -Detail "ON but $wiringMsg - approvals silently never arm" `
              -Why ('config says widget taps decide real tool-call permissions, but the PermissionRequest hook does not ' +
                    'reach crabd, so no prompt ever arrives at the panel and no log says so. The operator believes it is armed.') `
              -Command "add `"$BaseUri/*`" to allowedHttpHookUrls and re-merge the hook: pwsh -File setup\Install-SideCrab.ps1   # settings.json is yours - this script does not write it"
} else {
    # Hook is wired and admitted - now the route itself. A 404 is a crabd too old to serve it.
    $route = Get-PermissionRouteState
    if ($route.Reachable -and $route.Is404) {
        Add-Check -Id 'panel-approvals' -Title 'panel approvals' -Status 'fail' `
                  -Detail "ON and $wiringMsg, but $permUrl returns 404 - this crabd predates panel approval" `
                  -Why 'the hook is wired but the running crabd has no permission route, so an armed approval can never be served.' `
                  -Command 'pwsh -File setup\Update-SideCrab.ps1 -SkipPull   # restart crabd onto the current code'
    } elseif (-not $route.Reachable) {
        # crabd is not answering at all - the health row above already owns that fault; here it just
        # means the ON wiring cannot be confirmed live, not that it is broken.
        Add-Check -Id 'panel-approvals' -Title 'panel approvals' -Status 'warn' `
                  -Detail "ON and $wiringMsg, but the permission route could not be probed - see the 'crabd answering' row above" `
                  -Why 'approvals are armed in config, but crabd is not answering, so the route cannot be confirmed right now.'
    } else {
        # ON, wired, admitted, and the route serves. Security-relevant, so never invisible: an INFO
        # that says out loud that taps are live (the same wording Verify-PanelApproval uses).
        Add-Check -Id 'panel-approvals' -Title 'panel approvals' -Status 'info' `
                  -Detail "ON - taps decide real permissions; $wiringMsg, $permUrl reachable"
    }
}

# -- 9. config.json parses -------------------------------------------------------------
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Add-Check -Id 'config' -Title 'config.json' -Status 'ok' -Detail "$ConfigPath absent - documented first-run state, defaults apply"
} else {
    try {
        $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8
        if (-not $raw.Trim()) { throw 'file is empty' }
        $cfg = $raw | ConvertFrom-Json -AsHashtable -Depth 40
        if ($cfg -isnot [System.Collections.IDictionary]) { throw 'not a JSON object' }
        Add-Check -Id 'config' -Title 'config.json' -Status 'ok' -Detail "parses; keys: $(@($cfg.Keys) -join ', ')"
    } catch {
        Add-Check -Id 'config' -Title 'config.json' -Status 'fail' `
                  -Detail "unparseable - $($_.Exception.Message)" `
                  -Why ('every consumer falls back to its defaults without saying so: quiet hours stop applying, the toast threshold ' +
                        'reverts, and panelApprovals reads as OFF whatever it said. Nothing logs it.') `
                  -Command "inspect $ConfigPath (rename it to start clean - crabd rewrites it on the next POST /v1/config)"
    }
}

# also worth saying out loud: settings.json itself
if ($settingsError) {
    Add-Check -Id 'settings' -Title 'settings.json' -Status 'fail' `
              -Detail "$SettingsPath unreadable - $settingsError" `
              -Why 'no hook this install wired can be read by the CLI, and neither Install nor Uninstall can safely rewrite it.' `
              -Command 'pwsh -File setup\Restore-SideCrab.ps1        # list the installer''s backups, then -Latest'
}

# ------------------------------------------------------------------------------ report

$order  = @{ fail = 0; warn = 1; unknown = 2; ok = 3; info = 4 }
$colour = @{ fail = 'Red'; warn = 'Yellow'; unknown = 'Yellow'; ok = 'Green'; info = 'DarkGray' }
foreach ($c in @($script:Checks | Sort-Object { $order[$_.Status] })) {
    Write-Host ('  {0,-7} {1,-22} {2}' -f $c.Status.ToUpper(), $c.Title, $c.Detail) -ForegroundColor $colour[$c.Status]
    if ($c.Status -in @('ok', 'info')) { continue }
    if ($c.Why)     { Write-Host "          why: $($c.Why)" -ForegroundColor DarkGray }
    if ($c.Command) { Write-Host "          fix: $($c.Command)" -ForegroundColor DarkGray }
}

# ------------------------------------------------------------------------------ fix

$fixable = @($script:Checks | Where-Object { $_.FixAction -and $_.Status -in @('fail', 'warn') })

if (-not $Fix) {
    Write-Host ''
    if ($fixable.Count -gt 0) {
        Write-Host "  $($fixable.Count) of these can be fixed by this script (-Fix):" -ForegroundColor Cyan
        foreach ($f in $fixable) { Write-Host "    - $($f.FixLabel)" -ForegroundColor Cyan }
    }
    Write-Host '  Report only - nothing was changed.'
} elseif ($fixable.Count -eq 0) {
    Write-Host ''
    Write-Host '  -Fix: nothing in the safe subset applies. Nothing was changed.'
} else {
    Write-Host ''
    Write-Host '  -Fix: applying the safe subset'
    foreach ($f in $fixable) {
        if ($PSCmdlet.ShouldProcess($f.Title, $f.FixLabel)) {
            try {
                & $f.FixAction
                # NOT-THROWING IS NOT FIXED. Start-ScheduledTask returns as soon as the
                # scheduler accepts the request; a process that starts and dies a second later
                # threw nothing at all, so the row was marked fixed, dropped out of the standing
                # count, and the script exited 0 having repaired nothing. Where a row can state
                # what "worked" means, it is re-measured here and that measurement decides.
                if ($f.FixVerify) {
                    $f.Fixed = [bool] (& $f.FixVerify)
                    if ($f.Fixed) {
                        Write-Host "    done: $($f.FixLabel) - verified" -ForegroundColor Green
                    } else {
                        Write-Host "    NOT FIXED: $($f.FixLabel) ran, but the fault is still there" -ForegroundColor Red
                    }
                } else {
                    $f.Fixed = $true
                    Write-Host "    done: $($f.FixLabel)" -ForegroundColor Green
                }
            } catch {
                Write-Host "    FAILED: $($f.FixLabel) - $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }
    if (-not $WhatIfPreference) {
        $after = Get-HealthDocument -TimeoutSec 5
        $verdict = if ($after -and (Test-HasProperty $after 'ok') -and $after.ok) {
                       "ok, crabd $(if (Test-HasProperty $after 'version') { $after.version } else { '?' })"
                   } else { 'still not answering' }
        Write-Host "    health after fixes: $verdict"
    }
}

$standing = @($script:Checks | Where-Object { $_.Status -eq 'fail' -and -not $_.Fixed })
Write-Host ''
Write-Host ('{0} check(s): {1} fail, {2} warn, {3} unknown' -f
            $script:Checks.Count,
            @($script:Checks | Where-Object { $_.Status -eq 'fail' }).Count,
            @($script:Checks | Where-Object { $_.Status -eq 'warn' }).Count,
            @($script:Checks | Where-Object { $_.Status -eq 'unknown' }).Count) `
           -ForegroundColor $(if ($standing.Count) { 'Red' } else { 'Green' })
Write-Host 'Verify with: pwsh -File setup\Test-SideCrab.ps1'

exit ([int]($standing.Count -gt 0))

#Requires -Version 7.0
<#
.SYNOPSIS
    End-to-end smoke test for an installed SideCrab. Run this after any update.

.DESCRIPTION
    Answers one question - "is the thing you just updated actually working?" - by checking
    the whole chain a real session travels: the three Scheduled Tasks, crabd's own health,
    the shape and freshness of /v1/state, a full hook round trip, the config file, the toast
    identity, the status-line chain wiring and the panel-approval posture (informational while
    approvals are OFF; a FAIL when they are ON and the PermissionRequest hook cannot reach crabd).
    Prints a PASS/FAIL table and exits 0 ONLY when every row passes.

    READ-ONLY against production, with exactly one exception: the hook cycle POSTs
    SessionStart / Notification / SessionEnd for the session id 'smoke-test' (-SessionId to
    change it) and asserts the row appears and then disappears. That is the only way to prove
    the write path end to end, and it is self-cleaning - SessionEnd is posted from a finally
    block, so an aborted or failing run cannot strand a phantom session in the widget.
    -SkipHookCycle drops even that.

    Nothing is installed, registered, unregistered, started or stopped. No task is touched.

.EXAMPLE
    pwsh -File .\setup\Test-SideCrab.ps1
.EXAMPLE
    pwsh -File .\setup\Test-SideCrab.ps1 -SkipHookCycle    # strictly read-only
#>
[CmdletBinding()]
param(
    [string] $RepoRoot     = (Split-Path -Parent $PSScriptRoot),
    [string] $BaseUri      = 'http://127.0.0.1:9999',
    [string] $ConfigPath   = (Join-Path $HOME '.sidecrab\config.json'),
    [string] $SettingsPath = (Join-Path $HOME '.claude\settings.json'),
    [string] $ChainPath    = (Join-Path $HOME '.sidecrab\statusline-chain.json'),
    [string] $SessionId    = 'smoke-test',
    [int]    $TimeoutSec   = 15,
    [switch] $SkipHookCycle
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

#: Schemas this smoke test understands - the same range the widget accepts. A feed outside
#: it is a real failure, not a warning: producer and consumer have diverged.
$SupportedSchemas = 1, 2, 3, 4, 5

#: The widget calls the feed stale at 30 s (docs\STATE-CONTRACT.md). Same number here, on
#: purpose: this test must fail exactly when the widget would show its stale banner.
$MaxLagSec = 30

#: Below this, a smoke test's own 'smoke-test' session could mature past the notifier's
#: threshold mid-run and raise a real toast about a fake question.
$MinSafeThresholdSec = 60

$script:Results = [System.Collections.Generic.List[object]]::new()

function Add-Result {
    param(
        [Parameter(Mandatory)][string] $Check,
        [Parameter(Mandatory)][bool]   $Pass,
        [string] $Detail = ''
    )
    $script:Results.Add([pscustomobject]@{ Check = $Check; Pass = $Pass; Detail = $Detail })
    $mark  = if ($Pass) { 'PASS' } else { 'FAIL' }
    $color = if ($Pass) { 'Green' } else { 'Red' }
    Write-Host ('  {0,-4} {1,-22} {2}' -f $mark, $Check, $Detail) -ForegroundColor $color
}

function Get-StateDocument {
    <# One /v1/state read. Returns $null rather than throwing - an unreachable crabd is a
       FAIL row, not a crashed test run. #>
    param([int] $TimeoutSec = 5)
    try { Invoke-RestMethod -Uri "$BaseUri/v1/state" -TimeoutSec $TimeoutSec -ErrorAction Stop }
    catch { $null }
}

function Get-StateSession {
    <# Defensive on purpose: under Set-StrictMode a missing `sessions` key is a thrown
       error, and this runs against whatever crabd happens to be serving. #>
    param($State, [string] $Id)
    if ($null -eq $State -or @($State.PSObject.Properties.Name) -notcontains 'sessions') { return $null }
    @($State.sessions) | Where-Object { $_ -and $_.id -eq $Id } | Select-Object -First 1
}

function Wait-ForCondition {
    <# Polls /v1/state until the predicate holds. crabd rebuilds its snapshot every ~2 s, so
       a hook is never visible on the very next read - the wait IS the assertion. #>
    param(
        [Parameter(Mandatory)][scriptblock] $Predicate,
        [int] $TimeoutSec = 15
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $state = Get-StateDocument
        if ($state -and (& $Predicate $state)) {
            return [pscustomobject]@{ Ok = $true; State = $state; Waited = $TimeoutSec }
        }
        Start-Sleep -Milliseconds 700
    } while ((Get-Date) -lt $deadline)
    return [pscustomobject]@{ Ok = $false; State = $state; Waited = $TimeoutSec }
}

function Send-SmokeHook {
    <# POST one Claude-Code-shaped hook payload. crabd answers 204 and reads
       session_id / hook_event_name / cwd / message - see docs\STATE-CONTRACT.md. #>
    param([Parameter(Mandatory)][string] $Event, [string] $Message)

    $payload = @{ session_id = $SessionId; hook_event_name = $Event; cwd = $RepoRoot }
    if ($Message) { $payload['message'] = $Message }
    $body = $payload | ConvertTo-Json -Compress
    $resp = Invoke-WebRequest -Uri "$BaseUri/v1/hook" -Method Post -Body $body `
                              -ContentType 'application/json' -TimeoutSec 5 `
                              -Headers @{ 'X-SideCrab-Panel' = '1' } `
                              -SkipHttpErrorCheck -ErrorAction Stop
    [int] $resp.StatusCode
}

# ------------------------------------------------------------------------------ run

Write-Host "SideCrab smoke test  ($BaseUri)"
Write-Host "  repo:    $RepoRoot"
Write-Host ''

# -- 1. the three Scheduled Tasks ------------------------------------------------------
foreach ($component in (Get-SideCrabComponentSpec -RepoRoot $RepoRoot)) {
    $state = Get-SideCrabTaskState -TaskName $component.TaskName
    if (-not $state.Registered) {
        Add-Result -Check "task $($component.Key)" -Pass $false -Detail "$($component.TaskName) not registered"
        continue
    }
    if ($state.State -eq 'Disabled') {
        # A DISABLED task is a stated decision (Disable-ScheduledTask), not a fault - the
        # glow is parked this way while the SDK's headless crash stands (docs/BACKLOG.md).
        # A crashed-but-enabled task still fails below; only deliberate disablement passes.
        Add-Result -Check "task $($component.Key)" -Pass $true `
                   -Detail "$($component.TaskName) disabled - deliberate (see docs/BACKLOG.md)"
        continue
    }
    $running = $state.State -eq 'Running'
    $detail  = "$($component.TaskName) $($state.State)"
    if (-not $running -and $null -ne $state.LastTaskResult) {
        $detail += ('  last result 0x{0:X8}' -f ([int64] $state.LastTaskResult -band 0xFFFFFFFFL))
    }
    Add-Result -Check "task $($component.Key)" -Pass $running -Detail $detail
}

# -- 2. /v1/health ---------------------------------------------------------------------
$health = Get-SideCrabHealth -Uri "$BaseUri/v1/health"
if (-not $health.Reachable) {
    Add-Result -Check 'health' -Pass $false -Detail "unreachable - $($health.Error)"
} else {
    Add-Result -Check 'health' -Pass ($health.Ok -and [bool] $health.Version) `
               -Detail "ok=$($health.Ok) crabd $($health.Version)"
}

# -- 3. /v1/state shape and freshness --------------------------------------------------
$state = Get-StateDocument
if ($null -eq $state) {
    Add-Result -Check 'state reachable' -Pass $false -Detail "GET $BaseUri/v1/state failed"
    Add-Result -Check 'state schema'    -Pass $false -Detail 'not evaluated'
    Add-Result -Check 'state freshness' -Pass $false -Detail 'not evaluated'
} else {
    Add-Result -Check 'state reachable' -Pass $true -Detail "$(@($state.sessions).Count) session(s)"

    $names   = @($state.PSObject.Properties.Name)
    # The keys every schema from 1 up carries. Newer optional blocks (recap, quiet, fleet)
    # are deliberately NOT required - this test must not fail an older crabd for being older.
    # Outer @() is load-bearing: Where-Object yields $null for no matches, and $null.Count
    # throws under Set-StrictMode rather than reading as zero.
    $missing = @(@('schema', 'generatedAt', 'crabd', 'limits', 'burn', 'sessions') |
                 Where-Object { $names -notcontains $_ })
    $schemaOk = ($missing.Count -eq 0) -and ($SupportedSchemas -contains [int] $state.schema)
    $detail   = if ($missing.Count) { "missing key(s): $($missing -join ', ')" }
                else { "schema $($state.schema), keys ok" }
    Add-Result -Check 'state schema' -Pass $schemaOk -Detail $detail

    $generated = $null
    try { $generated = [datetime]::Parse("$($state.generatedAt)", [cultureinfo]::InvariantCulture,
                                         [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor
                                         [System.Globalization.DateTimeStyles]::AssumeUniversal) } catch { }
    if ($null -eq $generated) {
        Add-Result -Check 'state freshness' -Pass $false -Detail "unparseable generatedAt '$($state.generatedAt)'"
    } else {
        $lag = [math]::Round(((Get-Date).ToUniversalTime() - $generated).TotalSeconds, 1)
        Add-Result -Check 'state freshness' -Pass ($lag -lt $MaxLagSec) -Detail "generatedAt lag ${lag}s (limit ${MaxLagSec}s)"
    }
}

# -- 4. the hook round trip ------------------------------------------------------------
if ($SkipHookCycle) {
    Write-Host '  SKIP hook cycle             (-SkipHookCycle)' -ForegroundColor DarkGray
} else {
    $existing = Get-StateSession -State $state -Id $SessionId
    if ($existing) {
        Add-Result -Check 'hook cycle' -Pass $false `
                   -Detail "session id '$SessionId' is already live - pass -SessionId to use another"
    } else {
        # A live threshold shorter than this run would let the fake needs_input mature and
        # raise a real toast about a session that does not exist. Skip that leg instead.
        $threshold = 120
        try {
            $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8 -ErrorAction Stop | ConvertFrom-Json
            if ($cfg.PSObject.Properties.Name -contains 'toast' -and
                $cfg.toast.PSObject.Properties.Name -contains 'thresholdSec') {
                $threshold = [int] $cfg.toast.thresholdSec
            }
        } catch { }
        $notifySafe = $threshold -ge $MinSafeThresholdSec

        $posted = @()
        try {
            $code = Send-SmokeHook -Event 'SessionStart'
            $posted += "SessionStart=$code"
            $appeared = Wait-ForCondition -TimeoutSec $TimeoutSec -Predicate {
                param($s) $null -ne (Get-StateSession -State $s -Id $SessionId)
            }
            $row = Get-StateSession -State $appeared.State -Id $SessionId
            Add-Result -Check 'hook SessionStart' -Pass $appeared.Ok `
                       -Detail $(if ($appeared.Ok) { "row appeared, state '$($row.state)'" }
                                 else { "row never appeared within ${TimeoutSec}s ($($posted -join ' '))" })

            if ($notifySafe) {
                $code = Send-SmokeHook -Event 'Notification' -Message 'SideCrab smoke test - not a real question'
                $posted += "Notification=$code"
                $waiting = Wait-ForCondition -TimeoutSec $TimeoutSec -Predicate {
                    param($s)
                    $r = Get-StateSession -State $s -Id $SessionId
                    ($null -ne $r) -and ($r.state -eq 'needs_input')
                }
                Add-Result -Check 'hook Notification' -Pass $waiting.Ok `
                           -Detail $(if ($waiting.Ok) { "row moved to needs_input" }
                                     else { "row never reached needs_input within ${TimeoutSec}s" })
            } else {
                Write-Host "  SKIP hook Notification      toast thresholdSec=$threshold < ${MinSafeThresholdSec}s - would raise a real toast" `
                           -ForegroundColor DarkGray
            }
        } finally {
            # ALWAYS: a failed assertion above must not leave a phantom row in the widget.
            $endCode  = Send-SmokeHook -Event 'SessionEnd'
            $posted  += "SessionEnd=$endCode"
            $gone = Wait-ForCondition -TimeoutSec $TimeoutSec -Predicate {
                param($s) $null -eq (Get-StateSession -State $s -Id $SessionId)
            }
            Add-Result -Check 'hook SessionEnd' -Pass $gone.Ok `
                       -Detail $(if ($gone.Ok) { "row cleared ($($posted -join ' '))" }
                                 else { "row still served after SessionEnd ($($posted -join ' '))" })
        }
    }
}

# -- 5. config.json --------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    # Absent is the documented first-run state: the consumers all fall back to defaults.
    Add-Result -Check 'config.json' -Pass $true -Detail "$ConfigPath absent - defaults apply"
} else {
    try {
        $cfg  = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8 | ConvertFrom-Json -Depth 20
        $keys = @($cfg.PSObject.Properties.Name)
        Add-Result -Check 'config.json' -Pass $true -Detail "parses; keys: $($keys -join ', ')"
    } catch {
        Add-Result -Check 'config.json' -Pass $false -Detail "unparseable - $($_.Exception.Message)"
    }
}

# -- 6. the notifier accepts what crabd is serving -------------------------------------
# Found the hard way 2026-08-26: crabd moved to schema 4 and the notifier's SUPPORTED_SCHEMAS
# did not. It kept running, kept polling and never toasted again - "task Running" is NOT a
# test that notifications work. This row is the one that would have caught it.
$notifierPy = Join-Path $RepoRoot 'notifier\sidecrab_toast.py'
if (-not (Test-Path -LiteralPath $notifierPy)) {
    Add-Result -Check 'notifier schema' -Pass $true -Detail 'notifier not present - n/a'
} elseif ($null -eq $state) {
    Add-Result -Check 'notifier schema' -Pass $false -Detail 'no state document to compare against'
} else {
    $py = Get-Content -LiteralPath $notifierPy -Raw -Encoding utf8
    if ($py -match 'SUPPORTED_SCHEMAS\s*=\s*frozenset\(\{([0-9,\s]+)\}\)') {
        $accepted = @($Matches[1] -split ',' | Where-Object { $_.Trim() } | ForEach-Object { [int] $_.Trim() })
        $live     = [int] $state.schema
        Add-Result -Check 'notifier schema' -Pass ($accepted -contains $live) `
                   -Detail "crabd serves $live; notifier accepts $($accepted -join ',')$(if ($accepted -notcontains $live) { ' - it will never toast' })"
    } else {
        Add-Result -Check 'notifier schema' -Pass $false -Detail 'could not read SUPPORTED_SCHEMAS from sidecrab_toast.py'
    }
}

# -- 6b. the glow accepts what crabd is serving ----------------------------------------
# Same bug class as row 6, found in the glow on 2026-08-26 (ACCEPTED_SCHEMAS stopped at 3
# while crabd served 5 - Running task, dark forever). One row per consumer, no exceptions.
$glowPy = Join-Path $RepoRoot 'lighting\decision.py'
if (-not (Test-Path -LiteralPath $glowPy)) {
    Add-Result -Check 'glow schema' -Pass $true -Detail 'glow not present - n/a'
} elseif ($null -eq $state) {
    Add-Result -Check 'glow schema' -Pass $false -Detail 'no state document to compare against'
} else {
    $gpy = Get-Content -LiteralPath $glowPy -Raw -Encoding utf8
    if ($gpy -match 'ACCEPTED_SCHEMAS\s*=\s*frozenset\(\{([0-9,\s]+)\}\)') {
        $gaccepted = @($Matches[1] -split ',' | Where-Object { $_.Trim() } | ForEach-Object { [int] $_.Trim() })
        $glive     = [int] $state.schema
        Add-Result -Check 'glow schema' -Pass ($gaccepted -contains $glive) `
                   -Detail "crabd serves $glive; glow accepts $($gaccepted -join ',')$(if ($gaccepted -notcontains $glive) { ' - it will never light' })"
    } else {
        Add-Result -Check 'glow schema' -Pass $false -Detail 'could not read ACCEPTED_SCHEMAS from decision.py'
    }
}

# -- 7. toast identity -----------------------------------------------------------------
$aumid = Get-SideCrabAumidState -RepoRoot $RepoRoot
$toastInstalled = (Get-SideCrabTaskState -TaskName 'SideCrab-toast').Registered
if (-not $toastInstalled) {
    Add-Result -Check 'toast identity' -Pass $true -Detail 'toast component not installed - n/a'
} elseif (-not $aumid.Registered) {
    Add-Result -Check 'toast identity' -Pass $false `
               -Detail "$($aumid.Aumid) not registered - run setup\Register-SideCrabAumid.ps1"
} else {
    Add-Result -Check 'toast identity' -Pass ($aumid.Current -and $aumid.IconPresent) `
               -Detail "$($aumid.Aumid) registered, icon $(if ($aumid.IconPresent) { 'present' } else { "MISSING at $($aumid.IconPath)" })"
}

# -- 8. toast actions (the Acknowledge and Snooze buttons) -----------------------------
# One row per scheme: each button is its own handler behind its own scheme, and a single row
# would report the first one's state as if it covered both.
# Registered-and-stale is the failure worth catching: the button then launches a handler
# path that a repo move invalidated, which is a shell error rather than a quiet no-op.
foreach ($proto in @(Get-SideCrabProtocolState -RepoRoot $RepoRoot)) {
    $row = "toast action ($($proto.Key))"
    if (-not $toastInstalled) {
        Add-Result -Check $row -Pass $true -Detail 'toast component not installed - n/a'
    } elseif (-not $proto.Registered) {
        Add-Result -Check $row -Pass $false `
                   -Detail "$($proto.Scheme): not registered - the $($proto.Button) button no-ops. Run setup\Register-SideCrabProtocol.ps1"
    } else {
        Add-Result -Check $row -Pass ($proto.Current -and $proto.HandlerPresent -and $proto.CarriesArgument) `
                   -Detail "$($proto.Scheme): registered, handler $(if ($proto.HandlerPresent) { 'present' } else { "MISSING at $($proto.HandlerPath)" })$(if (-not $proto.CarriesArgument) { ', command has no "%1"' })$(if (-not $proto.Current) { ', command differs from expected' })"
    }
}

# -- 9. the status-line chain ----------------------------------------------------------
# The status line renders sidecrab_statusline.py, which POSTs to /v1/statusline and then
# chains to whatever the installer saved. "installed" is not enough on its own: a command
# that points at a script the repo moved would print nothing on every refresh.
$slSpec     = Get-SideCrabStatusLineSpec -RepoRoot $RepoRoot
$slSettings = $null
try { $slSettings = Read-SideCrabSettings -SettingsPath $SettingsPath } catch { }
$slCmd = if ($null -ne $slSettings -and $slSettings.Contains('statusLine') -and
             $slSettings['statusLine'] -is [System.Collections.IDictionary]) {
             "$($slSettings['statusLine']['command'])"
         } else { '' }
if (-not (Test-SideCrabStatusLineIsOurs -Command $slCmd)) {
    $why = if ($slCmd) { 'a non-SideCrab status line is configured' }
           else        { 'no SideCrab status line in settings.json' }
    Add-Result -Check 'statusline chain' -Pass $false -Detail "$why - run Install-SideCrab.ps1"
} else {
    $scriptOk = [bool] (Test-Path -LiteralPath $slSpec.Script)
    # "Ours" matches the script NAME, so a command left behind by another checkout also passes
    # it. The installed command must point at THIS repo's copy, or every refresh runs a script
    # this run never checked - and after a repo move, one that is not there at all.
    $pointsHere = [bool] ($slCmd -like "*$($slSpec.Script)*")

    $saved = Get-SideCrabSavedStatusLine -ChainPath $ChainPath
    $priorCmd = if ($saved.StatusLine -is [System.Collections.IDictionary]) { "$($saved.StatusLine['command'])" } else { '' }
    if (-not $saved.Present) {
        # The installer writes this file on the same run that takes the slot. Our command
        # installed with no chain file means Uninstall has nothing to restore - the operator's
        # own status line is gone, not chained. That is a broken install, not a posture.
        $chainOk = $false; $chainMsg = 'NO chain file - uninstall cannot restore a prior line'
    } elseif ($null -eq $saved.StatusLine) {
        $chainOk = $true;  $chainMsg = 'chain file records no prior line (slot was empty) - nothing to chain to'
    } elseif (-not $priorCmd) {
        $chainOk = $false; $chainMsg = 'saved prior has no command - uninstall would restore an empty line'
    } else {
        $chainOk = $true
        $shown   = if ($priorCmd.Length -gt 48) { $priorCmd.Substring(0, 45) + '...' } else { $priorCmd }
        $chainMsg = "chains to saved prior: $shown"
    }

    $why = @()
    if (-not $scriptOk)   { $why += "MISSING script at $($slSpec.Script)" }
    if (-not $pointsHere) { $why += "command points OUTSIDE this repo ($slCmd)" }
    Add-Result -Check 'statusline chain' -Pass ($scriptOk -and $pointsHere -and $chainOk) `
               -Detail "installed; $chainMsg$(if ($why.Count) { ' - ' + ($why -join '; ') })"
}

# -- 9b. limits token source (crabd 0.30.0) --------------------------------------------
# Reported, never judged: the gauges reading the CLI token is the ordinary state. The row
# says which token is answering and, when the gauges are dark, why - so "token expired"
# on the glass is a row here and not a mystery.
try {
    $lim = (Invoke-RestMethod "$BaseUri/v1/state" -TimeoutSec 5).limits
    $ltState = Get-SideCrabLimitsTokenState -TokenPath (Join-Path (Split-Path -Parent $ConfigPath) 'limits-token.dpapi')
    $stored = if ($ltState.Present) { 'long-lived token stored' } else { 'no long-lived token stored' }
    if ($lim.available) {
        $src = if ($lim.PSObject.Properties['tokenSource']) { $lim.tokenSource } else { 'cli (pre-0.30.0 crabd)' }
        Add-Result -Check 'limits token' -Pass $true -Detail "gauges live via token source '$src'; $stored"
    } else {
        Add-Result -Check 'limits token' -Pass $true -Detail "gauges dark: $($lim.note); $stored"
    }
} catch {
    Add-Result -Check 'limits token' -Pass $true -Detail 'state not readable - see the state rows above'
}

# -- 10. panel approvals ---------------------------------------------------------------
# OFF is the default and a valid state, so an OFF posture is reported, never judged. ON is
# judged: config saying "taps decide" while the PermissionRequest hook is absent or blocked
# is the silent failure worth catching - no prompt ever reaches the panel, and the operator
# believes it is armed. The row names the wiring either way, so OFF is not a bare word.
$pa = Get-SideCrabPanelApprovalsState -ConfigPath $ConfigPath
# crabd 0.29.0: a decide is refused without the pairing code, so ON + no code = armed but
# every tap 403s. Presence only - the code itself never lands in a smoke table.
$tok = Get-SideCrabPanelToken -TokenPath (Join-Path (Split-Path -Parent $ConfigPath) 'panel-token')
$tokMsg = if ($tok.Present) { 'pairing code present' } else { 'NO pairing code (crabd 0.29.0+ mints it on first start)' }
$permUrl   = "$BaseUri/v1/hook/permission"
$permWired = [bool] @(@(Get-SideCrabHookEvent -Settings $slSettings) |
                      Where-Object { $_.Event -eq 'PermissionRequest' })
# allowedHttpHookUrls, when the operator has set it, must admit our URL or the CLI refuses
# to call the http hook at all ($null here = unset = everything allowed).
$permAllowed = $null
if ($null -ne $slSettings -and $slSettings.Contains('allowedHttpHookUrls')) {
    $permAllowed = [bool] @(@($slSettings['allowedHttpHookUrls']) |
                            Where-Object { "$_" -and $permUrl -like "$_" }).Count
}
$wiringMsg = "PermissionRequest hook $(if ($permWired) { 'wired' } else { 'NOT wired' })" +
             $(if ($permAllowed -eq $false) { '; BLOCKED by allowedHttpHookUrls' } else { '' })

if ($pa.Enabled) {
    Add-Result -Check 'panel approvals' -Pass ($permWired -and $permAllowed -ne $false -and $tok.Present) `
               -Detail "ENABLED - widget taps can allow/deny tool calls; $wiringMsg; $tokMsg"
} else {
    $paMsg = if ($null -eq $pa.Enabled) { 'default OFF (key absent)' } else { 'disabled' }
    Add-Result -Check 'panel approvals' -Pass $true `
               -Detail "$paMsg; $wiringMsg; $tokMsg; never verified on a live prompt - run setup\Verify-PanelApproval.ps1 -DryRun before enabling"
}

# ------------------------------------------------------------------------------ verdict

$failed = @($script:Results | Where-Object { -not $_.Pass })
Write-Host ''
Write-Host ('{0} check(s): {1} passed, {2} failed' -f
            $script:Results.Count, ($script:Results.Count - $failed.Count), $failed.Count) `
           -ForegroundColor $(if ($failed.Count) { 'Red' } else { 'Green' })
foreach ($f in $failed) { Write-Host "  FAIL $($f.Check): $($f.Detail)" -ForegroundColor Red }

exit ([int]($failed.Count -gt 0))

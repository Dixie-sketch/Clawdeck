#Requires -Version 7.0
<#
.SYNOPSIS
    Preflight checker + written procedure for verifying SideCrab panel approval on a live
    Claude Code permission prompt. It NEVER enables approvals and never approves anything.

.DESCRIPTION
    Panel approval (contract v0.12.0 §4) is settled on paper - the PermissionRequest response
    shape was read out of the shipped v2.1.246 zod schema - but it has never been exercised
    against a real CLI prompt. This script is the on-machine procedure that closes that gap:

      1. It runs a READ-ONLY preflight over the things that must already be true (crabd up and
         new enough to serve /v1/hook/permission, the PermissionRequest hook wired into
         settings.json and not blocked by allowedHttpHookUrls, the current approvals posture).
      2. It prints the EXACT steps and commands for the live run: enable, trigger a real prompt
         in a DISPOSABLE session, approve it, confirm the CLI honoured it, then deny one.

    What it deliberately does NOT do, and must never be changed to do:
      - it does not write config.json, settings.json or the registry, and never enables
        approvals (that is `Install-SideCrab.ps1 -WithApprovals`, an operator decision);
      - it does not POST a `decide` - an automated approver is the exact thing panel approval
        exists to prevent. The taps in step 3 are the operator's.

    Exit code: 0 when the preflight is green (ready to run the live verification), 1 when a
    blocker stands. -DryRun always exits 0.

.PARAMETER DryRun
    Explain only. No HTTP call is made and no file is read for state - the script prints what
    it WOULD check and the full procedure. Use this to read the procedure without touching
    a running crabd.

.EXAMPLE
    pwsh -File .\setup\Verify-PanelApproval.ps1 -DryRun
.EXAMPLE
    pwsh -File .\setup\Verify-PanelApproval.ps1
#>
[CmdletBinding()]
param(
    [string] $RepoRoot     = (Split-Path -Parent $PSScriptRoot),
    [string] $BaseUri      = 'http://127.0.0.1:2722',
    [string] $ConfigPath   = (Join-Path $HOME '.sidecrab\config.json'),
    [string] $SettingsPath = (Join-Path $HOME '.claude\settings.json'),
    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

#: crabd holds a PermissionRequest this long awaiting a tap (PERMISSION_POLL_SEC in crabd.py);
#: the hook's own timeout is 60 s so the CLI outlives the hold rather than racing it.
$PollSec        = 55
$HookTimeoutSec = 60
$PermPath       = '/v1/hook/permission'

$script:Blockers = [System.Collections.Generic.List[string]]::new()

function Write-Row {
    param([string] $State, [string] $Check, [string] $Detail)
    $color = switch ($State) { 'OK' { 'Green' } 'BLOCK' { 'Red' } default { 'Yellow' } }
    Write-Host ('  {0,-5} {1,-22} {2}' -f $State, $Check, $Detail) -ForegroundColor $color
}
function Add-Block {
    param([string] $Check, [string] $Detail)
    $script:Blockers.Add("$Check - $Detail")
    Write-Row 'BLOCK' $Check $Detail
}
function Write-Head { param([string] $Text) Write-Host ''; Write-Host $Text -ForegroundColor Cyan }

# ------------------------------------------------------------------------------ preflight

Write-Host "SideCrab panel-approval verification  ($BaseUri)"
Write-Host "  repo:    $RepoRoot"

$approvalsOn = $null

if ($DryRun) {
    Write-Head 'PREFLIGHT (not run - -DryRun)'
    Write-Host '  Without -DryRun this checks, all read-only:'
    Write-Host '    - crabd health and version (GET /v1/health)'
    Write-Host "    - crabd serves $PermPath (an EMPTY POST: no session id, so crabd's first"
    Write-Host '      branch answers the pass-through {} and registers nothing - it is inert)'
    Write-Host '    - the PermissionRequest http hook is in settings.json, timeout past crabd''s hold'
    Write-Host '    - allowedHttpHookUrls, if set, admits the crabd URL'
    Write-Host '    - the current panelApprovals posture in config.json'
} else {
    Write-Head 'PREFLIGHT (read-only)'

    # --- crabd up
    $health = Get-SideCrabHealth -Uri "$BaseUri/v1/health"
    if (-not $health.Reachable) {
        Add-Block 'crabd health' "unreachable - $($health.Error)"
    } elseif (-not $health.Ok) {
        Add-Block 'crabd health' 'reachable but not ok'
    } else {
        Write-Row 'OK' 'crabd health' "crabd $($health.Version)"
    }

    # --- the endpoint exists. An empty body carries no session_id, and crabd's permission
    #     handler returns the pass-through {} before it registers anything or writes history,
    #     so this probe cannot create, approve or deny a request. A 404 = crabd predates §4.
    $permStatus = $null
    $permBody   = ''
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUri$PermPath" -Method Post -Body '{}' `
                                  -ContentType 'application/json' -TimeoutSec 5 `
                                  -SkipHttpErrorCheck -ErrorAction Stop
        $permStatus = [int] $resp.StatusCode
        $permBody   = "$($resp.Content)".Trim()
    } catch { $permStatus = $null }
    if ($null -eq $permStatus) {
        Add-Block 'permission route' "POST $PermPath did not answer"
    } elseif ($permStatus -eq 404) {
        Add-Block 'permission route' "404 - this crabd predates panel approval; update it first"
    } elseif ($permStatus -ne 200) {
        Add-Block 'permission route' "unexpected HTTP $permStatus"
    } else {
        $isPassThrough = ($permBody -eq '{}' -or -not $permBody)
        if (-not $isPassThrough) {
            # The one answer that must never come back from a body with no session id.
            Add-Block 'permission route' "200 but body was '$permBody', not the pass-through {}"
        } else {
            Write-Row 'OK' 'permission route' '200 + pass-through {} (inert probe, nothing registered)'
        }
    }

    # --- the hook that carries a prompt to crabd
    $settings = $null
    try { $settings = Read-SideCrabSettings -SettingsPath $SettingsPath } catch { }
    if ($null -eq $settings) {
        Add-Block 'PermissionRequest hook' "$SettingsPath unreadable or absent - run Install-SideCrab.ps1"
    } else {
        $wired = [bool] @(@(Get-SideCrabHookEvent -Settings $settings) |
                          Where-Object { $_.Event -eq 'PermissionRequest' })
        if (-not $wired) {
            Add-Block 'PermissionRequest hook' 'not in settings.json - run Install-SideCrab.ps1'
        } else {
            # A hook timeout at or under crabd's 55 s hold makes every prompt look like a
            # SideCrab timeout to the CLI even when the operator taps in time.
            $timeout = $null
            foreach ($m in @($settings['hooks']['PermissionRequest'])) {
                if ($m -is [System.Collections.IDictionary] -and $m.Contains('hooks')) {
                    foreach ($h in @($m['hooks'])) {
                        if ($h -is [System.Collections.IDictionary] -and "$($h['url'])" -like "*$PermPath*") {
                            $timeout = $h['timeout']
                        }
                    }
                }
            }
            if ($null -ne $timeout -and [int] $timeout -le $PollSec) {
                Add-Block 'PermissionRequest hook' "timeout ${timeout}s is not past crabd's ${PollSec}s hold (want ${HookTimeoutSec}s)"
            } else {
                Write-Row 'OK' 'PermissionRequest hook' "wired, timeout $(if ($null -ne $timeout) { "${timeout}s" } else { 'default' })"
            }
        }

        if ($settings.Contains('allowedHttpHookUrls')) {
            $patterns = @($settings['allowedHttpHookUrls'])
            $admits = [bool] @($patterns | Where-Object { "$_" -and "$BaseUri$PermPath" -like "$_" }).Count
            if ($admits) { Write-Row 'OK' 'allowedHttpHookUrls' "set and admits $BaseUri$PermPath" }
            else         { Add-Block 'allowedHttpHookUrls' "set but does not admit $BaseUri$PermPath - the CLI will not call the hook" }
        } else {
            Write-Row 'OK' 'allowedHttpHookUrls' 'unset - all hook URLs allowed'
        }
    }

    # --- posture. Not a blocker either way: OFF is what step 1 turns on, ON means step 1 is
    #     already done (and is worth saying out loud, because taps are live right now).
    $pa = Get-SideCrabPanelApprovalsState -ConfigPath $ConfigPath
    $approvalsOn = [bool] $pa.Enabled
    if ($null -eq $pa.Enabled)  { Write-Row 'INFO' 'panelApprovals' 'default OFF (key absent) - step 1 below turns it on' }
    elseif ($pa.Enabled)        { Write-Row 'INFO' 'panelApprovals' 'ENABLED - taps decide RIGHT NOW; skip step 1' }
    else                        { Write-Row 'INFO' 'panelApprovals' 'disabled - step 1 below turns it on' }

    $widget = Get-SideCrabWidgetVersion -RepoRoot $RepoRoot
    if ($widget) {
        Write-Row 'INFO' 'widget manifest' "$widget - the copy IMPORTED into iCUE is what shows Approve/Deny, not this file"
    }
}

# ------------------------------------------------------------------------------ procedure

$skipStep1 = ($approvalsOn -eq $true)

Write-Head 'THE PROCEDURE (do this on the machine, in order)'

Write-Host ''
Write-Host '  0. Use a DISPOSABLE session in a scratch directory. The first prompt you approve is a'
Write-Host '     REAL tool call in whatever session raises it - never do this run inside real work.'
Write-Host '     Have the Xeneon Edge widget visible before you start; the sheet is where you tap.'

Write-Host ''
if ($skipStep1) {
    Write-Host '  1. Enable - ALREADY DONE (panelApprovals.enabled is true). Skip to step 2.' -ForegroundColor DarkGray
} else {
    Write-Host '  1. Enable panel approval (this script will not do it for you):'
    Write-Host ''
    Write-Host '       pwsh -File setup\Install-SideCrab.ps1 -WithApprovals' -ForegroundColor White
    Write-Host ''
    Write-Host '     crabd reads config.json live - no restart. Re-run this script to confirm the'
    Write-Host '     posture row flipped to ENABLED before going on.'
}

Write-Host ''
Write-Host '  2. Trigger a REAL permission prompt in the disposable session: ask it to do something'
Write-Host '     your settings do not pre-allow (a Bash command outside the allowlist is the usual'
Write-Host '     one). The moment the CLI would show its terminal dialog, the hook fires and crabd'
Write-Host "     holds it for ${PollSec}s. Confirm the hold is live - the session's card shows the"
Write-Host '     pending request, and the state document carries it:'
Write-Host ''
Write-Host "       curl.exe -s $BaseUri/v1/state | ConvertFrom-Json | %{ `$_.sessions } | ?{ `$_.pendingPermission } | fl id,state,pendingPermission" -ForegroundColor White
Write-Host ''
Write-Host '     If nothing is pending, STOP - the hook is not reaching crabd, and nothing below'
Write-Host '     proves anything. Re-run the preflight above.'

Write-Host ''
Write-Host '  3. APPROVE it - tap Approve on the widget sheet. That tap is the whole point; only'
Write-Host '     use the equivalent POST if the widget is unavailable, and type it yourself:'
Write-Host ''
Write-Host "       curl.exe -s -X POST $BaseUri/v1/action -H 'Content-Type: application/json' ``" -ForegroundColor White
Write-Host '              --data ''{"sessionId":"<id from step 2>","action":"decide","decision":"allow"}''' -ForegroundColor White
Write-Host ''
Write-Host '     Expect 204. A 404 means the hold already expired (>55s) - the terminal dialog now'
Write-Host '     owns the decision; answer it and redo step 2.'

Write-Host ''
Write-Host '  4. CONFIRM THE CLI HONOURED IT. This is the row that has never been measured, so read'
Write-Host '     it off the terminal, not off crabd:'
Write-Host '       - the tool call proceeds WITHOUT the terminal permission dialog ever appearing;'
Write-Host '       - the session history gains "approved from panel: <Tool>":'
Write-Host ''
Write-Host "       curl.exe -s $BaseUri/v1/state | ConvertFrom-Json | %{ `$_.sessions } | ?{ `$_.id -eq '<id>' } | %{ `$_.events }" -ForegroundColor White
Write-Host ''
Write-Host '     A dialog appearing anyway = the CLI ignored the response shape. Record that in'
Write-Host '     docs\BACKLOG.md and leave approvals OFF - it is the failure this exists to find.'

Write-Host ''
Write-Host '  5. NOW ONE DENY. Trigger a second prompt (step 2), tap Deny (or the same POST with'
Write-Host '     "decision":"deny"). Expect: the tool does NOT run, and the CLI reports the denial'
Write-Host '     carrying crabd''s message "denied from the SideCrab panel". A denied call that runs'
Write-Host '     anyway is a hard stop - turn approvals off and write it up.'

Write-Host ''
Write-Host '  6. OPTIONAL but cheap - the fail-safe: trigger a third prompt and tap NOTHING for'
Write-Host "     ${PollSec}s. The terminal dialog must appear as it always did (crabd answers the"
Write-Host '     pass-through on timeout). This proves a dead widget cannot block your session.'

Write-Host ''
Write-Host '  7. Write the result into docs\BACKLOG.md (the "Verify before relying on" entry), and'
Write-Host '     if you are not adopting it yet, turn it back off:'
Write-Host ''
Write-Host '       pwsh -File setup\Uninstall-SideCrab.ps1   # or set panelApprovals.enabled=false' -ForegroundColor White

# ------------------------------------------------------------------------------ verdict

Write-Host ''
if ($DryRun) {
    Write-Host 'Dry run: nothing was checked and nothing was changed. Re-run without -DryRun for the preflight.' -ForegroundColor Cyan
    exit 0
}
if ($script:Blockers.Count -gt 0) {
    Write-Host ('Preflight: {0} blocker(s) - fix these before the live run.' -f $script:Blockers.Count) -ForegroundColor Red
    foreach ($b in $script:Blockers) { Write-Host "  $b" -ForegroundColor Red }
    exit 1
}
Write-Host 'Preflight green - the wiring is ready for the live verification above. Nothing was changed.' -ForegroundColor Green
exit 0

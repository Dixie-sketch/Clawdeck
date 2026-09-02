#Requires -Version 7.0
<#
.SYNOPSIS
    Installs the SideCrab companion: logon Scheduled Tasks for crabd (and, when
    present, glow and toast), plus the Claude Code hook entries that feed them.

.DESCRIPTION
    Idempotent parts, any of which can be run alone:
      1. Scheduled Tasks - one per selected component, started at logon, hidden,
         restarting on failure. crabd is always installed; glow and toast are
         optional (see COMPONENTS). Installing toast also makes two HKCU registrations:
         SideCrab's own toast identity (setup\Register-SideCrabAumid.ps1) so notifications
         are attributed to SideCrab rather than to Windows PowerShell, and the toast's
         button protocols (setup\Register-SideCrabProtocol.ps1) - 'sidecrab-ack:' for
         Acknowledge and 'sidecrab-snooze:' for Snooze 30m.
      2. Merges hooks/settings-hooks-fragment.json into ~/.claude/settings.json (the five
         curl ingest hooks plus the two type-http control hooks, Stop and PermissionRequest).
      3. Installs the status-line command (hooks\sidecrab_statusline.py) into
         ~/.claude/settings.json, SAVING any pre-existing statusLine to
         ~/.sidecrab/statusline-chain.json first so the chain script can call it and Uninstall
         can restore it. Skip with -SkipStatusLine.
      4. Sets ~/.sidecrab/config.json panelApprovals.enabled. DEFAULT is FALSE (panel approval
         off); -WithApprovals turns it on and prints a one-line security notice.

    Re-running is safe: tasks are re-registered from scratch and the hook merge is
    matched on the crabd URL, so entries are never duplicated and other hooks are
    left untouched. A task the operator DISABLED stays disabled across a re-run - it is
    re-registered (paths stay current) and put straight back into Disabled, and it is not
    started; -ForceEnable is the deliberate override. The prior status line is saved only when it is not already ours, so a
    re-run never captures our own command. settings.json is backed up (timestamped) before
    any write.

    COMPONENTS
      crabd  always installed          companion\crabd.py
      glow   -WithGlow, or auto        lighting\glow_launcher.pyw
      toast  -WithToast, or auto       notifier\sidecrab_toast.py

    With no switches, an optional component is installed when its script file
    exists and skipped when it does not. Passing -WithGlow / -WithToast for a
    script that is missing is an error, not a silent skip.

    GLOW ALSO HAS TO IMPORT. Its script file existing is what auto-selects it, and that says
    nothing about whether cuesdk is installed - without it the launcher starts, raises
    ImportError and exits, leaving a Registered task that has never controlled a light. So
    `import cuesdk` is run under the interpreter the task will use, before the task is
    registered. Auto-detected + not importable = SKIPPED with the pip line, because
    auto-detection is an inference and the failed import refutes it. -WithGlow + not importable
    = registered anyway, loudly: an explicit switch is an instruction, and failing the whole
    install (crabd included) over a lighting dependency would be the worse outcome.

.EXAMPLE
    pwsh -File .\setup\Install-SideCrab.ps1
.EXAMPLE
    pwsh -File .\setup\Install-SideCrab.ps1 -WithGlow -WithToast
.EXAMPLE
    pwsh -File .\setup\Install-SideCrab.ps1 -WithApprovals   # turn ON panel approval
.EXAMPLE
    pwsh -File .\setup\Install-SideCrab.ps1 -ForceEnable   # re-enable a task you disabled
.EXAMPLE
    pwsh -File .\setup\Install-SideCrab.ps1 -Status
.EXAMPLE
    pwsh -File .\setup\Install-SideCrab.ps1 -SkipTask
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $RepoRoot   = (Split-Path -Parent $PSScriptRoot),
    [string] $SettingsPath = (Join-Path $HOME '.claude\settings.json'),
    [string] $ConfigPath = (Join-Path $HOME '.sidecrab\config.json'),
    [string] $ChainPath  = (Join-Path $HOME '.sidecrab\statusline-chain.json'),
    # NO -TaskName. It renamed the crabd task and nothing else, and nothing downstream could
    # find the result: Update, Repair and Test all discover by the catalogue's names, and
    # SideCrab is single-instance by construction anyway - port 2722 is fixed in the component
    # catalogue, in the hook fragment's URLs and in the status-line command, so a second
    # install could never have run beside the first. All the switch ever bought was an install
    # the rest of the toolchain could not see. See the "single instance" test in setup\tests.
    [switch] $SkipTask,
    [switch] $SkipHooks,
    [switch] $SkipStatusLine,
    [switch] $WithGlow,
    [switch] $WithToast,
    [switch] $WithApprovals,
    [switch] $ForceEnable,
    [switch] $Status,
    # Prints the approval pairing code (crabd 0.29.0) and exits. The code goes into iCUE's
    # widget settings under "Approval Pairing Code"; Approve/Deny taps are refused without it.
    [switch] $PairingCode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$HookUrlMarker = '127.0.0.1:2722/v1/hook'
$TokenPath     = Join-Path (Split-Path -Parent $ConfigPath) 'panel-token'

function Write-Step { param([string] $Message) Write-Host "  $Message" }

function Backup-File {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Path.sidecrab-bak-$stamp"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    return $backup
}

function Merge-HookFragment {
    param([hashtable] $Settings, [hashtable] $Fragment)

    if (-not $Settings.ContainsKey('hooks') -or $Settings['hooks'] -isnot [hashtable]) {
        $Settings['hooks'] = @{}
    }
    $added = 0
    foreach ($eventName in $Fragment.Keys) {
        $existing = @()
        if ($Settings['hooks'].ContainsKey($eventName)) {
            # Drop any prior SideCrab ENTRY so a re-run replaces rather than duplicates - but
            # entry-level, not matcher-level: a hook a human hand-merged into one of our
            # matchers is theirs, and dropping the matcher whole ate it on every re-install
            # (the defect uninstall/Restore were fixed for). Split-SideCrabHookMatcher hands
            # back the ORIGINAL object for an unshared matcher, so the normal path stays
            # byte-identical; only a genuinely shared matcher is rebuilt, minus our entries.
            foreach ($matcher in @($Settings['hooks'][$eventName])) {
                $part = Split-SideCrabHookMatcher -Matcher $matcher -Marker $HookUrlMarker
                if ($null -ne $part.Foreign) { $existing += , $part.Foreign }
            }
        }
        $Settings['hooks'][$eventName] = @($existing + @($Fragment[$eventName]))
        $added++
    }
    return $added
}

function Get-ComponentPlan {
    <# Catalogue -> filesystem probe -> decision. The decision itself is pure and
       lives in Select-SideCrabComponent; this is the thin impure wrapper. #>
    param([string] $RepoRoot, [bool] $WithGlow, [bool] $WithToast)

    $spec = Get-SideCrabComponentSpec -RepoRoot $RepoRoot
    $present = @{}
    foreach ($c in $spec) { $present[$c.Key] = [bool](Test-Path -LiteralPath $c.Script) }

    @(Select-SideCrabComponent -Spec $spec -Present $present `
                               -Requested @{ glow = $WithGlow; toast = $WithToast })
}

function Show-Status {
    <# Read-only by design: probes tasks, /v1/health and settings.json, writes nothing. #>
    param([object[]] $Plan, [string] $RepoRoot, [string] $SettingsPath,
          [string] $ConfigPath, [string] $ChainPath)

    Write-Host 'SideCrab status'
    Write-Step "repo:    $RepoRoot"

    Write-Step 'tasks:'
    foreach ($c in $Plan) {
        $state = Get-SideCrabTaskState -TaskName $c.TaskName
        if ($state.Registered) {
            # LastTaskResult arrives signed; masking to 32 bits keeps a negative HRESULT
            # printable ([uint32] on a negative value throws).
            $result = if ($null -ne $state.LastTaskResult) {
                          '0x{0:X8}' -f ([int64] $state.LastTaskResult -band 0xFFFFFFFFL)
                      } else { 'n/a' }
            $last = if ($state.LastRunTime) { $state.LastRunTime } else { 'never' }
            $note = if ($state.State -eq 'Disabled') { '  [a re-run leaves it disabled; -ForceEnable to re-enable]' } else { '' }
            Write-Host ('    {0,-16} {1,-10} last run {2}  result {3}{4}' -f `
                        $c.TaskName, $state.State, $last, $result, $note)
        } else {
            $why = if ($c.Present) { 'script present, task not registered' }
                   else            { "script absent ($($c.Script))" }
            Write-Host ('    {0,-16} {1,-10} {2}' -f $c.TaskName, 'not-registered', $why)
        }
    }

    $health = Get-SideCrabHealth
    if ($health.Reachable) {
        $verdict = if ($health.Ok) { 'ok' } else { 'reachable but not ok' }
        Write-Step "health:  $verdict  crabd $($health.Version)  ($($health.Uri))"
    } else {
        Write-Step "health:  unreachable  ($($health.Uri)) - $($health.Error)"
    }

    $settings = $null
    try { $settings = Read-SideCrabSettings -SettingsPath $SettingsPath }
    catch { Write-Step "hooks:   $SettingsPath unreadable - $($_.Exception.Message)" }
    if ($null -eq $settings) {
        if (-not (Test-Path -LiteralPath $SettingsPath)) {
            Write-Step "hooks:   $SettingsPath not present"
        }
    } else {
        $events = @(Get-SideCrabHookEvent -Settings $settings -Marker $HookUrlMarker)
        if ($events.Count -eq 0) {
            Write-Step "hooks:   no SideCrab entries in $SettingsPath"
        } else {
            $total = ($events | Measure-Object -Property Count -Sum).Sum
            Write-Step "hooks:   $total entr(ies) across $($events.Count) event(s) in $SettingsPath"
            Write-Host ('           ' + (($events | ForEach-Object { $_.Event }) -join ', '))
        }
    }

    $toastSelected = [bool] @($Plan | Where-Object { $_.Key -eq 'toast' -and $_.Selected })
    $aumid = Get-SideCrabAumidState -RepoRoot $RepoRoot
    if ($aumid.Registered) {
        $note = if ($aumid.Current) { 'current' } else { 'values differ - re-run install to update' }
        Write-Step "aumid:   $($aumid.Aumid) registered ($note)"
    } elseif ($toastSelected) {
        Write-Step "aumid:   $($aumid.Aumid) not registered - toasts group under 'Windows PowerShell'"
    } else {
        Write-Step "aumid:   not registered (toast component not installed)"
    }

    # One line per scheme: an unregistered scheme is a toast button the shell silently no-ops,
    # so "the protocol is registered" is not a single fact.
    foreach ($proto in @(Get-SideCrabProtocolState -RepoRoot $RepoRoot)) {
        if ($proto.Registered) {
            $note = if ($proto.Current) { 'current' } else { 'command differs - re-run install to update' }
            Write-Step "proto:   $($proto.Scheme): registered ($note)"
        } elseif ($toastSelected) {
            Write-Step "proto:   $($proto.Scheme): not registered - the toast $($proto.Button) button will not resolve"
        } else {
            Write-Step "proto:   $($proto.Scheme): not registered (toast component not installed)"
        }
    }

    # status line (read-only): is OUR command installed, and did we save a prior to chain to?
    $slSpec = Get-SideCrabStatusLineSpec -RepoRoot $RepoRoot
    $slCmd  = $null
    if ($null -ne $settings -and $settings.Contains('statusLine') -and
        $settings['statusLine'] -is [System.Collections.IDictionary]) {
        $slCmd = "$($settings['statusLine']['command'])"
    }
    if (Test-SideCrabStatusLineIsOurs -Command $slCmd) {
        $saved = Get-SideCrabSavedStatusLine -ChainPath $ChainPath
        $chain = if (-not $saved.Present)      { 'no prior saved' }
                 elseif ($null -eq $saved.StatusLine) { 'chains to nothing (none existed)' }
                 else                          { 'chains to a saved prior status line' }
        Write-Step "statusln: SideCrab installed - $chain"
    } elseif ($slCmd) {
        Write-Step 'statusln: a non-SideCrab status line is configured (run install to take it over)'
    } else {
        Write-Step 'statusln: none configured'
    }

    # panel approvals (read-only). ENABLED is security-relevant - widget taps then decide REAL
    # tool-call permissions - and is only armed when the PermissionRequest hook reaches crabd, so
    # surface the hook wiring alongside the posture rather than leaving ON a bare word (SET-a1).
    $pa = Get-SideCrabPanelApprovalsState -ConfigPath $ConfigPath
    if ($null -eq $pa.Enabled -or -not $pa.Enabled) {
        $paState = if ($null -eq $pa.Enabled) { 'default OFF (key absent)' } else { 'disabled' }
        Write-Step "approv:  panelApprovals $paState - widget taps cannot decide tool permissions"
    } else {
        $permWired = [bool] @(@(Get-SideCrabHookEvent -Settings $settings -Marker $HookUrlMarker) |
                              Where-Object { $_.Event -eq 'PermissionRequest' })
        Write-Step "approv:  panelApprovals ENABLED - widget taps can allow/deny tool calls; PermissionRequest hook $(if ($permWired) { 'wired' } else { 'NOT wired - approvals never arm' })"
    }

    # the pairing code (crabd 0.29.0): its PRESENCE is status; the code itself is printed only
    # by -PairingCode, so a status paste into a ticket never carries it.
    $tok = Get-SideCrabPanelToken -TokenPath $TokenPath
    if ($tok.Present) {
        Write-Step "pairing: code present ($TokenPath) - print it with -PairingCode, enter it in iCUE > widget settings > Approval Pairing Code"
    } else {
        Write-Step "pairing: NO code yet ($TokenPath) - crabd 0.29.0+ mints it on first start; until it exists Approve/Deny taps are refused"
    }

    $widget = Get-SideCrabWidgetVersion -RepoRoot $RepoRoot
    if ($widget) { Write-Step "widget:  manifest $widget (installed into iCUE by import, not by this script)" }

    Write-Host 'Nothing was changed.'
}

# ------------------------------------------------------------------------------ run

$plan = Get-ComponentPlan -RepoRoot $RepoRoot -WithGlow $WithGlow.IsPresent `
                          -WithToast $WithToast.IsPresent

if ($PairingCode) {
    $tok = Get-SideCrabPanelToken -TokenPath $TokenPath
    if ($tok.Present) {
        Write-Host "Approval pairing code: $($tok.Code)"
        Write-Host "Enter it in iCUE > the SideCrab widget's settings > Approval Pairing Code. Approve/Deny taps are refused until it matches."
        exit 0
    }
    Write-Host "No pairing code at $TokenPath - crabd 0.29.0 or newer mints one on its first start. Start (or update) crabd, then run this again."
    exit 1
}

if ($Status) {
    Show-Status -Plan $plan -RepoRoot $RepoRoot -SettingsPath $SettingsPath `
                -ConfigPath $ConfigPath -ChainPath $ChainPath
    return
}

$fragment = Join-Path $RepoRoot 'hooks\settings-hooks-fragment.json'

$problems = @($plan | Where-Object { $_.Selected -and $_.Problem })
if ($problems.Count -gt 0) { throw (($problems | ForEach-Object { $_.Problem }) -join '; ') }
if (-not $SkipHooks -and -not (Test-Path -LiteralPath $fragment)) {
    throw "hook fragment not found at $fragment"
}

Write-Host 'SideCrab install'

if (-not $SkipTask) {
    $python = Resolve-SideCrabPython
    Write-Step "python:  $python"
    foreach ($c in @($plan | Where-Object Selected)) {
        Write-Step "$($c.Key.PadRight(8)) $($c.Script)  [$($c.Reason)]"

        # DOES ITS BINDING ACTUALLY IMPORT? Only glow declares one (cuesdk). Its script file
        # existing is what auto-selects it, and that says nothing about the SDK: without cuesdk
        # the launcher starts, raises ImportError, exits, and the task sits there Registered and
        # green having never controlled a light. Run under the SAME interpreter the task will
        # use - a different python on PATH is a different set of site-packages.
        if ($c.PyImport) {
            # try/catch as well as the exit code: with
            # $PSNativeCommandUseErrorActionPreference on (it is $false by default on 7.6.4,
            # measured 2026-08-27, but a profile can set it), a non-zero native exit THROWS
            # under $ErrorActionPreference='Stop' - and a failed import check must report a
            # missing SDK, never abort the whole install.
            $importable = $false
            try {
                & $python -c "import $($c.PyImport)" 2>&1 | Out-Null
                $importable = ($LASTEXITCODE -eq 0)
            } catch { $importable = $false }
            $pre = Get-SideCrabGlowPreflight -Selected $true -Requested $c.Requested `
                                             -Importable $importable -Module $c.PyImport `
                                             -RequirementsPath $c.PyRequires
            if ($pre.Status -ne 'ok') {
                Write-Host "    $($c.Key.PadRight(8)) $($pre.Reason)" -ForegroundColor Yellow
                Write-Host "    $(''.PadRight(8)) fix: $($pre.Command)" -ForegroundColor Yellow
            }
            if (-not $pre.Install) {
                Write-Host "    $($c.Key.PadRight(8)) NOT registered - pass $($c.Switch) to install it anyway" -ForegroundColor Yellow
                continue
            }
        }

        if ($PSCmdlet.ShouldProcess($c.TaskName, 'Register scheduled task')) {
            $reg = Register-SideCrabTask -TaskName $c.TaskName -PythonExe $python `
                                         -ScriptPath $c.Script -Description $c.Description `
                                         -ForceEnable:$ForceEnable
            Write-Step "task:    '$($c.TaskName)' registered (at logon, hidden, restarts on failure) - $($reg.Reason)"
            if ($reg.Start) {
                Start-ScheduledTask -TaskName $c.TaskName
                Write-Step "task:    '$($c.TaskName)' started"
            } else {
                # Loud on purpose: the operator disabled this for a reason (glow: docs/BACKLOG.md),
                # and a silent "registered" line would read as if it were running.
                Write-Host "    task:    '$($c.TaskName)' LEFT DISABLED and not started - it was disabled before this run. Pass -ForceEnable to re-enable it." -ForegroundColor Yellow
            }
        }
        if ($c.Key -eq 'toast') {
            # Every HKCU registration ships WITH the toast component, not beside it: an app
            # identity and the button schemes are only meaningful while a notifier exists to
            # raise toasts, and an install without toast must not leave any of them behind
            # (Uninstall removes them either way).
            $aumid = Set-SideCrabAumid -RepoRoot $RepoRoot
            Write-Step "aumid:   $($aumid.Aumid) [$($aumid.Action)] $($aumid.RegistryPath)"
            foreach ($proto in @(Set-SideCrabProtocol -RepoRoot $RepoRoot -PythonExe $python)) {
                Write-Step "proto:   $($proto.Scheme): [$($proto.Action)] $($proto.RegistryPath)"
            }
        }
    }
    foreach ($c in @($plan | Where-Object { -not $_.Selected })) {
        Write-Step "$($c.Key.PadRight(8)) skipped ($($c.Reason)) - $($c.Script)"
    }
} else {
    Write-Step 'task:    skipped (-SkipTask)'
}

# settings.json: hooks merge + status-line command in one read / one backup / one write.
$doStatusLine = -not $SkipStatusLine
$slSpec = Get-SideCrabStatusLineSpec -RepoRoot $RepoRoot
if ($doStatusLine -and -not (Test-Path -LiteralPath $slSpec.Script)) {
    throw "status-line script not found at $($slSpec.Script)"
}

if (-not $SkipHooks -or $doStatusLine) {
    $settings = @{}
    if (Test-Path -LiteralPath $SettingsPath) {
        $raw = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8
        if ($raw.Trim()) {
            $settings = $raw | ConvertFrom-Json -AsHashtable -Depth 40
            if ($settings -isnot [hashtable]) { throw "$SettingsPath is not a JSON object" }
        }
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SettingsPath) | Out-Null
    }

    if ($PSCmdlet.ShouldProcess($SettingsPath, 'Merge SideCrab hooks / install status line')) {
        $backup = Backup-File -Path $SettingsPath
        if ($backup) { Write-Step "backup:  $backup" }

        if (-not $SkipHooks) {
            $fragmentHooks = (Get-Content -LiteralPath $fragment -Raw -Encoding utf8 |
                              ConvertFrom-Json -AsHashtable -Depth 40)['hooks']
            $count = Merge-HookFragment -Settings $settings -Fragment $fragmentHooks
            Write-Step "hooks:   $count event(s) merged into $SettingsPath"
        } else {
            Write-Step 'hooks:   skipped (-SkipHooks)'
        }

        if ($doStatusLine) {
            # Console python: the status line renders this command's stdout, and pythonw has none.
            $pyConsole   = Resolve-SideCrabPythonConsole
            $slCommand   = Get-SideCrabStatusLineCommand -PythonExe $pyConsole -ScriptPath $slSpec.Script
            $existingSL  = if ($settings.ContainsKey('statusLine')) { $settings['statusLine'] } else { $null }
            $existingCmd = if ($existingSL -is [hashtable]) { "$($existingSL['command'])" } else { '' }
            if (-not (Test-SideCrabStatusLineIsOurs -Command $existingCmd)) {
                # Save the operator's prior status line - once - so the chain script can call
                # it and Uninstall can restore it. Skipped when it is already ours: re-saving
                # would capture our own command as its prior and build an endless chain loop.
                Save-SideCrabPriorStatusLine -ChainPath $ChainPath -PriorStatusLine $existingSL | Out-Null
                Write-Step "chain:   prior status line saved to $ChainPath$(if ($null -eq $existingSL) { ' (none existed)' })"
            }
            $ours = @{ type = 'command'; command = $slCommand }
            # Carry the operator's padding forward so the line keeps its spacing.
            if ($existingSL -is [hashtable] -and $existingSL.ContainsKey('padding')) { $ours['padding'] = $existingSL['padding'] }
            $settings['statusLine'] = $ours
            Write-Step "statusln: $slCommand"
        } else {
            Write-Step 'statusln: skipped (-SkipStatusLine)'
        }

        # -Depth must exceed the hooks nesting or ConvertTo-Json silently stringifies it.
        $json = $settings | ConvertTo-Json -Depth 40
        Set-Content -LiteralPath $SettingsPath -Value $json -Encoding utf8NoBOM
    }
} else {
    Write-Step 'hooks:   skipped (-SkipHooks)'
    Write-Step 'statusln: skipped (-SkipStatusLine)'
}

# panel approval (config.json) - OFF by default; -WithApprovals turns it on.
if ($PSCmdlet.ShouldProcess($ConfigPath, 'Configure panelApprovals')) {
    if ($WithApprovals) {
        $res = Set-SideCrabPanelApprovals -ConfigPath $ConfigPath -Enabled $true
        Write-Step "approv:  panelApprovals.enabled = TRUE (was $(if ($null -eq $res.Previous) { 'unset' } else { $res.Previous }))"
        Write-Host '  SECURITY: panel approvals are ON. Approve/Deny taps on the on-glass widget can now allow or reject tool calls; crabd holds each permission prompt up to 55s, NEVER auto-allows, and falls back to the terminal dialog on no-tap. Disable with Uninstall-SideCrab.ps1 or by setting panelApprovals.enabled=false.' -ForegroundColor Yellow
        $tok = Get-SideCrabPanelToken -TokenPath $TokenPath
        if ($tok.Present) {
            Write-Host "  PAIRING: taps are only honoured with the pairing code. Enter $($tok.Code) in iCUE > widget settings > Approval Pairing Code (print it again any time with -PairingCode)." -ForegroundColor Yellow
        } else {
            Write-Host "  PAIRING: no code yet at $TokenPath - crabd 0.29.0+ mints it on first start. Re-run with -PairingCode once crabd is up, then enter the code in iCUE > widget settings." -ForegroundColor Yellow
        }
    } else {
        # Default OFF. Only WRITE false when the key is absent, so a plain re-run does not
        # silently revert an operator who deliberately chose -WithApprovals earlier.
        $state = Get-SideCrabPanelApprovalsState -ConfigPath $ConfigPath
        if ($null -eq $state.Enabled) {
            Set-SideCrabPanelApprovals -ConfigPath $ConfigPath -Enabled $false | Out-Null
            Write-Step 'approv:  panelApprovals.enabled = false (default; pass -WithApprovals to enable)'
        } else {
            Write-Step "approv:  panelApprovals.enabled = $($state.Enabled) (unchanged; -WithApprovals to enable)"
        }
    }
}

Write-Host 'Done. Check http://127.0.0.1:2722/v1/health'

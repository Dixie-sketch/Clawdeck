#Requires -Version 7.0
<#
.SYNOPSIS
    Shared helpers for the SideCrab setup scripts (Install / Uninstall / Update).

.DESCRIPTION
    Dot-sourced by the three setup scripts. Nothing here runs at load time: the file
    defines functions only, so dot-sourcing it can never change machine state.

    The pure decision helpers - Get-SideCrabComponentSpec, Select-SideCrabComponent,
    Get-SideCrabTaskName, Get-SideCrabHookEvent - take every input as a parameter and
    touch neither the filesystem, the task scheduler nor the network. setup/tests lifts
    them out of this file by AST and exercises them without running an install.
#>

# ---------------------------------------------------------------- pure decisions

function Get-SideCrabComponentSpec {
    <# The catalogue of installable components. Pure: Join-Path does no I/O.
       Adding a component here is all that Install/Uninstall/Update/-Status need. #>
    param([Parameter(Mandatory)][string] $RepoRoot)

    @(
        [pscustomobject]@{
            Key         = 'crabd'
            TaskName    = 'SideCrab-crabd'
            Script      = (Join-Path $RepoRoot 'companion\crabd.py')
            Required    = $true
            Switch      = $null
            # The ONE component that owns a TCP port - written down here so a restart path can
            # ASK rather than assume (0 = owns no port). crabd sets allow_reuse_address = False
            # on purpose, so a restart that does not wait for this port to be released loses
            # the bind race: see Wait-SideCrabPortRelease.
            Port        = 2722
            # EVERY file whose mtime makes the running process stale, not just the entry point.
            # The doctor's freshness check reads the NEWEST of these: a component whose entry
            # point is a three-line launcher (glow) would otherwise report "current" after a
            # rewrite of the module that does all the work. Entry point first by convention.
            WatchFiles  = @((Join-Path $RepoRoot 'companion\crabd.py'))
            # $null = stdlib only, nothing to import-check. Present on every row so a caller
            # under Set-StrictMode can read it without testing for the property first.
            PyImport    = $null
            PyRequires  = $null
            Description = 'SideCrab companion (crabd) - serves /v1/state to the Xeneon Edge widget.'
        }
        [pscustomobject]@{
            Key         = 'glow'
            TaskName    = 'SideCrab-glow'
            Port        = 0
            # glow_launcher.pyw, NOT sidecrab_glow.py: cuesdk hard-crashes (0xC000001D) under a
            # console-less pythonw; the launcher hands off to python.exe with CREATE_NO_WINDOW.
            Script      = (Join-Path $RepoRoot 'lighting\glow_launcher.pyw')
            # The launcher is 26 lines that import sidecrab_glow.main. Watching it alone made
            # every change to the glow itself invisible to the stale-code check.
            WatchFiles  = @(
                (Join-Path $RepoRoot 'lighting\glow_launcher.pyw')
                (Join-Path $RepoRoot 'lighting\sidecrab_glow.py')
                (Join-Path $RepoRoot 'lighting\icue.py')
                (Join-Path $RepoRoot 'lighting\decision.py')
            )
            Required    = $false
            Switch      = '-WithGlow'
            # Importable-at-install-time, checked before the task is registered: a glow task
            # whose cuesdk is missing registers, runs, exits and reads as installed.
            PyImport    = 'cuesdk'
            PyRequires  = (Join-Path $RepoRoot 'lighting\requirements.txt')
            Description = 'SideCrab glow - drives iCUE lighting from crabd state.'
        }
        [pscustomobject]@{
            Key         = 'toast'
            TaskName    = 'SideCrab-toast'
            Script      = (Join-Path $RepoRoot 'notifier\sidecrab_toast.py')
            # The two *_handler.pyw files are deliberately NOT here: the shell launches them as
            # their own processes on a button press, so their mtime says nothing about the age
            # of the running toast daemon.
            WatchFiles  = @((Join-Path $RepoRoot 'notifier\sidecrab_toast.py'))
            Required    = $false
            Port        = 0
            Switch      = '-WithToast'
            # Measured 2026-08-27: sidecrab_toast.py imports stdlib only (it raises toasts by
            # shelling out, not through a binding), so there is nothing to import-check.
            PyImport    = $null
            PyRequires  = $null
            Description = 'SideCrab toast - raises Windows notifications from crabd state.'
        }
    )
}

function Select-SideCrabComponent {
    <# Decides which components an install covers, from three inputs only:
       the catalogue, which script files exist, and which switches were passed.

       Required components are always selected. An optional one is selected when its
       switch was passed OR - the default path - when its script file is present.
       A switch passed for a script that is not there is a Problem, never a silent
       skip: asking for -WithGlow and getting nothing is the worst outcome. #>
    param(
        [Parameter(Mandatory)][object[]] $Spec,
        [System.Collections.IDictionary] $Present   = @{},   # Key -> [bool] script file exists
        [System.Collections.IDictionary] $Requested = @{}    # Key -> [bool] switch was passed
    )

    foreach ($c in $Spec) {
        $isPresent   = [bool]($Present.Contains($c.Key)   -and $Present[$c.Key])
        $isRequested = [bool]($Requested.Contains($c.Key) -and $Requested[$c.Key])

        $selected = $false
        $reason   = 'not-installed'
        $problem  = $null

        if ($c.Required) {
            $selected = $true
            $reason   = 'required'
            if (-not $isPresent) { $problem = "$($c.Key) script not found at $($c.Script)" }
        }
        elseif ($isRequested) {
            $selected = $true
            $reason   = 'requested'
            if (-not $isPresent) {
                $problem = "$($c.Switch) was specified but $($c.Script) does not exist"
            }
        }
        elseif ($isPresent) {
            $selected = $true
            $reason   = 'auto-detected'
        }

        [pscustomobject]@{
            Key         = $c.Key
            TaskName    = $c.TaskName
            Script      = $c.Script
            Required    = $c.Required
            Switch      = $c.Switch
            # Carried through, not re-derived: a plan row that lost the port would send a
            # restart path back to guessing which component binds one. Same for WatchFiles
            # and PyImport - a plan row that dropped them silently re-narrows the freshness
            # check to the entry point and drops the import preflight.
            Port        = $c.Port
            WatchFiles  = @($c.WatchFiles)
            PyImport    = $c.PyImport
            PyRequires  = $c.PyRequires
            Description = $c.Description
            Present     = $isPresent
            Requested   = $isRequested
            Selected    = $selected
            Reason      = $reason
            Problem     = $problem
        }
    }
}

function Get-SideCrabAumidSpec {
    <# The toast app identity, as one object every caller reads instead of re-deriving.
       Pure: Join-Path does no I/O and the registry is not touched here.

       WHY THIS EXISTS: without a registered AppUserModelID the notifier has to borrow
       Windows PowerShell's, so Action Center groups SideCrab's toasts under "Windows
       PowerShell" and Windows' per-app notification switch for SideCrab is really
       PowerShell's. Registering our own is a HKCU write - no elevation, no COM server,
       no Start-menu shortcut - and the notifier picks it up by probing for this key.

       ComponentKey ties the registration to the toast component: an install without
       toast must not leave an app identity behind for a notifier that is not there. #>
    param([Parameter(Mandatory)][string] $RepoRoot)

    [pscustomobject]@{
        Aumid        = 'SideCrab.Notifier'
        RegistryPath = 'HKCU:\SOFTWARE\Classes\AppUserModelId\SideCrab.Notifier'
        DisplayName  = 'SideCrab'
        IconUri      = (Join-Path $RepoRoot 'notifier\sidecrab.ico')
        ComponentKey = 'toast'
    }
}

function Get-SideCrabAumidIconDecision {
    <# What the AUMID key's IconUri value SHOULD be, given whether the icon file is actually
       on disk, and what to do about the value that is there now. Pure.

       THE TRAP THIS CLOSES: the icon file can go (a repo move, a checkout without it) while
       the registry keeps pointing at where it used to be. Both halves failed independently -
       the state read compared IconUri against the spec path and called that "current" without
       ever asking whether the file existed, and the re-registration wrote DisplayName only
       and left the dead IconUri in place, because New-ItemProperty -Force does not clear a
       value it is not given a value for. The result was a key that reads healthy, renders no
       icon, and cannot be repaired by re-running the installer.

       Expected is $null when the icon is missing: an IconUri pointing at nothing renders as no
       icon with NO error, which is strictly worse than an absent value that reads as "not set"
       on inspection. Remove is $true only when a value is registered that Expected says should
       not be - so the caller deletes rather than leaving a dead pointer behind. #>
    param(
        [bool]   $IconPresent,
        [string] $SpecIconUri,
        [AllowNull()] $RegisteredIconUri
    )

    $expected   = if ($IconPresent) { $SpecIconUri } else { $null }
    $registered = if ($null -eq $RegisteredIconUri) { $null } else { [string] $RegisteredIconUri }

    [pscustomobject]@{
        Expected = $expected
        # The value on the key already says what it should - nothing to write, nothing to clear.
        Matches  = [bool] ($registered -eq $expected)
        # A registered pointer with no icon behind it. Deleting it is the repair.
        Remove   = [bool] ($null -eq $expected -and $null -ne $registered)
        Reason   = if ($IconPresent) { 'icon present - IconUri names it' }
                   elseif ($null -ne $registered) { 'icon MISSING - the registered IconUri is a dead pointer and is removed' }
                   else { 'icon missing - no IconUri registered, which is the honest state' }
    }
}

function Get-SideCrabProtocolSpec {
    <# The toast's buttons, ONE ROW PER URL SCHEME - the table every caller loops over
       instead of re-deriving. Pure: Join-Path does no I/O and the registry is not touched.

       WHY A PROTOCOL: a toast sits in Action Center until it is dismissed, long after the
       notifier process that raised it may have gone. An activation that called back into
       that process would be dead on arrival; a registered URL scheme is routed by the
       shell, which is still there. Each handler validates the URI it receives - see
       notifier\sidecrab_ack_handler.pyw and notifier\sidecrab_snooze_handler.pyw.

       TWO SCHEMES, NOT ONE (v0.16.0): Acknowledge answers the question, Snooze defers the
       toast for 30 minutes and deliberately never touches crabd (notifier\README.md,
       "Snooze 30m"). They are separate schemes because they are separate handlers: an
       unregistered scheme is a button the shell silently no-ops, which is how Snooze
       shipped inert. Adding a row here is all Install/Uninstall/-Status need.

       ComponentKey ties every registration to the toast component, exactly as the AUMID
       does: an install without toast must not leave a scheme pointing at a handler for a
       notifier that is not there. #>
    param([Parameter(Mandatory)][string] $RepoRoot)

    @(
        [pscustomobject]@{
            Key          = 'ack'
            Scheme       = 'sidecrab-ack'
            RegistryPath = 'HKCU:\SOFTWARE\Classes\sidecrab-ack'
            CommandPath  = 'HKCU:\SOFTWARE\Classes\sidecrab-ack\shell\open\command'
            Description  = 'URL:SideCrab Acknowledge'
            Handler      = (Join-Path $RepoRoot 'notifier\sidecrab_ack_handler.pyw')
            Button       = 'Acknowledge'
            ComponentKey = 'toast'
        }
        [pscustomobject]@{
            Key          = 'snooze'
            Scheme       = 'sidecrab-snooze'
            RegistryPath = 'HKCU:\SOFTWARE\Classes\sidecrab-snooze'
            CommandPath  = 'HKCU:\SOFTWARE\Classes\sidecrab-snooze\shell\open\command'
            Description  = 'URL:SideCrab Snooze'
            Handler      = (Join-Path $RepoRoot 'notifier\sidecrab_snooze_handler.pyw')
            Button       = 'Snooze 30m'
            ComponentKey = 'toast'
        }
    )
}

function Get-SideCrabProtocolCommand {
    <# The exact (Default) string of shell\open\command. Pure.

       Every one of the three quoted parts is load-bearing: an unquoted interpreter or
       handler path breaks on the space in "Program Files", and a bare %1 hands a URI
       containing a space to the handler as two arguments. #>
    param(
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][string] $HandlerPath
    )
    '"{0}" "{1}" "%1"' -f $PythonExe, $HandlerPath
}

function Get-SideCrabTaskName {
    <# Task names from a component list. Default: only the selected ones (what an
       install registers). -All: every known name (what an uninstall sweeps). #>
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]] $Component,
        [switch] $All
    )

    $rows = @($Component)
    if (-not $All) {
        # A raw Spec row has no Selected property; -All is the only valid call for it.
        $rows = @($rows | Where-Object { $_.PSObject.Properties.Name -contains 'Selected' -and $_.Selected })
    }
    # Emitted unrolled, not as a single array object: callers wrap in @() and a
    # comma-wrapped return would hand them an array nested one level deep.
    $rows | ForEach-Object { $_.TaskName }
}

function Get-SideCrabTaskEnableDecision {
    <# Should a re-registration leave the task DISABLED, and should it be started? Pure.

       The defect this exists for (measured 2026-08-26): Register-ScheduledTask -Force always
       writes an enabled task, so re-running the installer resurrected SideCrab-glow - parked
       with Disable-ScheduledTask because the Corsair SDK crashes headless (docs/BACKLOG.md) -
       and then started it into that crash. Re-registering a disabled task is fine and keeps
       its action/path current; STARTING it, or leaving it enabled, overturns a decision the
       operator made deliberately. -ForceEnable is the only way to overturn it. #>
    param(
        [bool]   $Registered,
        [string] $PriorState,
        [bool]   $ForceEnable
    )

    $wasDisabled = $Registered -and ($PriorState -eq 'Disabled')
    if ($wasDisabled -and -not $ForceEnable) {
        return [pscustomobject]@{
            WasDisabled = $true; LeaveDisabled = $true; Start = $false
            Reason = 'was DISABLED - re-registered and left disabled (-ForceEnable to override)'
        }
    }
    if ($wasDisabled) {
        return [pscustomobject]@{
            WasDisabled = $true; LeaveDisabled = $false; Start = $true
            Reason = 'was DISABLED - re-enabled by -ForceEnable'
        }
    }
    [pscustomobject]@{
        WasDisabled = $false; LeaveDisabled = $false; Start = $true
        Reason = if ($Registered) { "re-registered (was $PriorState)" } else { 'newly registered' }
    }
}

function Get-SideCrabHookEvent {
    <# Which settings.json hook events carry a SideCrab entry, and how many each.
       Matched on the crabd URL substring - the same marker the merge/remove use. #>
    param(
        $Settings,
        [string] $Marker = '127.0.0.1:2722/v1/hook'
    )

    if ($null -eq $Settings -or $Settings -isnot [System.Collections.IDictionary]) { return @() }
    if (-not $Settings.Contains('hooks')) { return @() }
    $hooks = $Settings['hooks']
    if ($hooks -isnot [System.Collections.IDictionary]) { return @() }

    $out = @()
    foreach ($eventName in @($hooks.Keys)) {
        $n = 0
        foreach ($matcher in @($hooks[$eventName])) {
            if ($matcher -is [System.Collections.IDictionary] -and $matcher.Contains('hooks')) {
                foreach ($h in @($matcher['hooks'])) {
                    # A command hook carries the marker in 'command'; an http hook (Stop,
                    # PermissionRequest) carries it in 'url'. Concatenate - a hook has only
                    # one of the two - so the same marker finds both kinds.
                    if ($h -is [System.Collections.IDictionary] -and "$($h['command'])$($h['url'])" -like "*$Marker*") { $n++ }
                }
            }
        }
        if ($n -gt 0) { $out += [pscustomobject]@{ Event = $eventName; Count = $n } }
    }
    $out          # unrolled - see Get-SideCrabTaskName
}

# ------------------------------------------------------------------ status line + config

function Get-SideCrabStatusLineSpec {
    <# The status-line command, as one object every caller reads instead of re-deriving.
       Pure: Join-Path does no I/O.

       The status-line command's stdout is what Claude Code renders, so it must run under a
       console python (python.exe), NOT the windowless pythonw the daemons use - see
       Resolve-SideCrabPythonConsole. Marker is the substring that identifies OUR command in
       a settings.json statusLine, the way the hook URL marker identifies our hooks. #>
    param([Parameter(Mandatory)][string] $RepoRoot)

    [pscustomobject]@{
        Script    = (Join-Path $RepoRoot 'hooks\sidecrab_statusline.py')
        ChainPath = (Join-Path $HOME '.sidecrab\statusline-chain.json')
        Marker    = 'sidecrab_statusline'
    }
}

function Get-SideCrabStatusLineCommand {
    <# The exact statusLine command string. Pure. Both quoted parts are load-bearing: an
       unquoted interpreter or script path breaks on the space in "Program Files". #>
    param(
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][string] $ScriptPath
    )
    '"{0}" "{1}"' -f $PythonExe, $ScriptPath
}

function Test-SideCrabStatusLineIsOurs {
    <# Does a settings.json statusLine command belong to SideCrab? Matched on the script
       marker, the same idea as the hook URL marker. Pure. #>
    param([string] $Command)
    [bool] ($Command -and $Command -like '*sidecrab_statusline*')
}

function Resolve-SideCrabPythonConsole {
    <# The console python.exe for the status-line command. NEVER pythonw.exe: pythonw has no
       usable stdout, and Claude Code reads the status-line command's stdout. Otherwise the
       same rule as Resolve-SideCrabPython - the WindowsApps alias stub does not count. #>
    foreach ($name in 'python.exe', 'python3.exe') {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Source -like '*\WindowsApps\*') { continue }
            return $cmd.Source
        }
    }
    throw 'No usable console python.exe found on PATH (the WindowsApps alias stub does not count). Install Python 3.13.'
}

function Save-SideCrabPriorStatusLine {
    <# Records the operator's pre-existing statusLine so the chain script can call it and the
       uninstaller can restore it. A $null prior is saved as {"statusLine": null}: the file's
       PRESENCE marks that SideCrab took the slot; null means there was nothing before us.

       Callers MUST only save when the current statusLine is NOT already ours - re-saving our
       own command would capture the chain script as its own prior and build a loop. #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string] $ChainPath,
        $PriorStatusLine            # hashtable {type,command,padding?} or $null
    )
    if ($PSCmdlet.ShouldProcess($ChainPath, 'Save prior status line')) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ChainPath) | Out-Null
        $json = @{ statusLine = $PriorStatusLine } | ConvertTo-Json -Depth 40
        Set-Content -LiteralPath $ChainPath -Value $json -Encoding utf8NoBOM
    }
    $ChainPath
}

function Get-SideCrabSavedStatusLine {
    <# The prior statusLine the installer saved: {Present, StatusLine}. Present is $false only
       when the chain file is absent (SideCrab never took the slot, or a prior uninstall
       already restored). StatusLine is the saved object, or $null when the saved value was
       null / no prior existed. Read-only, never throws. #>
    param([Parameter(Mandatory)][string] $ChainPath)

    if (-not (Test-Path -LiteralPath $ChainPath)) {
        return [pscustomobject]@{ Present = $false; StatusLine = $null }
    }
    try {
        $raw = Get-Content -LiteralPath $ChainPath -Raw -Encoding utf8
        $doc = if ($raw.Trim()) { $raw | ConvertFrom-Json -AsHashtable -Depth 40 } else { @{} }
    } catch {
        return [pscustomobject]@{ Present = $false; StatusLine = $null }
    }
    $sl = if ($doc -is [System.Collections.IDictionary] -and $doc.Contains('statusLine')) { $doc['statusLine'] } else { $null }
    [pscustomobject]@{ Present = $true; StatusLine = $sl }
}

function Backup-SideCrabFile {
    <# Copy $Path to a timestamped "<path>.sidecrab-bak-yyyyMMdd-HHmmss" beside it - the SAME
       convention Install/Uninstall use for settings.json, so Get-SideCrabBackupPattern finds it
       and Restore-SideCrab.ps1 restores it. Returns the backup path, or $null when there is
       nothing to copy (a first-ever write).

       WHY IN COMMON (SET-a2, 2026-08-28): settings.json got a pre-write backup and config.json
       did not, so an interrupted or clobbering write of the operator's config (quiet hours,
       recap repos, toast threshold) had no way back. One copy of the rule, so the two files
       cannot drift apart again. #>
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Path.sidecrab-bak-$stamp"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    $backup
}

function Write-SideCrabFileAtomic {
    <# Write $Content to $Path so a crash mid-write can NEVER leave a truncated file: the bytes
       land in a temp file beside the target and are then renamed over it in one step.

       WHY (SET-a2, 2026-08-28): config.json was a plain truncate+write (Set-Content), so a crash
       between the truncate and the last byte lost the operator's config outright. A temp+rename
       makes the live file either the old bytes or the new bytes, never a half-written mix.

       The temp sits BESIDE the target, not in %TEMP%, so the rename is a same-volume move
       (atomic); a cross-volume move would silently fall back to copy+delete and lose the
       guarantee. [System.IO.File]::Move(overwrite) is used rather than Move-Item -Force because
       the .NET call documents the atomic replace; Move-Item's atomicity is not contractual. The
       temp name is unique so two writers never collide, and it is cleaned up on either path. #>
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Content
    )
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $tmp = "$Path.sidecrab-tmp-$([guid]::NewGuid().ToString('N'))"
    try {
        Set-Content -LiteralPath $tmp -Value $Content -Encoding utf8NoBOM
        # The ONLY step that touches the live file, and it is a single rename on one volume.
        [System.IO.File]::Move($tmp, $Path, $true)
    }
    finally {
        # A throw before the Move leaves a partial temp; a successful Move leaves none. Either way
        # nothing stray survives - and the '-tmp-' infix is not the '-bak-' one, so a leftover
        # would never be mistaken for a restorable backup.
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

function Get-SideCrabPanelApprovalsState {
    <# Read-only probe of config.json's panelApprovals.enabled. Never throws: an absent file
       or key is a state (OFF/default), not an error. Enabled is $true/$false, or $null when
       the key is absent (which the consumers treat as OFF). #>
    param([Parameter(Mandatory)][string] $ConfigPath)

    $out = [pscustomobject]@{ ConfigPath = $ConfigPath; Present = $false; Enabled = $null }
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return $out }
    try {
        $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8
        if (-not $raw.Trim()) { return $out }
        $cfg = $raw | ConvertFrom-Json -AsHashtable -Depth 40
    } catch { return $out }
    $out.Present = $true
    if ($cfg -is [System.Collections.IDictionary] -and $cfg.Contains('panelApprovals')) {
        $pa = $cfg['panelApprovals']
        if ($pa -is [System.Collections.IDictionary] -and $pa.Contains('enabled')) {
            $out.Enabled = [bool] $pa['enabled']
        }
    }
    $out
}

function Get-SideCrabPanelToken {
    <# Read-only probe of the approval PAIRING CODE crabd mints into ~/.sidecrab/panel-token
       (crabd 0.29.0, SEC-a). Never throws: an absent file means crabd 0.29.0 has not started
       yet (or is older), which is a state the callers report, not an error. The code is
       returned normalised (upper-case, hyphen shown) so it can be printed for the operator
       to type into iCUE's widget settings. Present=$false when the file is missing or does
       not hold a usable code. #>
    param([Parameter(Mandatory)][string] $TokenPath)

    $out = [pscustomobject]@{ TokenPath = $TokenPath; Present = $false; Code = $null }
    if (-not (Test-Path -LiteralPath $TokenPath)) { return $out }
    try { $raw = Get-Content -LiteralPath $TokenPath -Raw -Encoding utf8 } catch { return $out }
    $code = (("$raw").ToUpperInvariant() -replace '[^0-9A-Z]', '')
    if ($code -notmatch '^[0-9A-HJ-NP-TV-Z]{10}$') { return $out }
    $out.Present = $true
    $out.Code    = $code.Substring(0, 5) + '-' + $code.Substring(5)
    $out
}

function Set-SideCrabPanelApprovals {
    <# Writes config.json's panelApprovals.enabled, PRESERVING every other key - the same
       whole-file-rewrite contract POST /v1/config honours. Creates the file/dir if absent. #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string] $ConfigPath,
        [Parameter(Mandatory)][bool]   $Enabled
    )
    $before = Get-SideCrabPanelApprovalsState -ConfigPath $ConfigPath
    $cfg = @{}
    if (Test-Path -LiteralPath $ConfigPath) {
        $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8
        if ($raw.Trim()) {
            $cfg = $raw | ConvertFrom-Json -AsHashtable -Depth 40
            if ($cfg -isnot [hashtable]) { throw "$ConfigPath is not a JSON object" }
        }
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigPath) | Out-Null
    }
    $cfg['panelApprovals'] = @{ enabled = $Enabled }
    $backup = $null
    if ($PSCmdlet.ShouldProcess($ConfigPath, "Set panelApprovals.enabled=$Enabled")) {
        # Back up first (SET-a2), then write through the temp+rename path so a crash mid-write
        # cannot truncate the operator's config. Backup is $null on a first-ever write - nothing
        # to lose yet.
        $backup = Backup-SideCrabFile -Path $ConfigPath
        $json = $cfg | ConvertTo-Json -Depth 40
        Write-SideCrabFileAtomic -Path $ConfigPath -Content $json
    }
    [pscustomobject]@{
        ConfigPath = $ConfigPath
        Enabled    = $Enabled
        Previous   = $before.Enabled
        Changed    = ($before.Enabled -ne $Enabled)
        Backup     = $backup
    }
}

function Clear-SideCrabPanelApprovals {
    <# Removes the panelApprovals key from config.json, PRESERVING every other key. An absent
       file or key is a no-op reported honestly, not an error. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string] $ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        return [pscustomobject]@{ ConfigPath = $ConfigPath; Action = 'absent' }
    }
    $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8
    if (-not $raw.Trim()) { return [pscustomobject]@{ ConfigPath = $ConfigPath; Action = 'absent' } }
    $cfg = $raw | ConvertFrom-Json -AsHashtable -Depth 40
    if ($cfg -isnot [hashtable] -or -not $cfg.Contains('panelApprovals')) {
        return [pscustomobject]@{ ConfigPath = $ConfigPath; Action = 'not-present' }
    }
    if ($PSCmdlet.ShouldProcess($ConfigPath, 'Remove panelApprovals key')) {
        # Same as Set: back up first (SET-a2), then write atomically. This path rewrites the whole
        # config to drop one key, so an interrupted write would strand the rest of it.
        $backup = Backup-SideCrabFile -Path $ConfigPath
        $cfg.Remove('panelApprovals')
        $json = $cfg | ConvertTo-Json -Depth 40
        Write-SideCrabFileAtomic -Path $ConfigPath -Content $json
        return [pscustomobject]@{ ConfigPath = $ConfigPath; Action = 'removed'; Backup = $backup }
    }
    [pscustomobject]@{ ConfigPath = $ConfigPath; Action = 'skipped' }
}

# ------------------------------------------------------------------- environment

function Resolve-SideCrabPython {
    <# pythonw.exe runs a daemon with no console window; python.exe is the fallback
       and relies on the task's Hidden setting alone. #>
    $candidates = @()
    foreach ($name in 'python.exe', 'python3.exe') {
        $cmd = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($cmd) { $candidates += $cmd.Source }
    }
    foreach ($exe in $candidates) {
        # A WindowsApps python.exe is the Store alias stub - it cannot host a service.
        if ($exe -like '*\WindowsApps\*') { continue }
        $windowless = Join-Path (Split-Path -Parent $exe) 'pythonw.exe'
        if (Test-Path -LiteralPath $windowless) { return $windowless }
        return $exe
    }
    throw 'No usable python.exe found on PATH (the WindowsApps alias stub does not count). Install Python 3.13.'
}

function Register-SideCrabTask {
    <# One task shape for every SideCrab component: at logon, hidden, restart x3,
       no execution time limit, Limited run level.

       Returns the enable decision (see Get-SideCrabTaskEnableDecision) so the caller knows
       whether to start the task. A task the operator had DISABLED is re-registered - its
       action and paths stay current - and then put straight back into Disabled, because
       Register-ScheduledTask -Force always writes an enabled task and would otherwise
       resurrect it silently. #>
    param(
        [Parameter(Mandatory)][string] $TaskName,
        [Parameter(Mandatory)][string] $PythonExe,
        [Parameter(Mandatory)][string] $ScriptPath,
        [string] $Description = 'SideCrab component.',
        [switch] $ForceEnable
    )

    # Read the prior state BEFORE the -Force write - after it, the disabled flag is gone.
    $prior    = Get-SideCrabTaskState -TaskName $TaskName
    $decision = Get-SideCrabTaskEnableDecision -Registered $prior.Registered `
                                               -PriorState "$($prior.State)" `
                                               -ForceEnable $ForceEnable.IsPresent

    $action = New-ScheduledTaskAction -Execute $PythonExe `
                                      -Argument ('"{0}"' -f $ScriptPath) `
                                      -WorkingDirectory (Split-Path -Parent $ScriptPath)
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # ExecutionTimeLimit 0 = never kill it; these are daemons, not jobs.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd `
        -Hidden -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                            -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                           -Settings $settings -Principal $principal `
                           -Description $Description -Force | Out-Null

    if ($decision.LeaveDisabled) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }

    [pscustomobject]@{
        TaskName      = $TaskName
        PriorState    = if ($prior.Registered) { $prior.State } else { $null }
        WasDisabled   = $decision.WasDisabled
        LeaveDisabled = $decision.LeaveDisabled
        Start         = $decision.Start
        Reason        = $decision.Reason
    }
}

function Get-SideCrabTaskState {
    <# Read-only task probe. Never throws: an absent task is a state, not an error. #>
    param([Parameter(Mandatory)][string] $TaskName)

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        return [pscustomobject]@{
            TaskName = $TaskName; Registered = $false; State = $null
            LastRunTime = $null; LastTaskResult = $null
        }
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    [pscustomobject]@{
        TaskName       = $TaskName
        Registered     = $true
        State          = [string] $task.State
        LastRunTime    = if ($info) { $info.LastRunTime }    else { $null }
        LastTaskResult = if ($info) { $info.LastTaskResult } else { $null }
    }
}

# ------------------------------------------------------- the port, and the restart race

function Get-SideCrabPortHolder {
    <# WHO is listening on a TCP port right now - one row per listener, none when the port is
       free. Read-only and total: an absent listener is a state, not an error, and a probe that
       throws (no NetTCPIP module, a locked-down host) reports as "no rows" rather than crashing
       a restart.

       WHY THIS EXISTS (measured 2026-08-27): health-by-HTTP cannot tell WHO answered. After a
       failed restart a stray non-task process held 2722 and answered /v1/health convincingly
       while SideCrab-crabd itself was dead. The PID is the only thing that separates "our
       daemon" from "something else wearing its clothes".

       -Probe and -ProcessLookup are injected by the suite, so a test never opens a socket and
       the operator's live crabd on 2722 is never contacted. #>
    param(
        [Parameter(Mandatory)][int] $Port,
        [scriptblock] $Probe,
        [scriptblock] $ProcessLookup
    )

    if (-not $Probe) {
        $Probe = { param($p) Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue }
    }
    if (-not $ProcessLookup) {
        $ProcessLookup = { param($id) Get-Process -Id $id -ErrorAction SilentlyContinue }
    }

    $conns = @()
    try { $conns = @(& $Probe $Port) } catch { $conns = @() }

    foreach ($c in $conns) {
        if ($null -eq $c) { continue }
        $owner = if (@($c.PSObject.Properties.Name) -contains 'OwningProcess') { $c.OwningProcess } else { $null }
        $name  = $null
        $path  = $null
        if ($null -ne $owner) {
            $proc = $null
            try { $proc = & $ProcessLookup ([int] $owner) } catch { $proc = $null }
            if ($proc) {
                $name = [string] $proc.ProcessName
                # .Path is unreadable for a process owned by another user - null, not a throw.
                try { $path = [string] $proc.Path } catch { $path = $null }
            }
        }
        [pscustomobject]@{ Port = $Port; ProcessId = $owner; ProcessName = $name; Path = $path }
    }
    # Emitted unrolled - see Get-SideCrabTaskName.
}

function Format-SideCrabPortHolder {
    <# Holder rows as one line an operator can act on - the PID first, because that is what
       Stop-Process takes. Pure. #>
    param([AllowEmptyCollection()][object[]] $Holder = @(), [int] $Port = 0)

    $rows = @(@($Holder) | Where-Object { $_ })
    if ($rows.Count -eq 0) { return "no listener found on port $Port" }
    ($rows | ForEach-Object {
        $name = if ($_.ProcessName) { $_.ProcessName } else { 'unknown process' }
        $path = if ($_.Path) { " $($_.Path)" } else { '' }
        "PID $($_.ProcessId) ($name$path)"
    }) -join ', '
}

function Wait-SideCrabPortRelease {
    <# Poll until NOTHING is listening on $Port, or the budget runs out.

       THE INCIDENT (measured 2026-08-27): Update-SideCrab.ps1 -SkipPull stopped SideCrab-crabd
       and started it again immediately. The old process had left Running but had not yet closed
       its listening socket; crabd sets allow_reuse_address = False deliberately (companion\
       crabd.py - two instances answering half the requests each is worse than a loud refusal),
       so the NEW instance lost the bind race, printed "port 2722 is already in use" and exited
       1. The task parked in Ready with LastTaskResult=1 and the operator's panel was dark for
       ~6 minutes with nothing saying why.

       The budget is counted in POLLS, not off the wall clock: the sleep is injectable, and a
       clock-based deadline under a neutered sleep either spins forever or gives up after one
       attempt. Probe latency is therefore not charged against the budget - Attempts is the
       number to read, and WaitedSec is the sleeping part alone. #>
    param(
        [Parameter(Mandatory)][int] $Port,
        [int]    $TimeoutSec      = 10,
        [double] $PollIntervalSec = 0.3,
        [scriptblock] $HolderProbe,
        [scriptblock] $Wait
    )

    if (-not $HolderProbe)         { $HolderProbe = { param($p) Get-SideCrabPortHolder -Port $p } }
    if ($PollIntervalSec -le 0)    { $PollIntervalSec = 0.3 }

    $budget   = [int] [math]::Max(1, [math]::Ceiling($TimeoutSec / $PollIntervalSec))
    $attempts = 0
    $waited   = 0.0
    $holder   = @()

    while ($true) {
        $attempts++
        $holder = @(& $HolderProbe $Port)
        if ($holder.Count -eq 0) {
            return [pscustomobject]@{
                Port = $Port; Released = $true; Attempts = $attempts
                WaitedSec = [math]::Round($waited, 2); TimeoutSec = $TimeoutSec; Holder = @()
                Reason = if ($attempts -eq 1) { "port $Port was already free" }
                         else { "port $Port freed after ~$([math]::Round($waited, 2))s" }
            }
        }
        if ($attempts -ge $budget) { break }
        if ($Wait) { & $Wait $PollIntervalSec } else { Start-Sleep -Seconds $PollIntervalSec }
        $waited += $PollIntervalSec
    }

    [pscustomobject]@{
        Port = $Port; Released = $false; Attempts = $attempts
        WaitedSec = [math]::Round($waited, 2); TimeoutSec = $TimeoutSec; Holder = $holder
        Reason = ("port $Port still held after $attempts probe(s) over ~$([math]::Round($waited, 2))s by " +
                  (Format-SideCrabPortHolder -Holder $holder -Port $Port))
    }
}

function Restart-SideCrabTask {
    <# Stop a SideCrab task, wait for it to actually be GONE, then start it again. The ONE
       restart path: Update-SideCrab.ps1 and Repair-SideCrab.ps1 both call this rather than
       carrying a copy each, which is how one of them kept the race after the other was fixed.

       Two waits, and the second is the one the incident was about:
         1. the task leaving Running - starting a task that is still stopping is silently
            ignored by the scheduler;
         2. the PORT being released, for a component that owns one. A task leaves Running while
            the process's listening socket is still open, and the new instance then cannot bind
            (see Wait-SideCrabPortRelease).

       ON TIMEOUT IT DOES NOT START. Starting blind is what produced the dark panel: the restart
       "succeeded", the new process exited 1, and the only trace was LastTaskResult=1 on a task
       reading Ready. This throws instead, naming the PID that still holds the port - a foreign
       process there is a different problem from a slow shutdown, and the PID is the only thing
       that tells them apart.

       Every -*Task / -*Probe / -Wait scriptblock is injected by the suite, so the whole restart
       runs with no scheduler and no socket. #>
    param(
        [Parameter(Mandatory)][string] $TaskName,
        # 0 = this component owns no port. Which one does is a fact of the catalogue
        # (Get-SideCrabComponentSpec), never a guess made here.
        [int]    $Port            = 0,
        [int]    $StopWaitSec     = 10,
        [int]    $PortWaitSec     = 10,
        [double] $PollIntervalSec = 0.3,
        [scriptblock] $StopTask,
        [scriptblock] $StartTask,
        [scriptblock] $StateProbe,
        [scriptblock] $HolderProbe,
        [scriptblock] $Wait
    )

    if (-not $StopTask)   { $StopTask   = { param($n) Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue | Out-Null } }
    if (-not $StartTask)  { $StartTask  = { param($n) Start-ScheduledTask -TaskName $n } }
    if (-not $StateProbe) { $StateProbe = { param($n) "$((Get-SideCrabTaskState -TaskName $n).State)" } }
    if ($PollIntervalSec -le 0) { $PollIntervalSec = 0.3 }

    & $StopTask $TaskName

    $budget      = [int] [math]::Max(1, [math]::Ceiling($StopWaitSec / $PollIntervalSec))
    $leftRunning = $false
    for ($i = 1; $i -le $budget; $i++) {
        if ("$(& $StateProbe $TaskName)" -ne 'Running') { $leftRunning = $true; break }
        if ($Wait) { & $Wait $PollIntervalSec } else { Start-Sleep -Seconds $PollIntervalSec }
    }

    $release = $null
    if ($Port -gt 0) {
        $release = Wait-SideCrabPortRelease -Port $Port -TimeoutSec $PortWaitSec `
                                            -PollIntervalSec $PollIntervalSec `
                                            -HolderProbe $HolderProbe -Wait $Wait
        if (-not $release.Released) {
            throw ("$TaskName was NOT restarted: $($release.Reason). Starting now would lose the bind race - " +
                   'the new process exits 1 and the task parks in Ready with LastTaskResult=1, serving nothing. ' +
                   "If that PID is not this task's own daemon it is a foreign process or an orphan from a failed " +
                   'restart: stop it (Stop-Process -Id <pid>) and re-run.')
        }
    }

    & $StartTask $TaskName

    [pscustomobject]@{
        TaskName     = $TaskName
        Port         = $Port
        Started      = $true
        LeftRunning  = $leftRunning
        PortReleased = if ($release) { $release.Released }  else { $null }
        PortWaitSec  = if ($release) { $release.WaitedSec } else { $null }
        Holder       = if ($release) { $release.Holder }    else { @() }
    }
}

function Get-SideCrabAumidState {
    <# Read-only probe of the toast app identity. Never throws: an unregistered AUMID is
       a state, not an error - the notifier falls back to the borrowed one and still works. #>
    param([Parameter(Mandatory)][string] $RepoRoot)

    $spec = Get-SideCrabAumidSpec -RepoRoot $RepoRoot
    $key  = Get-Item -LiteralPath $spec.RegistryPath -ErrorAction SilentlyContinue
    $displayName = $null
    $iconUri     = $null
    if ($key) {
        # GetValue returns $null for an absent value rather than throwing, which is
        # exactly the "half-written key" case this has to report honestly.
        $displayName = $key.GetValue('DisplayName')
        $iconUri     = $key.GetValue('IconUri')
    }

    $iconPresent = [bool] (Test-Path -LiteralPath $spec.IconUri)
    # Current used to compare IconUri against the spec path alone, so a key pointing at an icon
    # that had been deleted read as "registered and current" - green, and no icon on any toast.
    $icon = Get-SideCrabAumidIconDecision -IconPresent $iconPresent -SpecIconUri $spec.IconUri `
                                          -RegisteredIconUri $iconUri

    [pscustomobject]@{
        Aumid        = $spec.Aumid
        RegistryPath = $spec.RegistryPath
        Registered   = [bool] $key
        DisplayName  = $displayName
        IconUri      = $iconUri
        IconPath     = $spec.IconUri
        IconPresent  = $iconPresent
        ExpectedIcon = $icon.Expected
        IconStale    = [bool] ($key -and -not $icon.Matches)
        # Registered AND carrying the values we would write - the test the install reports on.
        Current      = [bool] ($key -and $displayName -eq $spec.DisplayName -and $icon.Matches)
    }
}

function Set-SideCrabAumid {
    <# Registers the toast app identity under HKCU. Idempotent: values are compared before
       they are written, so a re-run reports 'unchanged' and touches nothing.

       HKCU ONLY by design - HKLM would need elevation and this is a per-user notifier.
       No Start-menu shortcut is created: a DisplayName/IconUri key is all the toast API
       needs to accept the AUMID, and a shortcut would be an uninstall liability. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string] $RepoRoot)

    $spec   = Get-SideCrabAumidSpec -RepoRoot $RepoRoot
    $before = Get-SideCrabAumidState -RepoRoot $RepoRoot

    $icon = Get-SideCrabAumidIconDecision -IconPresent $before.IconPresent -SpecIconUri $spec.IconUri `
                                          -RegisteredIconUri $before.IconUri

    $values = [ordered]@{ DisplayName = $spec.DisplayName }
    if ($null -ne $icon.Expected) {
        $values['IconUri'] = $icon.Expected
    } else {
        # An IconUri pointing at nothing renders as no icon with no error - strictly worse
        # than leaving the value off, which at least reads as "not set" on inspection.
        Write-Warning "icon not found at $($spec.IconUri) - registering DisplayName only (run: python notifier\make_icon.py)"
    }

    $action = if (-not $before.Registered) { 'created' }
              elseif ($before.Current)     { 'unchanged' }
              else                         { 'updated' }

    if ($action -ne 'unchanged') {
        if ($PSCmdlet.ShouldProcess($spec.RegistryPath, 'Register SideCrab AppUserModelID')) {
            # -Force creates the intermediate AppUserModelId key too, and is a no-op on an
            # existing key (it does NOT clear values).
            New-Item -Path $spec.RegistryPath -Force | Out-Null
            foreach ($name in $values.Keys) {
                New-ItemProperty -LiteralPath $spec.RegistryPath -Name $name `
                                 -Value $values[$name] -PropertyType String -Force | Out-Null
            }
            # THE HALF THAT WAS MISSING: writing DisplayName only leaves a stale IconUri exactly
            # where it was, because -Force overwrites the values it is given and touches no
            # others. Without this delete, a repo move made the icon unrecoverable by re-running
            # the installer - it reported 'unchanged'/'updated' and the dead pointer survived.
            if ($icon.Remove) {
                Remove-ItemProperty -LiteralPath $spec.RegistryPath -Name 'IconUri' -Force -ErrorAction SilentlyContinue
                Write-Warning "removed the registered IconUri - it pointed at $($before.IconUri), which is not there"
            }
        }
    }

    [pscustomobject]@{
        Aumid        = $spec.Aumid
        RegistryPath = $spec.RegistryPath
        Action       = $action
        DisplayName  = $spec.DisplayName
        IconUri      = $icon.Expected
        IconPresent  = $before.IconPresent
        IconRemoved  = $icon.Remove
    }
}

function Remove-SideCrabAumid {
    <# Removes the toast app identity. Only OUR key: the parent AppUserModelId key is shared
       with every other app on the machine and is never touched. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string] $RepoRoot)

    $spec = Get-SideCrabAumidSpec -RepoRoot $RepoRoot
    if (-not (Test-Path -LiteralPath $spec.RegistryPath)) {
        return [pscustomobject]@{ Aumid = $spec.Aumid; RegistryPath = $spec.RegistryPath; Action = 'absent' }
    }
    if ($PSCmdlet.ShouldProcess($spec.RegistryPath, 'Remove SideCrab AppUserModelID')) {
        Remove-Item -LiteralPath $spec.RegistryPath -Recurse -Force
        return [pscustomobject]@{ Aumid = $spec.Aumid; RegistryPath = $spec.RegistryPath; Action = 'removed' }
    }
    [pscustomobject]@{ Aumid = $spec.Aumid; RegistryPath = $spec.RegistryPath; Action = 'skipped' }
}

function Get-SideCrabProtocolState {
    <# Read-only probe of the toast handler registrations - ONE ROW PER SCHEME, in spec
       order. Never throws: unregistered is a state, not an error; the toast simply renders
       with a button the shell no-ops.

       Callers wrap in @() and loop. A caller that reads .Scheme off the return value gets
       every scheme's, which is the bug this shape is meant to make obvious rather than
       silently reporting on the first one.

       -PythonExe is optional so this stays callable from a -Status path on a box where no
       usable interpreter is on PATH. Without one the EXPECTED command is unknowable, and
       Current is reported $false rather than guessed. #>
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [string] $PythonExe
    )

    if (-not $PythonExe) {
        try { $PythonExe = Resolve-SideCrabPython } catch { $PythonExe = $null }
    }

    foreach ($spec in @(Get-SideCrabProtocolSpec -RepoRoot $RepoRoot)) {
        $expected = if ($PythonExe) {
            Get-SideCrabProtocolCommand -PythonExe $PythonExe -HandlerPath $spec.Handler
        } else { $null }

        $root    = Get-Item -LiteralPath $spec.RegistryPath -ErrorAction SilentlyContinue
        $cmdKey  = Get-Item -LiteralPath $spec.CommandPath  -ErrorAction SilentlyContinue
        # GetValue('') is the default value; GetValueNames() is the only way to tell an empty
        # 'URL Protocol' (which is what Windows requires) from an absent one.
        $command = if ($cmdKey) { [string] $cmdKey.GetValue('') } else { $null }
        $hasFlag = [bool] ($root -and (@($root.GetValueNames()) -contains 'URL Protocol'))

        [pscustomobject]@{
            Key             = $spec.Key
            Scheme          = $spec.Scheme
            Button          = $spec.Button
            RegistryPath    = $spec.RegistryPath
            CommandPath     = $spec.CommandPath
            # Windows only treats a class key as a launchable scheme when URL Protocol is
            # present; a key with a command and no flag is registered-looking and inert.
            Registered      = [bool] ($hasFlag -and $command)
            UrlProtocolFlag = $hasFlag
            Command         = $command
            Expected        = $expected
            HandlerPath     = $spec.Handler
            HandlerPresent  = [bool] (Test-Path -LiteralPath $spec.Handler)
            # A command that lost its "%1" launches the handler with no URI at all: it logs a
            # refusal and exits, so the button looks wired and does nothing.
            CarriesArgument = [bool] ($command -and $command -like '*%1*')
            Current         = [bool] ($expected -and $command -eq $expected)
        }
    }
    # Emitted unrolled - see Get-SideCrabTaskName.
}

function Set-SideCrabProtocol {
    <# Registers EVERY scheme in the spec under HKCU, one row returned per scheme.
       Idempotent: each command string is compared before it is written, so a re-run reports
       'unchanged'.

       HKCU ONLY by design - HKLM would need elevation, and a per-user notifier's buttons
       belong to the user who gets the toasts.

       A scheme whose handler file is missing is SKIPPED, loudly, not registered: registering
       a scheme at a handler that is not there produces a shell error dialog on every press -
       louder AND less useful than the no-op an unregistered scheme gives. Only when NOT ONE
       handler is present does this throw, because then the notifier component itself is not
       really there and a silent success would be the lie. #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [string] $PythonExe,
        # Narrow to one scheme (the doctor fixes the row it diagnosed). Unset = all of them.
        [string[]] $Scheme
    )

    $specs = @(Get-SideCrabProtocolSpec -RepoRoot $RepoRoot)
    if ($Scheme) {
        $specs = @($specs | Where-Object { $Scheme -contains $_.Scheme -or $Scheme -contains $_.Key })
        if ($specs.Count -eq 0) { throw "no SideCrab protocol scheme matches: $($Scheme -join ', ')" }
    }
    if (-not $PythonExe) { $PythonExe = Resolve-SideCrabPython }

    $states  = @(Get-SideCrabProtocolState -RepoRoot $RepoRoot -PythonExe $PythonExe)
    $keys    = @($specs | ForEach-Object { $_.Key })
    $states  = @($states | Where-Object { $keys -contains $_.Key })
    $present = @($states | Where-Object { $_.HandlerPresent })
    if ($present.Count -eq 0) {
        throw ("no toast handler found under $RepoRoot\notifier - cannot register " +
               (($specs | ForEach-Object { "$($_.Scheme):" }) -join ', '))
    }

    foreach ($spec in $specs) {
        $before = @($states | Where-Object { $_.Key -eq $spec.Key })[0]
        if (-not $before.HandlerPresent) {
            Write-Warning "handler not found at $($spec.Handler) - $($spec.Scheme): NOT registered (the $($spec.Button) button stays inert)"
            [pscustomobject]@{
                Key = $spec.Key; Scheme = $spec.Scheme; RegistryPath = $spec.RegistryPath
                CommandPath = $spec.CommandPath; Action = 'handler-missing'
                Command = $null; HandlerPath = $spec.Handler
            }
            continue
        }

        $expected = Get-SideCrabProtocolCommand -PythonExe $PythonExe -HandlerPath $spec.Handler
        $action = if (-not $before.Registered) { 'created' }
                  elseif ($before.Current)     { 'unchanged' }
                  else                         { 'updated' }

        if ($action -ne 'unchanged') {
            if ($PSCmdlet.ShouldProcess($spec.RegistryPath, "Register $($spec.Scheme) protocol handler")) {
                # -Force on the deepest path creates the whole key chain, and is a no-op on an
                # existing key (it does NOT clear values).
                New-Item -Path $spec.CommandPath -Force | Out-Null
                Set-ItemProperty -LiteralPath $spec.RegistryPath -Name '(Default)' -Value $spec.Description -Type String
                Set-ItemProperty -LiteralPath $spec.RegistryPath -Name 'URL Protocol' -Value '' -Type String
                Set-ItemProperty -LiteralPath $spec.CommandPath  -Name '(Default)' -Value $expected -Type String
            }
        }

        [pscustomobject]@{
            Key          = $spec.Key
            Scheme       = $spec.Scheme
            RegistryPath = $spec.RegistryPath
            CommandPath  = $spec.CommandPath
            Action       = $action
            Command      = $expected
            HandlerPath  = $spec.Handler
        }
    }
}

function Remove-SideCrabProtocol {
    <# Removes EVERY scheme in the spec, one row returned per scheme. The whole scheme key
       goes, subkeys included: unlike AppUserModelId, these keys ARE ours - nothing else on
       the machine can own a scheme named sidecrab-ack or sidecrab-snooze. #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string] $RepoRoot)

    foreach ($spec in @(Get-SideCrabProtocolSpec -RepoRoot $RepoRoot)) {
        $row = [pscustomobject]@{
            Key = $spec.Key; Scheme = $spec.Scheme; RegistryPath = $spec.RegistryPath; Action = 'absent'
        }
        if (-not (Test-Path -LiteralPath $spec.RegistryPath)) { $row; continue }
        if ($PSCmdlet.ShouldProcess($spec.RegistryPath, "Remove $($spec.Scheme) protocol handler")) {
            Remove-Item -LiteralPath $spec.RegistryPath -Recurse -Force
            $row.Action = 'removed'
        } else {
            $row.Action = 'skipped'
        }
        $row
    }
}

function Get-SideCrabHealth {
    <# GET /v1/health. Returns a verdict object rather than throwing - a stopped
       crabd is the normal case this is asked about. #>
    param(
        [string] $Uri = 'http://127.0.0.1:2722/v1/health',
        [int]    $TimeoutSec = 3
    )

    try {
        $r = Invoke-RestMethod -Uri $Uri -TimeoutSec $TimeoutSec -ErrorAction Stop
        $ok = $false
        if ($r -and $r.PSObject.Properties.Name -contains 'ok') { $ok = [bool] $r.ok }
        $version = $null
        if ($r -and $r.PSObject.Properties.Name -contains 'version') { $version = [string] $r.version }
        [pscustomobject]@{ Reachable = $true; Ok = $ok; Version = $version; Error = $null; Uri = $Uri }
    } catch {
        [pscustomobject]@{ Reachable = $false; Ok = $false; Version = $null; Error = $_.Exception.Message; Uri = $Uri }
    }
}

function Test-SideCrabHealthOk {
    <# Does a /v1/health document say ok? Pure, and total: $null, a document with no 'ok', and
       ok:false are all "no". Takes either an Invoke-RestMethod object or a hashtable, so a
       test can hand it a literal. #>
    param($Document)

    if ($null -eq $Document) { return $false }
    if ($Document -is [System.Collections.IDictionary]) { return [bool] $Document['ok'] }
    [bool] ((@($Document.PSObject.Properties.Name) -contains 'ok') -and $Document.ok)
}

function Get-SideCrabHealthProbe {
    <# One health reading, with ONE retry after a short backoff, and an honest account of
       which attempt answered.

       WHY (measured 2026-08-27): a single GET can read a healthy crabd as dead - right after a
       task restart, and on this workstation whenever a loopback SYN-ACK is dropped (the burst
       described in docs/BACKLOG.md, "Host / environment"). Seen live: FAIL, then OK three
       seconds later, with the PID listening throughout. A doctor that cries wolf gets ignored.

       RecoveredOnRetry is the other half of the fix: swallowing the first failure silently
       would hide a real symptom - a crabd that needs two attempts is not the same as one that
       answers first time, and the operator is the one who gets to decide that matters.

       -Probe returns the health document or $null; -Wait is the sleep, injectable so a test
       runs at full speed. Never throws: the probe's own failure is the caller's to model. #>
    param(
        [Parameter(Mandatory)][scriptblock] $Probe,
        [int] $RetryDelaySec = 3,
        [scriptblock] $Wait
    )

    $first = & $Probe
    if (Test-SideCrabHealthOk -Document $first) {
        return [pscustomobject]@{
            Document = $first; Ok = $true; Attempts = 1; RecoveredOnRetry = $false; DelaySec = 0
        }
    }

    if ($Wait) { & $Wait $RetryDelaySec } else { Start-Sleep -Seconds $RetryDelaySec }

    $second = & $Probe
    $ok     = Test-SideCrabHealthOk -Document $second
    [pscustomobject]@{
        # The freshest document either attempt produced: downstream checks (version, statusline
        # age) read it, and the retry's answer is the current one whenever there is one.
        Document         = if ($null -ne $second) { $second } else { $first }
        Ok               = $ok
        Attempts         = 2
        RecoveredOnRetry = $ok
        DelaySec         = $RetryDelaySec
    }
}

function Get-SideCrabServiceVerdict {
    <# "Is crabd actually up?" - answered by the TWO readings that disagree in the case that
       matters, never by health alone. Pure.

       WHY BOTH (measured 2026-08-27): health-by-HTTP cannot tell WHO is answering. After a
       failed restart a stray non-task process held 2722 and answered /v1/health convincingly
       while SideCrab-crabd sat in Ready with LastTaskResult=1 - so the single check the updater
       made reported success over a task that was not running, and the panel stayed dark. An
       answer with no Running task is not "fine": it is the loudest row on the list, because it
       is also the thing that stops the real instance from ever binding.

       The four cases, and why each is different:
         ok                answering, and the task owns it.
         not-answering     task Running, nothing answered - starting up, or up and unbound.
                           This is the case the health retry exists for; not a foreign process.
         foreign-answerer  an answer with the task NOT Running. FAIL, and name the PID.
         down              neither. crabd is simply not running. #>
    param(
        [bool]   $HealthOk,
        [string] $TaskState,        # 'Running' / 'Ready' / 'Disabled'; '' when not registered
        $LastTaskResult,
        [AllowEmptyCollection()][object[]] $Holder = @(),
        [int] $Port = 0
    )

    $running = ($TaskState -eq 'Running')
    $where   = if ($TaskState) { "task is $TaskState" } else { 'task is not registered' }
    $last    = if ($null -ne $LastTaskResult) {
                   ', last result 0x{0:X8}' -f ([int64] $LastTaskResult -band 0xFFFFFFFFL)
               } else { '' }

    if ($HealthOk -and $running) {
        return [pscustomobject]@{
            Verdict = 'ok'; Ok = $true
            Reason  = "crabd answered and the task is Running - the task owns port $Port"
        }
    }
    if ($HealthOk) {
        return [pscustomobject]@{
            Verdict = 'foreign-answerer'; Ok = $false
            Reason  = ("something answered on port $Port but the $where$last - a health answer is NOT proof " +
                       "the task is up. Port $Port is held by $(Format-SideCrabPortHolder -Holder $Holder -Port $Port): " +
                       'a foreign process, or an orphan left by a failed restart - which is also what stops the ' +
                       'real instance binding.')
        }
    }
    if ($running) {
        return [pscustomobject]@{
            Verdict = 'not-answering'; Ok = $false
            Reason  = "the task is Running but nothing answered on port $Port - still starting, or up and unbound"
        }
    }
    [pscustomobject]@{
        Verdict = 'down'; Ok = $false
        Reason  = "nothing answered on port $Port and the $where$last - crabd is not running"
    }
}

function Get-SideCrabWidgetVersion {
    <# The widget ships through iCUE, not through this repo's install path - its
       manifest version is reported for comparison only. #>
    param([Parameter(Mandatory)][string] $RepoRoot)

    $manifest = Join-Path $RepoRoot 'widget\manifest.json'
    if (-not (Test-Path -LiteralPath $manifest)) { return $null }
    try {
        $json = Get-Content -LiteralPath $manifest -Raw -Encoding utf8 | ConvertFrom-Json -Depth 20
        if ($json.PSObject.Properties.Name -contains 'version') { return [string] $json.version }
        return $null
    } catch { return $null }
}

function Read-SideCrabSettings {
    <# settings.json as a dictionary, or $null when absent/empty. Read-only. #>
    param([Parameter(Mandatory)][string] $SettingsPath)

    if (-not (Test-Path -LiteralPath $SettingsPath)) { return $null }
    $raw = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8
    if (-not $raw.Trim()) { return $null }
    $parsed = $raw | ConvertFrom-Json -AsHashtable -Depth 40
    if ($parsed -isnot [System.Collections.IDictionary]) { throw "$SettingsPath is not a JSON object" }
    return $parsed
}

# ------------------------------------------------- backups, restore + residue (pure)

function Get-SideCrabBackupPattern {
    <# The wildcard that finds an installer backup beside a settings file. Pure.
       Both Install and Uninstall write "<path>.sidecrab-bak-yyyyMMdd-HHmmss"; this is the
       one place the shape is written down for the readers. #>
    param([Parameter(Mandatory)][string] $SettingsPath)
    '{0}.sidecrab-bak-*' -f (Split-Path -Leaf $SettingsPath)
}

function Read-SideCrabBackupStamp {
    <# The instant encoded in a backup's NAME, or $null when the name carries no well-formed
       stamp. Pure.

       Read the name, never the file's LastWriteTime: Copy-Item preserves the source's
       timestamp, so a backup taken today of a settings.json last edited in June has a June
       mtime. Sorting a backup pile by mtime therefore orders it by when the operator last
       edited settings.json - not by when the backups were taken. #>
    param([Parameter(Mandatory)][string] $Name)

    if ($Name -notmatch '\.sidecrab-bak-(\d{8})-(\d{6})$') { return $null }
    $text   = '{0}{1}' -f $Matches[1], $Matches[2]
    $parsed = [datetime]::MinValue
    $ok = [datetime]::TryParseExact($text, 'yyyyMMddHHmmss', [cultureinfo]::InvariantCulture,
                                    [System.Globalization.DateTimeStyles]::None, [ref] $parsed)
    # The stamp is written with Get-Date, so it is LOCAL time - compare it against local now.
    if ($ok) { return $parsed }
    $null
}

function Get-SideCrabBackupFile {
    <# Every SideCrab backup beside $TargetPath, newest first by the instant in its NAME, then any
       whose name carries no well-formed stamp (listed last so they are visible, not restorable).
       Each row is {Path, Name, Stamp, Bytes}. Read-only; an absent directory is @(), not a throw.

       TARGET-AGNOSTIC on purpose (SET-a2): the same convention names settings.json AND config.json
       backups, so Restore-SideCrab.ps1 finds either by pointing this at the right file - the leaf
       name in the pattern is all that differs. #>
    param([Parameter(Mandatory)][string] $TargetPath)

    $dir = Split-Path -Parent $TargetPath
    if (-not (Test-Path -LiteralPath $dir)) { return @() }
    $pattern = Get-SideCrabBackupPattern -SettingsPath $TargetPath

    $rows = foreach ($f in @(Get-ChildItem -LiteralPath $dir -Filter $pattern -File -ErrorAction SilentlyContinue)) {
        [pscustomobject]@{
            Path  = $f.FullName
            Name  = $f.Name
            Stamp = Read-SideCrabBackupStamp -Name $f.Name
            Bytes = $f.Length
        }
    }
    @(@($rows) | Where-Object { $_.Stamp } | Sort-Object -Property Stamp -Descending) +
    @(@($rows) | Where-Object { -not $_.Stamp })
}

function Get-SideCrabCanonicalValue {
    <# A JSON-able value with every dictionary key sorted, recursively. Pure.

       Arrays keep their order on purpose: the order of matchers inside a hooks event is
       meaningful, so two settings files that list the same hooks differently are DIFFERENT.
       Only key order is noise, and only key order is normalised away. #>
    param($Value, [int] $Depth = 40)

    if ($Depth -le 0)   { return "$Value" }
    if ($null -eq $Value) { return $null }
    if ($Value -is [string]) { return $Value }
    if ($Value -is [System.Collections.IDictionary]) {
        $out = [ordered]@{}
        foreach ($k in @($Value.Keys | Sort-Object { "$_" })) {
            $out["$k"] = Get-SideCrabCanonicalValue -Value $Value[$k] -Depth ($Depth - 1)
        }
        return $out
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        # Comma-wrapped: a bare @() return is unrolled by the pipeline and a one-element
        # array would come back to the caller as a scalar, comparing equal to the element.
        return , @(foreach ($item in $Value) { Get-SideCrabCanonicalValue -Value $item -Depth ($Depth - 1) })
    }
    $Value
}

function ConvertTo-SideCrabCanonicalJson {
    <# Key-sorted compact JSON: the comparison form for "did this change?". Pure. #>
    param($Value, [int] $Depth = 40)

    $norm = Get-SideCrabCanonicalValue -Value $Value -Depth $Depth
    if ($null -eq $norm) { return 'null' }
    ConvertTo-Json -InputObject $norm -Depth $Depth -Compress
}

function Test-SideCrabHookMatcherIsOurs {
    <# Does one settings.json hook matcher carry a SideCrab entry? Pure.
       A command hook carries the marker in 'command', an http hook (Stop, PermissionRequest)
       in 'url' - concatenating finds both, since a hook has only one of the two.
       KEPT AS PUBLIC API (2026-08-27): zero production callers since the v0.17.0 wave moved
       install/uninstall/restore/doctor to entry-level Split-SideCrabHookMatcher - matcher-level
       "is ours" over-claims on a shared matcher, so do NOT reintroduce it on an ownership
       decision path. Kept for scripts/tests that only need "does SideCrab touch this matcher". #>
    param($Matcher, [string] $Marker = '127.0.0.1:2722/v1/hook')

    if ($Matcher -isnot [System.Collections.IDictionary]) { return $false }
    if (-not $Matcher.Contains('hooks')) { return $false }
    foreach ($h in @($Matcher['hooks'])) {
        if ($h -is [System.Collections.IDictionary] -and "$($h['command'])$($h['url'])" -like "*$Marker*") {
            return $true
        }
    }
    $false
}

function Split-SideCrabHookMatcher {
    <# ONE hooks matcher, split into the SideCrab hook entries and the foreign ones. Pure.

       WHY ENTRY-LEVEL AND NOT MATCHER-LEVEL: the installer writes each SideCrab hook as its
       OWN matcher, so "this matcher is ours" and "this hook is ours" agree - right up until a
       human hand-merges a hook of their own INTO one of ours. Deciding at matcher level then
       deletes their hook on uninstall and hides it from the restore guard whose entire job is
       protecting that kind of edit (docs\findings\QA-Audit-2026-08-27.md, SETUP MED).

       Ours/Foreign are the matcher carrying only that half, or $null when the half is empty.
       The UNSHARED cases return the ORIGINAL object on the owning side - byte-identical, so a
       canonical-JSON comparison of an untouched matcher is unchanged by this split. Only a
       genuinely shared matcher is rebuilt, and then every other key it carries (the matcher
       pattern, and anything a future CLI adds) is copied onto both halves. #>
    param($Matcher, [string] $Marker = '127.0.0.1:2722/v1/hook')

    # Not a matcher we understand, or one with no hooks list: not ours, and passed through
    # untouched rather than dropped.
    if ($Matcher -isnot [System.Collections.IDictionary] -or -not $Matcher.Contains('hooks')) {
        return [pscustomobject]@{ Ours = $null; Foreign = $Matcher; OurCount = 0; ForeignCount = 1 }
    }

    $mine = @(); $theirs = @()
    foreach ($h in @($Matcher['hooks'])) {
        # A command hook carries the marker in 'command', an http hook in 'url' - concatenating
        # finds both, since a hook has only one of the two.
        if ($h -is [System.Collections.IDictionary] -and "$($h['command'])$($h['url'])" -like "*$Marker*") {
            $mine += , $h
        } else {
            $theirs += , $h
        }
    }

    # An empty matcher is nobody's: hand it back as foreign so it survives untouched.
    if ($mine.Count -eq 0) {
        return [pscustomobject]@{ Ours = $null; Foreign = $Matcher; OurCount = 0; ForeignCount = @($theirs).Count }
    }
    if ($theirs.Count -eq 0) {
        return [pscustomobject]@{ Ours = $Matcher; Foreign = $null; OurCount = $mine.Count; ForeignCount = 0 }
    }

    $rebuild = {
        param($entries)
        $copy = [ordered]@{}
        foreach ($k in @($Matcher.Keys)) {
            if ("$k" -eq 'hooks') { $copy['hooks'] = @($entries) } else { $copy["$k"] = $Matcher[$k] }
        }
        $copy
    }
    [pscustomobject]@{
        Ours         = (& $rebuild $mine)
        Foreign      = (& $rebuild $theirs)
        OurCount     = $mine.Count
        ForeignCount = $theirs.Count
    }
}

function Split-SideCrabSettings {
    <# Splits a settings.json document into the part SideCrab owns and the part it does not.
       Pure - the caller reads the file.

       OURS is exactly what an install writes: the hook matchers carrying the crabd marker,
       and statusLine when the command is ours. FOREIGN is everything else - every other
       top-level key, every hook matcher that is not ours, and a statusLine belonging to
       someone else.

       This split is what makes a restore safe to reason about: rolling settings.json back to
       a backup replaces the WHOLE file, so any foreign key that changed since the backup was
       taken is an operator edit the restore would silently discard. #>
    param($Settings, [string] $Marker = '127.0.0.1:2722/v1/hook')

    $ours    = [ordered]@{ hooks = [ordered]@{}; statusLine = $null }
    $foreign = [ordered]@{}
    if ($null -eq $Settings -or $Settings -isnot [System.Collections.IDictionary]) {
        return [pscustomobject]@{ Ours = $ours; Foreign = $foreign }
    }

    foreach ($key in @($Settings.Keys | Sort-Object { "$_" })) {
        $name = "$key"
        if ($name -eq 'hooks') {
            $hooks = $Settings[$key]
            if ($hooks -isnot [System.Collections.IDictionary]) { $foreign['hooks'] = $hooks; continue }
            $foreignHooks = [ordered]@{}
            foreach ($ev in @($hooks.Keys | Sort-Object { "$_" })) {
                $mine = @(); $theirs = @()
                foreach ($m in @($hooks[$ev])) {
                    # Entry-level, not matcher-level: a foreign hook hand-merged INTO one of
                    # our matchers must land in FOREIGN, or the guard below never sees the
                    # edit it exists to protect. See Split-SideCrabHookMatcher.
                    $part = Split-SideCrabHookMatcher -Matcher $m -Marker $Marker
                    if ($null -ne $part.Ours)    { $mine   += , $part.Ours }
                    if ($null -ne $part.Foreign) { $theirs += , $part.Foreign }
                }
                if ($mine.Count)   { $ours['hooks']["$ev"]  = $mine }
                if ($theirs.Count) { $foreignHooks["$ev"]   = $theirs }
            }
            if ($foreignHooks.Count) { $foreign['hooks'] = $foreignHooks }
            continue
        }
        if ($name -eq 'statusLine') {
            $sl  = $Settings[$key]
            $cmd = if ($sl -is [System.Collections.IDictionary]) { "$($sl['command'])" } else { '' }
            if (Test-SideCrabStatusLineIsOurs -Command $cmd) { $ours['statusLine'] = $sl }
            else                                             { $foreign['statusLine'] = $sl }
            continue
        }
        $foreign[$name] = $Settings[$key]
    }

    [pscustomobject]@{ Ours = $ours; Foreign = $foreign }
}

function Compare-SideCrabSettingsPair {
    <# What a restore of $Backup over $Current would change, split the way Split-SideCrabSettings
       splits it. Pure.

       ForeignDiff is the one that gates: those keys are the operator's, and a whole-file
       restore overwrites them. SideCrabDiff is informational - putting our own wiring back is
       the POINT of a restore, not a hazard. #>
    param($Backup, $Current, [string] $Marker = '127.0.0.1:2722/v1/hook')

    $b = Split-SideCrabSettings -Settings $Backup  -Marker $Marker
    $c = Split-SideCrabSettings -Settings $Current -Marker $Marker

    $foreignDiff = @()
    $keys = @(@($b.Foreign.Keys) + @($c.Foreign.Keys) | ForEach-Object { "$_" } | Sort-Object -Unique)
    foreach ($k in $keys) {
        $inB = $b.Foreign.Contains($k)
        $inC = $c.Foreign.Contains($k)
        $bv  = if ($inB) { ConvertTo-SideCrabCanonicalJson -Value $b.Foreign[$k] } else { $null }
        $cv  = if ($inC) { ConvertTo-SideCrabCanonicalJson -Value $c.Foreign[$k] } else { $null }
        if ($bv -eq $cv) { continue }
        $state = if (-not $inB)      { 'added since the backup - a restore DELETES it' }
                 elseif (-not $inC)  { 'removed since the backup - a restore BRINGS IT BACK' }
                 else                { 'edited since the backup - a restore REVERTS it' }
        $foreignDiff += [pscustomobject]@{ Key = $k; State = $state }
    }

    $ourDiff = @()
    $evKeys = @(@($b.Ours['hooks'].Keys) + @($c.Ours['hooks'].Keys) | ForEach-Object { "$_" } | Sort-Object -Unique)
    foreach ($ev in $evKeys) {
        $bn = if ($b.Ours['hooks'].Contains($ev)) { @($b.Ours['hooks'][$ev]).Count } else { 0 }
        $cn = if ($c.Ours['hooks'].Contains($ev)) { @($c.Ours['hooks'][$ev]).Count } else { 0 }
        if ($bn -ne $cn) { $ourDiff += [pscustomobject]@{ Key = "hooks/$ev"; State = "$cn now, $bn in the backup" } }
    }
    $bsl = if ($b.Ours['statusLine']) { 'SideCrab' } else { 'none' }
    $csl = if ($c.Ours['statusLine']) { 'SideCrab' } else { 'none' }
    if ($bsl -ne $csl) { $ourDiff += [pscustomobject]@{ Key = 'statusLine'; State = "$csl now, $bsl in the backup" } }

    [pscustomobject]@{
        ForeignChanged = ($foreignDiff.Count -gt 0)
        ForeignDiff    = $foreignDiff
        SideCrabDiff   = $ourDiff
        Identical      = (($foreignDiff.Count -eq 0) -and ($ourDiff.Count -eq 0))
    }
}

function Get-SideCrabPruneDecision {
    <# Which backups a -PruneOlderThan run deletes. Pure.

       The NEWEST backup is never pruned, whatever its age. A pile that is entirely older than
       the cutoff is the normal state of a stable install, and pruning it to zero removes the
       only way back from the install that is running right now. #>
    param(
        [AllowEmptyCollection()][object[]] $Backup,
        [Parameter(Mandatory)][int] $OlderThanDays,
        [datetime] $Now = (Get-Date)
    )

    $rows = @(@($Backup) | Where-Object { $_ } | Sort-Object -Property Stamp -Descending)
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $age = [math]::Round(($Now - [datetime] $rows[$i].Stamp).TotalDays, 2)
        if ($i -eq 0) {
            [pscustomobject]@{ Path = $rows[$i].Path; Stamp = $rows[$i].Stamp; AgeDays = $age
                               Delete = $false; Reason = 'newest backup - never pruned' }
            continue
        }
        $old = $age -gt $OlderThanDays
        [pscustomobject]@{
            Path = $rows[$i].Path; Stamp = $rows[$i].Stamp; AgeDays = $age
            Delete = $old
            Reason = if ($old) { "older than $OlderThanDays day(s)" } else { "within $OlderThanDays day(s)" }
        }
    }
}

function Get-SideCrabResidueSpec {
    <# Every FILE a SideCrab install leaves outside the repo, and what an uninstall does with
       each one. Pure: Join-Path only. This table IS the decision - Uninstall-SideCrab.ps1
       reads it rather than carrying its own list, so the two can never disagree.

       The rule, in one line: an uninstall removes WIRING and keeps DATA.
         wiring  - exists only to make the machine run SideCrab; meaningless once it is gone.
         data    - the operator's, or about the operator's work; outlives the install.
         cache   - derived, rebuildable, no operator value; grouped with data because deleting
                   it is never urgent and never required.
         backup  - the way BACK from an install. Never removed by an uninstall at any switch:
                   the moment you most need last week's settings.json is the moment after an
                   uninstall went wrong. Pruned deliberately, by Restore-SideCrab -PruneOlderThan. #>
    param(
        [Parameter(Mandatory)][string] $SettingsPath,
        [Parameter(Mandatory)][string] $ConfigPath,
        [Parameter(Mandatory)][string] $ChainPath
    )

    $stateDir = Split-Path -Parent $ChainPath

    @(
        [pscustomobject]@{
            Key = 'chain'; Path = $ChainPath; Kind = 'wiring'; Disposition = 'uninstall'
            Why = 'a handoff the installer writes so uninstall can put the prior status line back; nothing reads it afterwards'
        }
        [pscustomobject]@{
            Key = 'config'; Path = $ConfigPath; Kind = 'data'; Disposition = 'purge'
            Why = "the operator's own settings (quiet hours, recap repos, toast threshold) - the panelApprovals KEY is wiring and is cleared in place either way"
        }
        [pscustomobject]@{
            Key = 'history'; Path = (Join-Path $stateDir 'history.jsonl'); Kind = 'data'; Disposition = 'purge'
            Why = "a record of the operator's own sessions; a reinstall replays it and gets its doneToday back"
        }
        [pscustomobject]@{
            Key = 'toaststate'; Path = (Join-Path $stateDir 'toast-state.json'); Kind = 'data'; Disposition = 'purge'
            Why = 'the digest/budget ledger - removing it re-arms one digest and one budget toast, which is noise, not damage'
        }
        [pscustomobject]@{
            Key = 'limitscache'; Path = (Join-Path $stateDir 'limits-cache.json'); Kind = 'cache'; Disposition = 'purge'
            Why = 'derived from the usage feed and rebuilt on the next read; holds no secrets'
        }
        [pscustomobject]@{
            Key = 'glowlog'; Path = (Join-Path $stateDir 'glow.log'); Kind = 'log'; Disposition = 'purge'
            Why = 'the evidence for why the glow is parked (docs/BACKLOG.md) - keep it unless the operator asks'
        }
        [pscustomobject]@{
            Key = 'logs'; Path = (Join-Path $stateDir 'logs'); Kind = 'log'; Disposition = 'purge'
            Why = 'notifier and ack-handler logs; the only account of what a toast did'
        }
        [pscustomobject]@{
            Key = 'backups'; Path = (Join-Path (Split-Path -Parent $SettingsPath) (Get-SideCrabBackupPattern -SettingsPath $SettingsPath))
            Kind = 'backup'; Disposition = 'keep'
            Why = 'the only way back to a pre-SideCrab settings.json; an uninstall NEVER removes these - use Restore-SideCrab.ps1 -PruneOlderThan'
        }
        [pscustomobject]@{
            Key = 'statedir'; Path = $stateDir; Kind = 'data'; Disposition = 'purge-if-empty'
            Why = 'removed only when purging emptied it - another tool may have put a file there'
        }
    )
}

function Get-SideCrabUninstallScope {
    <# WHICH SURFACES a `-TaskName <name>` uninstall is allowed to remove. Pure.

       THE DEFECT THIS CLOSES: -TaskName narrowed the TASK deletion and nothing else, so
       `-TaskName SideCrab-glow` unregistered the glow task and then went on to strip the hooks,
       restore the status line and clear panelApprovals - tearing down a crabd install the
       operator had not asked about. (The two HKCU registrations were already narrowed; every
       other surface was not.)

       THE RULE: a narrowed uninstall removes only the named component's OWN surface. A full
       sweep removes everything. Ownership, once, here:
         crabd -> the settings.json hooks (they POST to crabd), the status line (it feeds
                  crabd) and the panelApprovals key (crabd's gate)
         toast -> the AUMID and the button schemes
         glow  -> its task, and nothing else
       A -TaskName that matches no component in the catalogue owns nothing beyond that task:
       it is a name we do not recognise, and guessing what else it implies is how a targeted
       uninstall becomes a full one. #>
    param(
        [Parameter(Mandatory)][object[]] $Spec,
        [string] $TaskName                       # empty / absent = the full sweep
    )

    $narrowed  = [bool] $TaskName
    $component = if ($narrowed) { @($Spec | Where-Object { $_.TaskName -eq $TaskName })[0] } else { $null }
    $key       = if ($component) { "$($component.Key)" } else { '' }

    [pscustomobject]@{
        Narrowed      = $narrowed
        TaskName      = $TaskName
        ComponentKey  = $key
        # $true when a narrowed name is not one the catalogue knows - reported, not guessed at.
        UnknownTask   = [bool] ($narrowed -and -not $component)
        Tasks         = $true                                          # always: it is the target
        Aumid         = [bool] (-not $narrowed -or $key -eq 'toast')
        Protocol      = [bool] (-not $narrowed -or $key -eq 'toast')
        Hooks         = [bool] (-not $narrowed -or $key -eq 'crabd')
        StatusLine    = [bool] (-not $narrowed -or $key -eq 'crabd')
        Approvals     = [bool] (-not $narrowed -or $key -eq 'crabd')
    }
}

function Get-SideCrabStatusLineRestoreDecision {
    <# What an uninstall should do with settings.json's statusLine. Pure.

       THE DEFECT THIS CLOSES: the restore read the chain file and wrote the saved prior over
       whatever was in settings.json WITHOUT asking whether the command sitting there was still
       ours. Install SideCrab, then install a different status line B, then uninstall SideCrab,
       and B was silently replaced by the status line SideCrab had displaced months earlier.
       The null-prior branch was worse: with no prior saved it called Remove('statusLine') and
       B was deleted outright.

       THE RULE - the same one the hook removal already follows: never write over a value that
       is not ours. Ownership is the marker match (Test-SideCrabStatusLineIsOurs), which is why
       CurrentIsOurs is a parameter here rather than re-derived. An ABSENT status line is not a
       foreign one: there is nothing to preserve, so a saved prior goes back into an empty slot.

       Actions: restore (put the saved prior back) - remove (ours is there, nothing was before
       it) - preserve-foreign (someone else's line is installed; leave it, say so) - none. #>
    param(
        [string] $CurrentCommand,
        [bool]   $CurrentIsOurs,
        [bool]   $SavedPresent,
        [AllowNull()] $SavedStatusLine
    )

    $hasCurrent = [bool] $CurrentCommand

    if ($hasCurrent -and -not $CurrentIsOurs) {
        return [pscustomobject]@{
            Action = 'preserve-foreign'; Changed = $false
            Reason = 'the status line configured now is not SideCrab''s - it is left exactly as it is'
        }
    }
    if (-not $SavedPresent) {
        # No chain file. Ours may still be installed from a run whose chain file was already
        # consumed; that case is the caller's existing "removed ours" path.
        if ($CurrentIsOurs) {
            return [pscustomobject]@{ Action = 'remove'; Changed = $true
                                      Reason = 'ours is installed and no saved prior exists' }
        }
        return [pscustomobject]@{ Action = 'none'; Changed = $false
                                  Reason = 'no SideCrab status line configured' }
    }
    if ($null -ne $SavedStatusLine) {
        return [pscustomobject]@{ Action = 'restore'; Changed = $true
                                  Reason = 'the prior status line is put back' }
    }
    if ($hasCurrent) {
        return [pscustomobject]@{ Action = 'remove'; Changed = $true
                                  Reason = 'nothing existed before us - the slot goes back to empty' }
    }
    [pscustomobject]@{ Action = 'none'; Changed = $false; Reason = 'nothing to restore' }
}

function Get-SideCrabPullPreflight {
    <# Is this working tree safe to `git pull --ff-only` into? Pure - it reads the porcelain
       text a caller already fetched.

       THE DEFECT THIS CLOSES: the updater PROMISED that "a diverged or dirty tree fails the
       pull and the script stops before touching any task" and did no such check. --ff-only
       refuses a merge, and refuses to overwrite a locally-modified file the incoming commits
       touch - but it fast-forwards happily over local edits to files those commits do NOT
       touch. So the promised stop never came, the tasks restarted, and the operator's
       uncommitted work stayed live under a repo that had moved. Measured on this very tree
       2026-08-27: ` M README.md` standing, and no incoming commit touching README.md would
       have been stopped by anything.

       Tracked changes BLOCK. Untracked files only WARN: they are not overwritten unless an
       incoming commit adds that exact path, and git refuses loudly and by name when it does -
       blocking on them would make the updater unusable on any tree with a stray log file. #>
    param([string] $StatusPorcelain)

    $lines = @("$StatusPorcelain" -split "`r?`n" | Where-Object { $_.Trim() })
    # Porcelain v1: columns 0-1 are the index/worktree status, '??' = untracked, '!!' = ignored
    # (only ever emitted under --ignored, listed here so it can never be read as a change).
    # StartsWith, NOT -like '??*': in a -like pattern '?' is a single-character WILDCARD, so
    # '??*' matches every line of two characters or more and read the whole tree as untracked -
    # which is to say it reported a dirty tree as clean, the exact failure being fixed here.
    $untracked = @($lines | Where-Object { $_.StartsWith('??') })
    $ignored   = @($lines | Where-Object { $_.StartsWith('!!') })
    $tracked   = @($lines | Where-Object { -not $_.StartsWith('??') -and -not $_.StartsWith('!!') })

    [pscustomobject]@{
        Clean         = [bool] ($lines.Count -eq 0)
        Blocked       = [bool] ($tracked.Count -gt 0)
        TrackedCount  = $tracked.Count
        # Substring(3) drops the two status columns and the space that follows them.
        Untracked     = @($untracked | ForEach-Object { $_.Substring(3) })
        Changed       = @($tracked   | ForEach-Object { $_.Substring(3) })
        Ignored       = $ignored.Count
        Reason        = if ($tracked.Count -gt 0) {
                            "$($tracked.Count) tracked file(s) modified: $((@($tracked | ForEach-Object { $_.Substring(3) }) | Select-Object -First 5) -join ', ')"
                        } elseif ($untracked.Count -gt 0) {
                            "clean of tracked changes; $($untracked.Count) untracked file(s) present"
                        } else { 'clean' }
    }
}

function Get-SideCrabGlowPreflight {
    <# Should the glow task be registered, given whether its binding actually imports? Pure.

       THE DEFECT THIS CLOSES: glow auto-installs the moment lighting\glow_launcher.pyw exists,
       and cuesdk was never checked. Without it the launcher starts, raises ImportError, exits,
       and the task reads Registered/Ready - a green-looking install that has never once
       controlled a light.

       AUTO-DETECTION IS AN INFERENCE and a failed import refutes it: skip, loudly, with the
       pip line. AN EXPLICIT -WithGlow IS AN INSTRUCTION: register it and say plainly that it
       will not light until the dependency is installed. Refusing there would block the whole
       install (crabd included) over a lighting dependency, which is the worse failure. #>
    param(
        [bool]   $Selected,
        [bool]   $Requested,
        [bool]   $Importable,
        [string] $Module        = 'cuesdk',
        [string] $RequirementsPath = ''
    )

    $pip = if ($RequirementsPath) { "pip install -r `"$RequirementsPath`"" } else { "pip install $Module" }

    if (-not $Selected)  { return [pscustomobject]@{ Install = $false; Status = 'not-selected'; Reason = 'glow is not part of this install'; Command = '' } }
    if ($Importable)     { return [pscustomobject]@{ Install = $true;  Status = 'ok';           Reason = "$Module imports";                  Command = '' } }
    if ($Requested)      { return [pscustomobject]@{ Install = $true;  Status = 'requested-broken'
                                                     Reason = "$Module does NOT import - the task is registered because you asked for it by name, and it will exit on every start until this is fixed"
                                                     Command = $pip } }
    [pscustomobject]@{ Install = $false; Status = 'skipped'
                       Reason  = "$Module does NOT import - glow was auto-detected from its script file, and a glow that cannot import its SDK would register green and never light"
                       Command = $pip }
}

# ------------------------------------------------------------- doctor decisions (pure)

function Get-SideCrabWatchedWriteTime {
    <# The NEWEST LastWriteTime across a component's watched files, and which file set it.
       Touches the filesystem (stat only) - the decision that uses it stays pure.

       Newest, not the entry point's: glow's entry point is a 26-line launcher that has not
       changed in months while sidecrab_glow.py / icue.py / decision.py move constantly. Taking
       the launcher's mtime reported "started after the script was last written" for a process
       running code from three rewrites ago. #>
    param([string[]] $Path)

    $newest = $null
    $winner = $null
    foreach ($p in @($Path)) {
        if (-not $p -or -not (Test-Path -LiteralPath $p)) { continue }
        $t = (Get-Item -LiteralPath $p).LastWriteTime
        if ($null -eq $newest -or $t -gt $newest) { $newest = $t; $winner = $p }
    }
    [pscustomobject]@{ WriteTime = $newest; Path = $winner; Checked = @($Path).Count }
}

function Get-SideCrabRunStateDecision {
    <# Is a registered component task's run state a fault? Pure.

       THE DEFECT THIS CLOSES: every SideCrab task is a LOGON DAEMON - AtLogOn trigger, no
       execution time limit, restart x3 - so for these three, Ready means the process is gone,
       not that it is waiting its turn. The doctor's only liveness question was crabd's health
       probe, and the freshness check answered "task is Ready - nothing is executing" as an OK
       row. A toast or glow task that had died read GREEN, and only crabd was ever offered a
       start.

       crabd is deliberately EXCLUDED by the caller, not here: its liveness is the health and
       port-owner rows, and a third row for the same fault would make one fault read as three. #>
    param(
        [bool]   $Registered,
        [string] $State
    )

    if (-not $Registered) {
        return [pscustomobject]@{ Ok = $true; Fault = $false; Verdict = 'not-registered'
                                  Reason = 'not registered - not expected to run' }
    }
    if ($State -eq 'Disabled') {
        # A stated decision (the glow is parked on the headless SDK crash, docs/BACKLOG.md).
        return [pscustomobject]@{ Ok = $true; Fault = $false; Verdict = 'disabled'
                                  Reason = 'disabled on purpose - not expected to run' }
    }
    if ($State -eq 'Running') {
        return [pscustomobject]@{ Ok = $true; Fault = $false; Verdict = 'running'; Reason = 'Running' }
    }
    [pscustomobject]@{
        Ok = $false; Fault = $true; Verdict = 'stopped'
        Reason = "enabled but $(if ($State) { $State } else { 'in no state' }) - a logon daemon that is not Running is not running"
    }
}

function Get-SideCrabStaleCodeDecision {
    <# "Task Running but on stale code": the process has been up since BEFORE the script it
       runs was last written, so what is executing is not what is on disk. Pure.

       This class is invisible to every other check - the task says Running, /v1/health says
       ok, and the fix that was just shipped is not in the process. A reported-vs-file version
       mismatch is decisive on its own and outranks the timestamps; the timestamps are what
       catches a change that did not move the version string. #>
    param(
        [string] $State,
        $LastRunTime,
        $ScriptWriteTime,
        [string] $ReportedVersion,
        [string] $FileVersion
    )

    if ($State -ne 'Running') {
        return [pscustomobject]@{ Verdict = 'not-running'; Stale = $false
                                  Reason = "task is $(if ($State) { $State } else { 'not registered' }) - nothing is executing" }
    }
    if ($ReportedVersion -and $FileVersion -and $ReportedVersion -ne $FileVersion) {
        return [pscustomobject]@{ Verdict = 'stale'; Stale = $true
                                  Reason = "serving $ReportedVersion, the file on disk is $FileVersion" }
    }
    if ($null -eq $LastRunTime -or $null -eq $ScriptWriteTime) {
        return [pscustomobject]@{ Verdict = 'unknown'; Stale = $false
                                  Reason = 'no start time or no script timestamp to compare' }
    }
    $started = [datetime] $LastRunTime
    $written = [datetime] $ScriptWriteTime
    if ($written -gt $started) {
        $mins = [math]::Round(($written - $started).TotalMinutes)
        return [pscustomobject]@{ Verdict = 'stale'; Stale = $true
                                  Reason = "script written $mins min AFTER the task last started" }
    }
    [pscustomobject]@{ Verdict = 'current'; Stale = $false
                       Reason = 'started after the script was last written' }
}

function Test-SideCrabHookUrlAllowed {
    <# allowedHttpHookUrls is a wildcard ALLOW-list: set it and an http hook whose URL matches
       no pattern is never called at all. Pure.

       $null Patterns = the key is unset = every URL is allowed (the default). An EMPTY list is
       not the same thing: the operator set the key and admitted nothing, which blocks us. #>
    param([string] $Url, $Patterns)

    if ($null -eq $Patterns) { return $true }
    $list = @($Patterns)
    if ($list.Count -eq 0) { return $false }
    foreach ($p in $list) { if ("$p" -and ($Url -like "$p")) { return $true } }
    $false
}

function Get-SideCrabCommandPath {
    <# The filesystem paths embedded in a command string. Pure.

       Quoted segments first - every command this repo writes quotes its interpreter and its
       script, because both break on the space in "Program Files" - then any bare drive-rooted
       token, which is what a hand-edited command tends to look like. #>
    param([string] $Command)

    if (-not $Command) { return @() }
    $found = [System.Collections.Generic.List[string]]::new()
    foreach ($m in [regex]::Matches($Command, '"([^"]+)"')) {
        $v = $m.Groups[1].Value
        if ($v -match '[\\/]' -and -not $found.Contains($v)) { $found.Add($v) }
    }
    foreach ($m in [regex]::Matches($Command, '(?<!["\w])([A-Za-z]:\\[^"\s]+)')) {
        $v = $m.Groups[1].Value.TrimEnd(',', ';')
        if (-not $found.Contains($v)) { $found.Add($v) }
    }
    $found      # unrolled - see Get-SideCrabTaskName
}

function Get-SideCrabPathOwnership {
    <# Is this path part of THIS checkout, another checkout, or nothing to do with us? Pure.

       'foreign-checkout' is the finding worth having: wiring that names a sidecrab path
       outside $RepoRoot still runs - it runs the OTHER copy, so a fix shipped here never
       takes effect and an uninstall here leaves it behind. Compared with a trailing separator
       so C:\Dev\sidecrab2 is not read as inside C:\Dev\sidecrab. #>
    param([string] $Path, [Parameter(Mandatory)][string] $RepoRoot)

    if (-not $Path) { return 'unknown' }
    $norm = ($Path -replace '/', '\').TrimEnd('\')
    $root = ($RepoRoot -replace '/', '\').TrimEnd('\')
    if ($norm -eq $root -or $norm.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase)) { return 'inside' }
    if ($norm -match '(?i)sidecrab|crabd') { return 'foreign-checkout' }
    'unrelated'
}

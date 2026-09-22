#Requires -Version 7.0
<#
.SYNOPSIS
    Lists, restores and prunes the timestamped backups SideCrab writes before it rewrites
    settings.json (default) or, with -Config, ~/.sidecrab/config.json - the supported way
    back from an install, update, uninstall or a panelApprovals toggle.

.DESCRIPTION
    Install-SideCrab.ps1 and Uninstall-SideCrab.ps1 copy ~/.claude/settings.json to
    "<path>.sidecrab-bak-yyyyMMdd-HHmmss" before every write, and Set/Clear-SideCrabPanelApprovals
    now do the same for ~/.sidecrab/config.json before every rewrite (SET-a2). This script is the
    other half of that: it reads the pile, explains what each backup would change, and puts one
    back. -Config points it at config.json's backups instead of settings.json's; the naming
    convention is identical, so the listing, restore and prune all work the same way.

    Config restores skip the SideCrab-vs-foreign split below: config.json is entirely SideCrab's
    own data file (quiet hours, recap repos, toast threshold, the panelApprovals key), so there is
    no third party's keys to protect - a restore simply replaces the whole file, backing up the
    current one first.

    THE ONE HAZARD, AND THE GUARD

    A restore replaces the WHOLE file, so it also reverts every key SideCrab does not own -
    permissions, theme, plugins, another tool's hooks - that the operator changed since the
    backup was taken. Those edits are invisible in a diff of SideCrab's own keys, which is
    exactly why they get lost.

    So every restore is compared first, split into the part SideCrab owns (its hook entries
    and its statusLine) and everything else. SideCrab-key differences are the POINT of a
    restore and never block. A FOREIGN key that differs does block: the script prints the
    keys, refuses, and prints the -Force command that overrides it. -Force is a statement
    that those edits are meant to go.

    A restore is itself reversible: the CURRENT settings.json is backed up - with the same
    naming convention, so it shows up in the next -List - before the restore is written.

    PRUNING
    -PruneOlderThan <days> deletes backups older than N days, except the newest one, which is
    never pruned at any age. A pile that is entirely older than the cutoff is what a stable
    install looks like, and pruning it to zero removes the only way back from the install
    that is running right now.

    Timestamps come from the backup's NAME, never its mtime: Copy-Item preserves the source's
    timestamp, so a backup taken today of a settings.json last edited in June has a June mtime.

    THE PANEL HOST (-Host)
    A different kind of restore, handled first and separately: -Host puts panel-host\dist.last-good
    back and restarts the panel task. That directory is the ONE generation a staged update keeps
    (setup\Update-SideCrab.ps1), so this is the answer to "the update I just ran broke the panel"
    when the update's own rollback did not run - because it succeeded, and the trouble showed up
    afterwards. The host that is replaced is moved to panel-host\dist.failed rather than deleted:
    it is the evidence for why.

.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1                       # list (the default)
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -Latest -WhatIf       # see what a restore does
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -Latest
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -Backup C:\Users\me\.claude\settings.json.sidecrab-bak-20260826-124940
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -PruneOlderThan 30
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -Config              # list config.json backups
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -Config -Latest      # restore the newest config.json backup
.EXAMPLE
    pwsh -File .\setup\Restore-SideCrab.ps1 -Host                # put the previous panel host back
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $SettingsPath = (Join-Path $HOME '.claude\settings.json'),
    [string] $ConfigPath   = (Join-Path $HOME '.sidecrab\config.json'),
    # Operate on config.json's backups instead of settings.json's. Same listing, restore and prune.
    [switch] $Config,
    [string] $Backup,
    [switch] $Latest,
    [switch] $List,
    [int]    $PruneOlderThan,
    [switch] $Force,
    # The PANEL HOST's kept generation, not a settings backup: put panel-host\dist.last-good
    # back and restart the task. Nothing else on this page applies to it - a host is a
    # directory of binaries, with no keys to compare and no third party's data to protect.
    [switch] $Host,
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$HookUrlMarker = '127.0.0.1:2722/v1/hook'

function Write-Step { param([string] $Message) Write-Host "  $Message" }

# The pile of backups beside the target, newest first - shared with Install/Uninstall through
# the ONE convention in Get-SideCrabBackupFile, so settings.json and config.json read identically.
function Get-BackupFile {
    param([string] $TargetPath)
    @(Get-SideCrabBackupFile -TargetPath $TargetPath)
}

function Read-JsonFile {
    <# A settings document, plus WHY it is not one when it is not. Never throws: an
       unparseable backup is a finding this script has to report, not a crash. #>
    param([string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ Present = $false; Settings = $null; Error = 'not present' }
    }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -Encoding utf8
        if (-not $raw.Trim()) { return [pscustomobject]@{ Present = $true; Settings = $null; Error = 'file is empty' } }
        $doc = $raw | ConvertFrom-Json -AsHashtable -Depth 40
        if ($doc -isnot [System.Collections.IDictionary]) {
            return [pscustomobject]@{ Present = $true; Settings = $null; Error = 'not a JSON object' }
        }
        [pscustomobject]@{ Present = $true; Settings = $doc; Error = $null }
    } catch {
        [pscustomobject]@{ Present = $true; Settings = $null; Error = $_.Exception.Message }
    }
}

function Format-Age {
    param([datetime] $Stamp, [datetime] $Now = (Get-Date))
    $span = $Now - $Stamp
    if ($span.TotalMinutes -lt 1)  { return 'just now' }
    if ($span.TotalHours   -lt 1)  { return '{0:N0} min ago'  -f $span.TotalMinutes }
    if ($span.TotalDays    -lt 1)  { return '{0:N1} h ago'    -f $span.TotalHours }
    '{0:N1} d ago' -f $span.TotalDays
}

function Show-BackupList {
    param([object[]] $Backups, $Current, [string] $TargetPath, [switch] $Config)

    $leaf = Split-Path -Leaf $TargetPath
    Write-Host "SideCrab $(if ($Config) { 'config' } else { 'settings' }) backups  ($TargetPath)"
    if (@($Backups).Count -eq 0) {
        Write-Step "none found - a backup is written before every $leaf change"
        return
    }

    $i = 0
    foreach ($b in $Backups) {
        $i++
        if (-not $b.Stamp) {
            Write-Host ('  [{0}] {1}   (name carries no timestamp - not restorable by -Latest)' -f $i, $b.Name)
            continue
        }
        Write-Host ('  [{0}] {1}' -f $i, $b.Name)
        Write-Host ('      taken {0:yyyy-MM-dd HH:mm:ss}  ({1})  {2} bytes' -f $b.Stamp, (Format-Age -Stamp $b.Stamp), $b.Bytes)

        $doc = Read-JsonFile -Path $b.Path
        if ($doc.Error) {
            Write-Host ('      UNREADABLE: {0} - restoring it would leave a broken {1}' -f $doc.Error, $leaf) -ForegroundColor Red
            continue
        }
        if ($Config) {
            # config.json is wholly SideCrab's own data file - no third-party keys to protect,
            # so there is no SideCrab-vs-foreign split. Just say what a restore would bring back.
            $keys = @($doc.Settings.Keys)
            Write-Host ('      config:   {0} key(s): {1}' -f $keys.Count, ($keys -join ', '))
            continue
        }
        $cmp = Compare-SideCrabSettingsPair -Backup $doc.Settings -Current $Current -Marker $HookUrlMarker
        if ($cmp.SideCrabDiff.Count -eq 0) {
            Write-Host '      sidecrab: same wiring as now'
        } else {
            foreach ($d in $cmp.SideCrabDiff) { Write-Host ('      sidecrab: {0} - {1}' -f $d.Key, $d.State) }
        }
        if (-not $cmp.ForeignChanged) {
            Write-Host '      other:    no non-SideCrab key has changed since this backup'
        } else {
            Write-Host ('      other:    {0} key(s) YOU changed since this backup - a restore would touch them:' -f $cmp.ForeignDiff.Count) -ForegroundColor Yellow
            foreach ($d in $cmp.ForeignDiff) { Write-Host ('                  {0} - {1}' -f $d.Key, $d.State) -ForegroundColor Yellow }
        }
    }
    Write-Host ''
    Write-Step 'restore with: -Latest   (or -Backup <path>).  Add -WhatIf to rehearse.'
}

# ------------------------------------------------------------------------------ run

# ---- the panel host's kept generation (MF-006)
# Handled first and on its own: this restores a DIRECTORY of binaries, so none of the
# settings-file machinery below - the SideCrab-vs-foreign key split, the backup pile, the
# prune - has anything to say about it. A staged update keeps exactly one generation, which is
# what makes this a one-command answer to "the update I just ran broke the panel".
if ($Host) {
    $distPath     = Join-Path $RepoRoot 'panel-host\dist'
    $lastGoodPath = Join-Path $RepoRoot 'panel-host\dist.last-good'
    $taskName     = 'SideCrab-panel'

    Write-Host 'SideCrab panel host restore'
    Write-Step "from:    $lastGoodPath"
    Write-Step "into:    $distPath"
    if (-not (Test-Path -LiteralPath $lastGoodPath)) {
        Write-Host "  REFUSED: there is no $lastGoodPath. A kept generation only exists after a staged update has swapped a host out (setup\Update-SideCrab.ps1)." -ForegroundColor Red
        exit 1
    }
    $before = (Get-SideCrabComponentVersion -RepoRoot $RepoRoot -PanelExe (Join-Path $distPath 'SideCrab.Panel.exe')).Host
    $keptEx = Join-Path $lastGoodPath 'SideCrab.Panel.exe'
    $kept   = if (Test-Path -LiteralPath $keptEx) { (Get-Item -LiteralPath $keptEx).VersionInfo.FileVersion } else { 'unknown' }
    Write-Step "host:    $before now, $kept kept"

    if ($PSCmdlet.ShouldProcess($distPath, "Restore the panel host from $lastGoodPath")) {
        # Stopped first: a directory whose executable is running cannot be renamed.
        $state = Get-SideCrabTaskState -TaskName $taskName
        if ($state.Registered) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            $deadline = (Get-Date).AddSeconds(15)
            while ((Get-Date) -lt $deadline -and
                   @(Get-Process -Name 'SideCrab.Panel' -ErrorAction SilentlyContinue).Count -gt 0) {
                Start-Sleep -Milliseconds 300
            }
        }
        $res = Restore-SideCrabHostLastGood -DistPath $distPath -LastGoodPath $lastGoodPath
        Write-Step "restore: $($res.Reason)"
        if ($state.Registered -and $state.State -ne 'Disabled') {
            Start-ScheduledTask -TaskName $taskName
            $after = Get-SideCrabTaskState -TaskName $taskName
            Write-Step "task:    $taskName is $($after.State)"
        } else {
            Write-Step "task:    $taskName is not registered or is disabled - nothing was started"
        }
        $now = (Get-SideCrabComponentVersion -RepoRoot $RepoRoot -PanelExe (Join-Path $distPath 'SideCrab.Panel.exe')).Host
        Write-Step "host:    $now is live"
    }
    exit 0
}

# The one target every step below reads. -Config flips it to config.json; the backup convention,
# the listing, the restore and the prune are identical either way (SET-a2).
$targetPath = if ($Config) { $ConfigPath } else { $SettingsPath }
$targetLeaf = Split-Path -Leaf $targetPath

$backups = @(Get-BackupFile -TargetPath $targetPath)
$currentDoc = Read-JsonFile -Path $targetPath
if ($currentDoc.Present -and $currentDoc.Error) {
    # A current file we cannot parse means the comparison cannot run - and is itself the
    # best possible reason to restore. Say so rather than comparing against $null silently.
    Write-Warning "$targetPath does not parse ($($currentDoc.Error)) - every key will read as 'added since the backup'. Restoring is probably what you want."
}

$doRestore = $Latest -or $Backup
$doPrune   = $PSBoundParameters.ContainsKey('PruneOlderThan')

if (-not $doRestore -and -not $doPrune) {
    Show-BackupList -Backups $backups -Current $currentDoc.Settings -TargetPath $targetPath -Config:$Config
    exit 0
}
if ($List) { Show-BackupList -Backups $backups -Current $currentDoc.Settings -TargetPath $targetPath -Config:$Config; Write-Host '' }

# ---- restore
if ($doRestore) {
    if ($Latest -and $Backup) { throw 'pass -Latest or -Backup, not both' }

    $target = if ($Backup) {
        if (-not (Test-Path -LiteralPath $Backup)) { throw "backup not found: $Backup" }
        $item = Get-Item -LiteralPath $Backup
        [pscustomobject]@{ Path = $item.FullName; Name = $item.Name
                           Stamp = Read-SideCrabBackupStamp -Name $item.Name; Bytes = $item.Length }
    } else {
        $newest = @($backups | Where-Object { $_.Stamp }) | Select-Object -First 1
        if (-not $newest) { throw "no timestamped backup found beside $targetPath - nothing to restore" }
        $newest
    }

    Write-Host 'SideCrab restore'
    Write-Step "from:    $($target.Path)"
    Write-Step "into:    $targetPath"
    if ($target.Stamp) { Write-Step ('taken:   {0:yyyy-MM-dd HH:mm:ss}  ({1})' -f $target.Stamp, (Format-Age -Stamp $target.Stamp)) }

    $backupDoc = Read-JsonFile -Path $target.Path
    if ($backupDoc.Error) {
        # Writing an unparseable file back is worse than the state that prompted the restore:
        # the consumer then starts with no config/settings at all.
        $msg = "the backup does not parse ($($backupDoc.Error)) - restoring it would leave a broken $targetLeaf"
        if (-not $Force) { Write-Host "  REFUSED: $msg. Re-run with -Force if that is genuinely what you want." -ForegroundColor Red; exit 1 }
        Write-Warning "$msg - proceeding on -Force"
    }

    if ($Config) {
        # config.json is wholly SideCrab's own file - no foreign-key guard to run. A restore
        # replaces the whole config; the current one is backed up first (below), so it is
        # reversible. -Force still overrides the identical short-circuit.
        $curJson = if ($currentDoc.Settings) { ConvertTo-SideCrabCanonicalJson -Value $currentDoc.Settings } else { $null }
        $bakJson = if ($backupDoc.Settings)  { ConvertTo-SideCrabCanonicalJson -Value $backupDoc.Settings }  else { $null }
        if ($curJson -eq $bakJson -and -not $Force) {
            Write-Step "identical to the current $targetLeaf - nothing to restore"
            exit 0
        }
        Write-Step 'config:   a restore replaces the whole config.json (the current one is backed up first)'
    } else {
        $cmp = Compare-SideCrabSettingsPair -Backup $backupDoc.Settings -Current $currentDoc.Settings -Marker $HookUrlMarker
        foreach ($d in $cmp.SideCrabDiff) { Write-Step "sidecrab: $($d.Key) - $($d.State)" }
        if ($cmp.SideCrabDiff.Count -eq 0) { Write-Step 'sidecrab: same wiring as now' }

        if ($cmp.ForeignChanged) {
            Write-Host ''
            Write-Host '  Keys that are NOT SideCrab''s have changed since this backup was taken.' -ForegroundColor Yellow
            Write-Host '  A restore replaces the whole file, so these edits of yours go with it:' -ForegroundColor Yellow
            foreach ($d in $cmp.ForeignDiff) { Write-Host ('    {0} - {1}' -f $d.Key, $d.State) -ForegroundColor Yellow }
            if (-not $Force) {
                $selector = if ($Latest) { '-Latest' } else { '-Backup "{0}"' -f $target.Path }
                Write-Host ''
                Write-Host '  REFUSED - nothing was changed.' -ForegroundColor Red
                Write-Host '  If those edits are meant to go, re-run with -Force:' -ForegroundColor Red
                Write-Host ('    pwsh -File setup\Restore-SideCrab.ps1 {0} -Force' -f $selector) -ForegroundColor Red
                exit 1
            }
            Write-Step 'proceeding on -Force'
        } else {
            Write-Step 'other:    no non-SideCrab key has changed since this backup'
        }

        if ($cmp.Identical -and -not $Force) {
            Write-Step 'identical to the current settings.json - nothing to restore'
            exit 0
        }
    }

    if ($PSCmdlet.ShouldProcess($targetPath, "Restore from $($target.Name)")) {
        # The restore is itself reversible: the file about to be overwritten is copied with
        # the SAME naming convention, so it appears in the next -List like any other backup.
        if (Test-Path -LiteralPath $targetPath) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $safety = "$targetPath.sidecrab-bak-$stamp"
            Copy-Item -LiteralPath $targetPath -Destination $safety -Force
            Write-Step "backup:  $safety  (the state you are restoring OVER)"
        }
        Copy-Item -LiteralPath $target.Path -Destination $targetPath -Force
        Write-Step 'restored.'

        $after = Read-JsonFile -Path $targetPath
        if ($after.Error) {
            Write-Warning "the restored $targetPath does not parse ($($after.Error))"
        } elseif ($Config) {
            $keys = @($after.Settings.Keys)
            Write-Step "verify:  config.json restored, $($keys.Count) key(s)$(if ($keys.Count) { ": $($keys -join ', ')" })"
        } else {
            $ours = Split-SideCrabSettings -Settings $after.Settings -Marker $HookUrlMarker
            $events = @($ours.Ours['hooks'].Keys)
            Write-Step "verify:  $($events.Count) SideCrab hook event(s)$(if ($events.Count) { ": $($events -join ', ')" }); statusLine $(if ($ours.Ours['statusLine']) { 'is ours' } else { 'is not ours' })"
            if ($events.Count -eq 0) {
                # Restoring a pre-install file leaves the tasks running and unfed: crabd keeps
                # answering /v1/health while no hook ever reaches it again.
                Write-Host '  NOTE: the restored file has no SideCrab hooks. Any SideCrab task still registered will run and never be fed - run setup\Install-SideCrab.ps1 to re-wire, or setup\Uninstall-SideCrab.ps1 to finish removing it.' -ForegroundColor Yellow
            }
        }
    }
}

# ---- prune
if ($doPrune) {
    if ($PruneOlderThan -lt 0) { throw '-PruneOlderThan takes a number of days (0 or more)' }
    Write-Host ''
    Write-Host "SideCrab backup prune  (older than $PruneOlderThan day(s))"

    # Re-read: a restore above just added one.
    $rows = @(Get-BackupFile -TargetPath $targetPath | Where-Object { $_.Stamp })
    if ($rows.Count -eq 0) { Write-Step 'no timestamped backups found'; exit 0 }

    $decisions = @(Get-SideCrabPruneDecision -Backup $rows -OlderThanDays $PruneOlderThan)
    $deleted = 0
    foreach ($d in $decisions) {
        if (-not $d.Delete) {
            Write-Step ('keep:    {0}  ({1:N1} d)  - {2}' -f (Split-Path -Leaf $d.Path), $d.AgeDays, $d.Reason)
            continue
        }
        if ($PSCmdlet.ShouldProcess($d.Path, "Delete SideCrab $targetLeaf backup")) {
            Remove-Item -LiteralPath $d.Path -Force
            Write-Step ('deleted: {0}  ({1:N1} d)' -f (Split-Path -Leaf $d.Path), $d.AgeDays)
            $deleted++
        }
    }
    Write-Step "pruned $deleted of $($decisions.Count) backup(s)"
}

exit 0

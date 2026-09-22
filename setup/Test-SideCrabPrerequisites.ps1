#Requires -Version 7.0
<#
.SYNOPSIS
    Checks that this PC has what SideCrab needs before anything is installed. Read-only.

.DESCRIPTION
    One row per prerequisite, each one saying what was found, what to do about it and where to
    get it. Nothing is installed, started or written: this reads PATH, the .NET runtime list and
    four registry values and prints what it saw.

      PowerShell 7          the setup scripts are 7.0-only
      Python 3              a real install with pythonw.exe beside it, not the Store alias stub
      .NET Desktop Runtime  the major the panel host targets, read from the csproj in a source
                            checkout and from package-manifest.json in a package
      WebView2 Evergreen    the runtime the panel window renders in
      curl.exe              ONLY while the hook fragment still has a "type": "command" hook
      HWiNFO                optional; without it temperatures are unavailable and nothing else
                            changes

    Exit code is 0 when nothing required is missing and 1 otherwise, so this is CI-safe and can
    gate an install. An UNKNOWN row - a probe that could not answer - warns and does not fail:
    refusing to install because a registry read threw is worse than trying.

    Install-SideCrab.ps1 runs the same check itself and declines to register the panel task when
    the Desktop Runtime or WebView2 is missing, rather than registering a task that fails at
    every logon.

.EXAMPLE
    pwsh -File .\setup\Test-SideCrabPrerequisites.ps1
.EXAMPLE
    pwsh -File .\setup\Test-SideCrabPrerequisites.ps1 -Quiet   # exit code only
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch] $Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$verdict = Test-SideCrabPrerequisite -RepoRoot $RepoRoot

if (-not $Quiet) {
    Write-Host 'SideCrab prerequisites'
    $manifest = Read-SideCrabPackageManifest -RepoRoot $RepoRoot
    if ($manifest.Present) { Write-Host "  package: $(Get-SideCrabPackageIdentity -Manifest $manifest.Manifest)" }
    else                   { Write-Host "  source:  $RepoRoot (no package manifest)" }
    Write-Host ''

    foreach ($row in $verdict.Rows) {
        $colour = switch ($row.Status) {
            'ok'       { 'Green' }
            'missing'  { 'Red' }
            'warn'     { 'Yellow' }
            'unknown'  { 'Yellow' }
            default    { 'DarkGray' }
        }
        Write-Host ('  {0,-8} {1,-28} {2}' -f $row.Status.ToUpperInvariant(), $row.Title, $row.Detail) -ForegroundColor $colour
        if ($row.Fix)  { Write-Host ('           fix:  {0}' -f $row.Fix) -ForegroundColor DarkGray }
        if ($row.Link) { Write-Host ('           get:  {0}' -f $row.Link) -ForegroundColor DarkGray }
    }
    Write-Host ''
    if ($verdict.HardMiss) {
        Write-Host "  MISSING: $($verdict.Missing -join ', '). Install those, then run this again." -ForegroundColor Red
    } else {
        Write-Host '  Everything SideCrab needs is here.' -ForegroundColor Green
    }
    if ($verdict.PanelBlocked) {
        Write-Host "  The panel window cannot run on this PC yet: $($verdict.PanelReason)" -ForegroundColor Red
        Write-Host '  Install-SideCrab.ps1 will install the companion and the notifier and skip the panel task.' -ForegroundColor Red
    }
}

exit $verdict.ExitCode

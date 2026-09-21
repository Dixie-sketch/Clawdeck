#Requires -Version 7.0
<#
.SYNOPSIS
    Builds the standalone panel host (panel-host\SideCrab.Panel) into panel-host\dist.

.DESCRIPTION
    `dotnet publish` of the WebView2 kiosk window that shows the panel full-screen on the
    Xeneon Edge without iCUE. Framework-dependent: the PC needs the .NET 10 Desktop Runtime
    to RUN it and the .NET 10 SDK to BUILD it; the WebView2 Runtime ships with Windows 11
    and with most Windows 10 installs.

    Install-SideCrab.ps1 -Panel runs this when the exe is missing, and Update-SideCrab.ps1
    runs it before restarting the panel task, so the running window is always the checked-
    out code. Run it by hand after editing anything under panel-host\.

    Output: panel-host\dist\SideCrab.Panel.exe (dist\ is gitignored). Exit code 0 only when
    the exe exists afterwards.

.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPanel.ps1
.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPanel.ps1 -Test    # also runs the host's unit tests
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch] $Test
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$project = Join-Path $RepoRoot 'panel-host\SideCrab.Panel\SideCrab.Panel.csproj'
$tests   = Join-Path $RepoRoot 'panel-host\SideCrab.Panel.Tests\SideCrab.Panel.Tests.csproj'
$out     = Join-Path $RepoRoot 'panel-host\dist'
$exe     = Join-Path $out 'SideCrab.Panel.exe'

Write-Host 'SideCrab panel host build'
Write-Host "  project: $project"
Write-Host "  output:  $out"

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Write-Host '  FAIL:    dotnet not found. Install the .NET 10 SDK (https://dotnet.microsoft.com/download) and re-run.' -ForegroundColor Red
    exit 1
}
$sdk = "$(& dotnet --version 2>$null)".Trim()
$major = 0
if ($sdk -match '^(\d+)\.') { $major = [int] $Matches[1] }
if ($major -lt 10) {
    Write-Host "  FAIL:    dotnet SDK $sdk found; the panel host needs 10.0 or newer." -ForegroundColor Red
    exit 1
}
Write-Host "  sdk:     $sdk"
if (-not (Test-Path -LiteralPath $project)) {
    Write-Host "  FAIL:    $project is missing - is this a full checkout?" -ForegroundColor Red
    exit 1
}

if ($Test) {
    & dotnet test $tests -c Release --nologo -v q
    if ($LASTEXITCODE -ne 0) { Write-Host '  FAIL:    unit tests failed' -ForegroundColor Red; exit $LASTEXITCODE }
    Write-Host '  tests:   passed'
}

# publish, not build: the output folder is the flat set the task action points at, with
# WebView2Loader.dll under runtimes\ where the SDK's loader expects it.
& dotnet publish $project -c Release -o $out --nologo -v q
if ($LASTEXITCODE -ne 0) { Write-Host "  FAIL:    dotnet publish exited $LASTEXITCODE" -ForegroundColor Red; exit $LASTEXITCODE }
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "  FAIL:    publish finished but $exe is missing" -ForegroundColor Red
    exit 1
}
$item = Get-Item -LiteralPath $exe
Write-Host ("  built:   {0} ({1:N0} bytes, {2:yyyy-MM-dd HH:mm:ss})" -f $exe, $item.Length, $item.LastWriteTime)
exit 0

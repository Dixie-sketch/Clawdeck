#Requires -Version 7.0
<#
.SYNOPSIS
    Builds the standalone panel host (panel-host\SideCrab.Panel) into panel-host\dist.

.DESCRIPTION
    `dotnet publish` of the WebView2 kiosk window that shows the panel full-screen on the
    Xeneon Edge. Framework-dependent: the PC needs the .NET 10 Desktop Runtime to RUN it and
    the .NET 10 SDK to BUILD it; the WebView2 Runtime ships with Windows 11 and with most
    Windows 10 installs.

    Install-SideCrab.ps1 runs this when the exe is missing, and Update-SideCrab.ps1 runs it
    before restarting the panel task, so the running window is always the checked-out code.
    Both treat a non-zero exit as a failure of the whole operation (SCA-003). Run it by hand
    after editing anything under panel-host\.

    Output: panel-host\dist\SideCrab.Panel.exe (dist\ is gitignored). Exit code 0 only when
    the exe exists afterwards. -OutDir publishes somewhere else instead, which is how
    Update-SideCrab.ps1 stages a new host beside the running one before swapping it in.

    It also writes build-record.json beside the executable: the SHA-256 of what this build
    produced, its version and the time. That is what lets Repair-SideCrab.ps1 prove the binary
    on disk IS this build rather than comparing timestamps, which answer a different question.

.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPanel.ps1
.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPanel.ps1 -Test    # also runs the host's unit tests
.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPanel.ps1 -OutDir C:\Dev\sidecrab\panel-host\dist.staging
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $OutDir,
    [switch] $Test
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

$project = Join-Path $RepoRoot 'panel-host\SideCrab.Panel\SideCrab.Panel.csproj'
$tests   = Join-Path $RepoRoot 'panel-host\SideCrab.Panel.Tests\SideCrab.Panel.Tests.csproj'
$out     = if ($OutDir) { $OutDir } else { Join-Path $RepoRoot 'panel-host\dist' }
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

# The identity of what was just produced, beside what was produced. The doctor reads it to
# answer "is the executable on disk this build", which no timestamp can answer: a file copied
# in by hand and a half-written publish both have a plausible mtime.
$facts  = Get-SideCrabHostProjectFacts -RepoRoot $RepoRoot
$sha    = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$record = [ordered]@{
    sha256               = $sha
    version              = $facts.Version
    targetFramework      = $facts.Tfm
    targetFrameworkMajor = $facts.TfmMajor
    deployment           = 'framework-dependent'
    builtUtc             = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}
[IO.File]::WriteAllText((Join-Path $out 'build-record.json'),
                        (($record | ConvertTo-Json -Depth 5) + "`n"),
                        (New-Object Text.UTF8Encoding $false))
Write-Host "  sha256:  $sha"
exit 0

#Requires -Version 7.0
<#
.SYNOPSIS
    Builds the distributable package: dist\SideCrab-<version>-win-x64.zip, installable on a PC
    with no Git, no .NET SDK and no checkout.

.DESCRIPTION
    MF-005. The package carries the PUBLISHED panel host, so the .NET SDK stops being an
    end-user prerequisite and becomes a developer one. What goes in is the product and the
    documents the person installing it reads: companion\, notifier\, hooks\, widget\, setup\,
    panel-host\dist\, README, LICENSE, CHANGELOG, the two guides and docs\images. What never
    goes in is how the product is made: no test suite, no developer notes, no audit history,
    no tools\, no .git, no build intermediates (setup\SideCrab.Common.ps1,
    Test-SideCrabPackagePathExcluded).

    FRAMEWORK-DEPENDENT, MEASURED. Publishing this project both ways on 2026-09-22:
    framework-dependent is 27,841,333 bytes over 16 files (7,342,888 zipped); self-contained is
    141,538,110 bytes over 229 files (56,113,910 zipped). Self-contained is 7.6x the download
    and removes only ONE of the two runtime prerequisites - the WebView2 Evergreen runtime is a
    separate install either way - so the trade is 49 MB of download against one .NET install
    that setup\Test-SideCrabPrerequisites.ps1 names with its download link. Framework-dependent
    stands.

    THE MANIFEST. package-manifest.json at the package root records the product version, each
    component's version read from its own file, the git commit, the build time and a SHA-256 per
    file. Install-SideCrab.ps1 verifies the package against it before it installs anything and
    names the first file that does not match; Repair-SideCrab.ps1 uses the host's entry to prove
    the executable on disk is the one that shipped.

    <version> is the PRODUCT version, widget\version.json - the number the public releases are
    named after. The three component versions are recorded separately, because there is no one
    SideCrab version and printing one would be right about at most one component.

.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPackage.ps1
.EXAMPLE
    pwsh -File .\setup\Build-SideCrabPackage.ps1 -SkipBuild   # package the host already in dist\
#>
[CmdletBinding()]
param(
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $OutDir,
    # Package panel-host\dist as it stands instead of publishing it again. For CI, where the
    # host was already built by an earlier step, and for packaging a host you are testing.
    [switch] $SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'SideCrab.Common.ps1')

function Write-Step { param([string] $Message) Write-Host "  $Message" }

$OutDir = if ($OutDir) { $OutDir } else { Join-Path $RepoRoot 'dist' }
$hostDist = Join-Path $RepoRoot 'panel-host\dist'
$hostExe  = Join-Path $hostDist 'SideCrab.Panel.exe'

Write-Host 'SideCrab package build'
Write-Step "repo:    $RepoRoot"

# ---- 1. the product version, and the component versions beside it
$widget = Get-SideCrabWidgetVersion -RepoRoot $RepoRoot
if (-not $widget.Present) { throw "cannot name the package: $($widget.Reason)" }
$version = $widget.Version
$facts   = Get-SideCrabHostProjectFacts -RepoRoot $RepoRoot
if (-not $facts.Present) { throw "cannot read the panel host project at $($facts.Path)" }

# ---- 2. the published host
if ($SkipBuild) {
    if (-not (Test-Path -LiteralPath $hostExe)) {
        throw "-SkipBuild was passed and $hostExe is not there. Run setup\Build-SideCrabPanel.ps1 first, or drop -SkipBuild."
    }
    Write-Step "host:    reusing $hostExe"
} else {
    & (Join-Path $PSScriptRoot 'Build-SideCrabPanel.ps1') -RepoRoot $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "the panel host did not build (exit $LASTEXITCODE) - a package without it would need an SDK on the PC that installs it" }
}

$components = Get-SideCrabComponentVersion -RepoRoot $RepoRoot -PanelExe $hostExe
Write-Step "version: $version  (crabd $($components.Crabd)  |  widget $($components.Widget)  |  host $($components.Host))"

# ---- 3. stage
$name  = "SideCrab-$version-win-x64"
$stage = Join-Path $OutDir $name
$zip   = Join-Path $OutDir "$name.zip"
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$copied = 0
foreach ($row in Get-SideCrabPackageContentSpec -RepoRoot $RepoRoot) {
    if (-not (Test-Path -LiteralPath $row.Source)) {
        if ($row.Required) { throw "$($row.Source) is missing - a package without it is not installable" }
        Write-Step "skip:    $($row.Target) (not in this checkout)"
        continue
    }
    if ($row.Kind -eq 'file') {
        $dest = Join-Path $stage $row.Target
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $row.Source -Destination $dest -Force
        $copied++
        continue
    }
    # A directory, file by file, so the exclusion rule applies to every path rather than to the
    # top of a tree. Copying the tree and deleting afterwards would put the excluded files on
    # disk first, and a build that is interrupted between the two ships them.
    $srcRoot = (Resolve-Path -LiteralPath $row.Source).ProviderPath.TrimEnd('\')
    foreach ($f in Get-ChildItem -LiteralPath $srcRoot -Recurse -File -Force) {
        $rel = Join-Path $row.Target ($f.FullName.Substring($srcRoot.Length).TrimStart('\'))
        if (Test-SideCrabPackagePathExcluded -RelativePath $rel) { continue }
        $dest = Join-Path $stage $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
        $copied++
    }
}
Write-Step "staged:  $copied file(s) into $stage"

# ---- 4. the manifest
$sha = ''
try { $sha = (& git -C $RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1) } catch { $sha = '' }
if (-not "$sha".Trim()) { $sha = 'unknown (no git checkout)' }

$hashes = Measure-SideCrabPackageHash -Root $stage
$files  = [ordered]@{}
foreach ($k in @($hashes.Keys | Sort-Object)) { $files[$k] = $hashes[$k] }

$manifest = [ordered]@{
    product              = 'SideCrab'
    version              = $version
    platform             = 'win-x64'
    deployment           = 'framework-dependent'
    targetFramework      = $facts.Tfm
    targetFrameworkMajor = $facts.TfmMajor
    components           = [ordered]@{ crabd = $components.Crabd; widget = $components.Widget; host = $facts.Version }
    gitSha               = "$sha".Trim()
    builtUtc             = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    fileCount            = $files.Count
    files                = $files
}
[IO.File]::WriteAllText((Join-Path $stage (Get-SideCrabPackageManifestName)),
                        (($manifest | ConvertTo-Json -Depth 10) + "`n"),
                        (New-Object Text.UTF8Encoding $false))
Write-Step "manifest: $($files.Count) file hash(es), git $($manifest.gitSha)"

# ---- 5. zip
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal
$zipItem = Get-Item -LiteralPath $zip

# The staged tree is left beside the zip on purpose: it is the thing to run an install from
# when testing a package, and deleting it would mean unzipping our own output to check it.
Write-Host ''
Write-Step ("package: {0} ({1:N0} bytes)" -f $zip, $zipItem.Length)
Write-Step "identity: $(Get-SideCrabPackageIdentity -Manifest $manifest)"
Write-Step "extracted tree kept at $stage"
Write-Host ''
Write-Host 'Install from it with:  pwsh -File .\setup\Install-SideCrab.ps1   (run inside the extracted folder)'
exit 0

#Requires -Version 7.0
<#
.SYNOPSIS
    Runs the SideCrab setup tests - under Pester 5 when it is installed, otherwise
    under the plain-PowerShell shim defined below.

.DESCRIPTION
    The tests parse the setup scripts and exercise their pure decision helpers, which
    are lifted out by AST. Nothing is installed: no scheduled task is registered,
    started or stopped, and settings.json is never written.

    Exit code is 0 when every test passes, 1 otherwise - so this is CI-safe.

.EXAMPLE
    pwsh -File .\setup\tests\RunTests.ps1
.EXAMPLE
    pwsh -File .\setup\tests\RunTests.ps1 -NoPester    # force the shim
#>
[CmdletBinding()]
param(
    [string] $Path,
    [switch] $NoPester
)

$ErrorActionPreference = 'Stop'

$TestsDir  = $PSScriptRoot
$TestFiles = @(Get-ChildItem -LiteralPath $TestsDir -Filter '*.Tests.ps1' -File | Sort-Object Name)
if ($Path) { $TestFiles = @(Get-Item -LiteralPath $Path) }
if ($TestFiles.Count -eq 0) { throw "no *.Tests.ps1 found in $TestsDir" }

# ------------------------------------------------------------------------ Pester

$pester = if ($NoPester) { $null } else {
    Get-Module -ListAvailable Pester |
        Where-Object { $_.Version.Major -ge 5 } |
        Sort-Object Version -Descending | Select-Object -First 1
}

if ($pester) {
    Import-Module Pester -MinimumVersion 5.0 -ErrorAction Stop
    Write-Host "Pester $($pester.Version)"
    $cfg = New-PesterConfiguration
    $cfg.Run.Path        = @($TestFiles | ForEach-Object { $_.FullName })
    $cfg.Run.PassThru    = $true
    $cfg.Output.Verbosity = 'Detailed'
    $result = Invoke-Pester -Configuration $cfg
    exit ([int]($result.FailedCount -gt 0))
}

# ------------------------------------------------------- plain-PowerShell fallback
# A deliberately small shim: enough Pester surface for these tests (Describe/Context/
# It/BeforeAll/AfterAll plus the Should operators used) so the suite runs on a machine
# with no Pester installed. It is not a Pester implementation - unsupported operators
# throw rather than passing quietly, which is the only failure mode that matters here.

Write-Host 'Pester 5 not available - running the plain-PowerShell shim'

$script:Depth    = 0
$script:Passed   = 0
$script:Failed   = 0
$script:Failures = @()

function script:Write-Indent {
    param([string] $Text, [string] $Color = 'Gray')
    Write-Host ((' ' * (2 * $script:Depth)) + $Text) -ForegroundColor $Color
}

$script:AfterQueue = $null

function script:Invoke-Block {
    <# One Describe/Context: run the body, then run whatever AfterAll it queued.

       The queue is the point. A block's fixtures are dot-sourced in FILE order, so an
       AfterAll written before the It blocks it cleans up after - which is where Pester puts
       it - used to delete the context's temp directory before a single It had run, and two
       passing tests reported as failures under this shim alone. #>
    param([string] $Name, [scriptblock] $Fixture)

    script:Write-Indent $Name 'Cyan'
    $script:Depth++
    $outer = $script:AfterQueue
    $script:AfterQueue = [System.Collections.Generic.List[scriptblock]]::new()
    try { . $Fixture }
    finally {
        foreach ($after in $script:AfterQueue) {
            try { . $after } catch { script:Write-Indent "    AfterAll: $($_.Exception.Message)" 'Yellow' }
        }
        $script:AfterQueue = $outer
        $script:Depth--
    }
}

function Describe {
    param([Parameter(Position = 0)][string] $Name, [Parameter(Position = 1)][scriptblock] $Fixture)
    script:Invoke-Block -Name $Name -Fixture $Fixture
}

function Context {
    param([Parameter(Position = 0)][string] $Name, [Parameter(Position = 1)][scriptblock] $Fixture)
    script:Invoke-Block -Name $Name -Fixture $Fixture
}

# BeforeAll/BeforeEach are dot-sourced so the test file's `$script:` variables and helper
# functions land in the shared script scope the It blocks read from. AfterAll is DEFERRED to
# the end of its block instead - see script:Invoke-Block.
function BeforeAll { param([Parameter(Position = 0)][scriptblock] $Fixture) . $Fixture }
function AfterAll {
    param([Parameter(Position = 0)][scriptblock] $Fixture)
    if ($null -ne $script:AfterQueue) { $script:AfterQueue.Add($Fixture) } else { . $Fixture }
}
function BeforeEach { param([Parameter(Position = 0)][scriptblock] $Fixture) . $Fixture }

function It {
    param([Parameter(Position = 0)][string] $Name, [Parameter(Position = 1)][scriptblock] $Test)
    try {
        & $Test
        $script:Passed++
        script:Write-Indent "[+] $Name" 'Green'
    } catch {
        $script:Failed++
        $script:Failures += "$Name -- $($_.Exception.Message)"
        script:Write-Indent "[-] $Name" 'Red'
        script:Write-Indent "    $($_.Exception.Message)" 'Red'
    }
}

function Should {
    [CmdletBinding()]
    param(
        [Parameter(ValueFromPipeline = $true)] $ActualValue,
        [switch] $Not,
        $Be,
        $BeExactly,
        [switch] $BeTrue,
        [switch] $BeFalse,
        [switch] $BeNullOrEmpty,
        $Contain,
        $Match,
        [switch] $Throw
    )
    begin { $items = @() }
    process { $items += , $ActualValue }
    end {
        $actual = if ($items.Count -eq 1) { $items[0] } elseif ($items.Count -eq 0) { $null } else { $items }
        $show   = if ($null -eq $actual) { '<null>' } else { ($actual | ForEach-Object { "$_" }) -join ',' }

        $ok = $null
        $what = ''
        if ($PSBoundParameters.ContainsKey('Be')) {
            $ok = if ($actual -is [array] -or $Be -is [array]) {
                      ((@($actual) -join "`u{1}") -eq (@($Be) -join "`u{1}"))
                  } else { $actual -eq $Be }
            $what = "be '$Be'"
        }
        elseif ($PSBoundParameters.ContainsKey('BeExactly')) {
            $ok = $actual -ceq $BeExactly; $what = "be exactly '$BeExactly'"
        }
        elseif ($BeTrue)        { $ok = [bool] $actual;  $what = 'be true' }
        elseif ($BeFalse)       { $ok = -not [bool] $actual; $what = 'be false' }
        elseif ($BeNullOrEmpty) { $ok = [string]::IsNullOrEmpty([string] $actual) -or @($actual).Count -eq 0
                                  $what = 'be null or empty' }
        elseif ($PSBoundParameters.ContainsKey('Contain')) {
            $ok = @($items) -contains $Contain; $what = "contain '$Contain'"
        }
        elseif ($PSBoundParameters.ContainsKey('Match')) {
            $ok = "$actual" -match $Match; $what = "match '$Match'"
        }
        elseif ($Throw) {
            if ($actual -isnot [scriptblock]) { throw 'Should -Throw needs a scriptblock' }
            $threw = $false
            try { & $actual | Out-Null } catch { $threw = $true }
            $ok = $threw; $what = 'throw'; $show = '<scriptblock>'
        }
        else { throw 'Should shim: unsupported operator (extend RunTests.ps1)' }

        if ($Not) { $ok = -not $ok; $what = "not $what" }
        if (-not $ok) { throw "expected '$show' to $what" }
    }
}

foreach ($f in $TestFiles) {
    Write-Host ''
    Write-Host $f.Name -ForegroundColor Yellow
    . $f.FullName
}

Write-Host ''
Write-Host ("Passed: {0}  Failed: {1}" -f $script:Passed, $script:Failed) `
           -ForegroundColor $(if ($script:Failed -gt 0) { 'Red' } else { 'Green' })
foreach ($fail in $script:Failures) { Write-Host "  $fail" -ForegroundColor Red }
exit ([int]($script:Failed -gt 0))

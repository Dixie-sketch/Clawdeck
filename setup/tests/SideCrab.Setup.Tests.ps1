#Requires -Version 7.0
<#
    Tests for the SideCrab setup scripts.

    NOTHING HERE INSTALLS ANYTHING. The scripts are never executed: they are parsed,
    and the pure decision helpers are lifted out of SideCrab.Common.ps1 by AST and
    re-defined in this session. No scheduled task is created, started or stopped, and
    the real ~/.claude/settings.json is never opened - hook cases use in-memory
    objects and temp files only.

    Run with:  pwsh -File setup\tests\RunTests.ps1
    (Pester 5 if installed, otherwise the plain-PowerShell shim in RunTests.ps1.)
#>

Describe 'SideCrab setup' {

    BeforeAll {
        $script:TestsDir = $PSScriptRoot
        $script:SetupDir = Split-Path -Parent $script:TestsDir
        $script:RepoRoot = Split-Path -Parent $script:SetupDir
        $script:Common   = Join-Path $script:SetupDir 'SideCrab.Common.ps1'

        $script:ScriptFiles = @(
            Get-ChildItem -LiteralPath $script:SetupDir -Filter '*.ps1' -File
            Get-ChildItem -LiteralPath $script:TestsDir -Filter '*.ps1' -File
        ) | Sort-Object FullName

        function script:Get-ScriptAst {
            param([string] $Path)
            $errors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseFile(
                       $Path, [ref] $null, [ref] $errors)
            [pscustomobject]@{ Ast = $ast; Errors = @($errors) }
        }

        # AST lift: take a function's BODY text out of the file and bind it to a
        # function of the same name here. Only the body is evaluated, so a script's
        # top-level code (the part that installs things) is never reached.
        function script:Import-AstFunction {
            param([string] $Path, [string[]] $Name)
            $parsed = script:Get-ScriptAst -Path $Path
            if ($parsed.Errors.Count -gt 0) { throw "parse errors in $Path" }
            foreach ($n in $Name) {
                $def = $parsed.Ast.FindAll({
                            param($node)
                            $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
                       }, $true) | Where-Object { $_.Name -eq $n } | Select-Object -First 1
                if (-not $def) { throw "function $n not found in $Path" }
                $text = $def.Body.Extent.Text          # includes the outer { }
                $body = $text.Substring(1, $text.Length - 2)
                Set-Item -Path "function:global:$n" -Value ([scriptblock]::Create($body)) -Force
            }
        }

        script:Import-AstFunction -Path $script:Common -Name @(
            'Get-SideCrabComponentSpec', 'Select-SideCrabComponent',
            'Get-SideCrabTaskName',      'Get-SideCrabHookEvent',
            'Get-SideCrabAumidSpec',     'Get-SideCrabProtocolSpec',
            'Get-SideCrabProtocolCommand',
            'Get-SideCrabStatusLineSpec', 'Get-SideCrabStatusLineCommand',
            'Test-SideCrabStatusLineIsOurs'
        )

        $script:FakeRoot = 'C:\Fake\sidecrab'
        $script:Spec     = Get-SideCrabComponentSpec -RepoRoot $script:FakeRoot

        function script:Plan {
            param([hashtable] $Present = @{}, [hashtable] $Requested = @{})
            @(Select-SideCrabComponent -Spec $script:Spec -Present $Present -Requested $Requested)
        }
        function script:One {
            param([object[]] $Plan, [string] $Key)
            $Plan | Where-Object { $_.Key -eq $Key } | Select-Object -First 1
        }
    }

    Context 'parse checks' {

        It 'every setup script parses with no errors' {
            $bad = @()
            foreach ($f in $script:ScriptFiles) {
                $parsed = script:Get-ScriptAst -Path $f.FullName
                if ($parsed.Errors.Count -gt 0) {
                    $bad += "$($f.Name): $($parsed.Errors[0].Message)"
                }
            }
            ($bad -join ' | ') | Should -Be ''
        }

        It 'every setup script tokenizes with no errors (PSParser)' {
            $bad = @()
            foreach ($f in $script:ScriptFiles) {
                $errors = $null
                $text = Get-Content -LiteralPath $f.FullName -Raw -Encoding utf8
                [void] [System.Management.Automation.PSParser]::Tokenize($text, [ref] $errors)
                if (@($errors).Count -gt 0) { $bad += "$($f.Name): $(@($errors)[0].Message)" }
            }
            ($bad -join ' | ') | Should -Be ''
        }

        It 'the ten setup scripts all exist' {
            foreach ($n in 'SideCrab.Common.ps1', 'Install-SideCrab.ps1',
                           'Uninstall-SideCrab.ps1', 'Update-SideCrab.ps1',
                           'Register-SideCrabAumid.ps1', 'Register-SideCrabProtocol.ps1',
                           'Test-SideCrab.ps1', 'Verify-PanelApproval.ps1',
                           'Restore-SideCrab.ps1', 'Repair-SideCrab.ps1') {
                (Test-Path -LiteralPath (Join-Path $script:SetupDir $n)) | Should -BeTrue
            }
        }
    }

    Context 'component catalogue' {

        It 'names the three components' {
            (($script:Spec | ForEach-Object { $_.Key }) -join ',') | Should -Be 'crabd,glow,toast'
        }

        It 'maps each component to its SideCrab-* task name' {
            (script:One $script:Spec 'crabd').TaskName | Should -Be 'SideCrab-crabd'
            (script:One $script:Spec 'glow').TaskName  | Should -Be 'SideCrab-glow'
            (script:One $script:Spec 'toast').TaskName | Should -Be 'SideCrab-toast'
        }

        It 'roots every script path under the supplied repo root' {
            (script:One $script:Spec 'crabd').Script | Should -Be 'C:\Fake\sidecrab\companion\crabd.py'
            (script:One $script:Spec 'glow').Script  | Should -Be 'C:\Fake\sidecrab\lighting\glow_launcher.pyw'
            (script:One $script:Spec 'toast').Script | Should -Be 'C:\Fake\sidecrab\notifier\sidecrab_toast.py'
        }

        It 'marks crabd required and the others optional' {
            (script:One $script:Spec 'crabd').Required | Should -BeTrue
            (script:One $script:Spec 'glow').Required  | Should -BeFalse
            (script:One $script:Spec 'toast').Required | Should -BeFalse
        }
    }

    Context 'component detection (no switches)' {

        It 'installs crabd alone when neither optional script exists' {
            $plan = script:Plan -Present @{ crabd = $true }
            (script:One $plan 'crabd').Selected | Should -BeTrue
            (script:One $plan 'glow').Selected  | Should -BeFalse
            (script:One $plan 'toast').Selected | Should -BeFalse
            (script:One $plan 'glow').Reason    | Should -Be 'not-installed'
        }

        It 'auto-includes glow when only its script is present' {
            $plan = script:Plan -Present @{ crabd = $true; glow = $true }
            (script:One $plan 'glow').Selected  | Should -BeTrue
            (script:One $plan 'glow').Reason    | Should -Be 'auto-detected'
            (script:One $plan 'toast').Selected | Should -BeFalse
        }

        It 'auto-includes toast independently of glow' {
            $plan = script:Plan -Present @{ crabd = $true; toast = $true }
            (script:One $plan 'toast').Selected | Should -BeTrue
            (script:One $plan 'toast').Reason   | Should -Be 'auto-detected'
            (script:One $plan 'glow').Selected  | Should -BeFalse
        }

        It 'auto-includes both when both scripts are present' {
            $plan = script:Plan -Present @{ crabd = $true; glow = $true; toast = $true }
            (@($plan | Where-Object Selected).Count) | Should -Be 3
        }

        It 'reports a problem when crabd itself is missing' {
            $plan = script:Plan -Present @{}
            (script:One $plan 'crabd').Selected | Should -BeTrue
            (script:One $plan 'crabd').Problem  | Should -Match 'crabd script not found'
        }

        It 'raises no problem for an absent optional component' {
            $plan = script:Plan -Present @{ crabd = $true }
            (script:One $plan 'glow').Problem  | Should -BeNullOrEmpty
            (script:One $plan 'toast').Problem | Should -BeNullOrEmpty
        }
    }

    Context 'component detection (switches)' {

        It 'selects glow on -WithGlow when the script is present' {
            $plan = script:Plan -Present @{ crabd = $true; glow = $true } -Requested @{ glow = $true }
            (script:One $plan 'glow').Selected | Should -BeTrue
            (script:One $plan 'glow').Reason   | Should -Be 'requested'
            (script:One $plan 'glow').Problem  | Should -BeNullOrEmpty
        }

        It 'turns -WithGlow with a missing script into a problem, not a silent skip' {
            $plan = script:Plan -Present @{ crabd = $true } -Requested @{ glow = $true }
            (script:One $plan 'glow').Selected | Should -BeTrue
            (script:One $plan 'glow').Problem  | Should -Match '-WithGlow'
            (script:One $plan 'glow').Problem  | Should -Match 'glow_launcher.pyw'
        }

        It 'turns -WithToast with a missing script into a problem naming that switch' {
            $plan = script:Plan -Present @{ crabd = $true } -Requested @{ toast = $true }
            (script:One $plan 'toast').Problem | Should -Match '-WithToast'
        }

        It 'does not let one switch select the other component' {
            $plan = script:Plan -Present @{ crabd = $true; glow = $true; toast = $true } `
                                -Requested @{ glow = $true }
            (script:One $plan 'glow').Reason  | Should -Be 'requested'
            (script:One $plan 'toast').Reason | Should -Be 'auto-detected'
        }

        It 'always selects crabd regardless of switches' {
            foreach ($req in @(@{}, @{ glow = $true }, @{ glow = $true; toast = $true })) {
                (script:One (script:Plan -Present @{ crabd = $true } -Requested $req) 'crabd').Selected |
                    Should -BeTrue
            }
        }

        It 'treats a false switch value as not requested' {
            $plan = script:Plan -Present @{ crabd = $true } -Requested @{ glow = $false }
            (script:One $plan 'glow').Selected | Should -BeFalse
            (script:One $plan 'glow').Problem  | Should -BeNullOrEmpty
        }
    }

    Context 'task-name assembly' {

        It 'returns only the selected task names, in catalogue order' {
            $plan  = script:Plan -Present @{ crabd = $true; toast = $true }
            $names = Get-SideCrabTaskName -Component $plan
            ($names -join ',') | Should -Be 'SideCrab-crabd,SideCrab-toast'
        }

        It 'returns every known task name with -All' {
            $names = Get-SideCrabTaskName -Component $script:Spec -All
            ($names -join ',') | Should -Be 'SideCrab-crabd,SideCrab-glow,SideCrab-toast'
        }

        It 'returns crabd alone for a bare install' {
            $names = Get-SideCrabTaskName -Component (script:Plan -Present @{ crabd = $true })
            ($names -join ',') | Should -Be 'SideCrab-crabd'
        }

        It 'returns an empty list, not an error, for an empty component set' {
            @(Get-SideCrabTaskName -Component @() -All).Count | Should -Be 0
        }

        It 'emits names unrolled, not nested one array deep' {
            # The trap this pins: a `, @(...)` return makes @(f).Count 1 forever.
            $names = @(Get-SideCrabTaskName -Component $script:Spec -All)
            $names.Count      | Should -Be 3
            $names[0]         | Should -Be 'SideCrab-crabd'
            ($names[0] -is [string]) | Should -BeTrue
        }

        It 'every task name carries the SideCrab- prefix the uninstall sweep matches' {
            foreach ($n in (Get-SideCrabTaskName -Component $script:Spec -All)) {
                ($n -like 'SideCrab-*') | Should -BeTrue
            }
        }
    }

    Context 'hook detection' {

        BeforeAll {
            $script:Marker = '127.0.0.1:9999/v1/hook'
            $script:OurHook = @{
                matcher = '*'
                hooks   = @(@{ type = 'command'
                               command = "curl.exe -s -m 2 -X POST -H `"X-SideCrab-Panel: 1`" --data-binary @- http://$($script:Marker) || exit 0" })
            }
            $script:ForeignHook = @{
                matcher = '*'
                hooks   = @(@{ type = 'command'; command = 'echo not ours' })
            }
        }

        It 'finds the events that carry a SideCrab entry' {
            $settings = @{ hooks = @{ Stop = @($script:OurHook); SessionStart = @($script:OurHook) } }
            $events = @(Get-SideCrabHookEvent -Settings $settings -Marker $script:Marker)
            $events.Count | Should -Be 2
            ($events | ForEach-Object { $_.Event }) | Should -Contain 'Stop'
            ($events | ForEach-Object { $_.Event }) | Should -Contain 'SessionStart'
        }

        It 'counts multiple SideCrab entries on one event' {
            $settings = @{ hooks = @{ Stop = @($script:OurHook, $script:ForeignHook, $script:OurHook) } }
            (@(Get-SideCrabHookEvent -Settings $settings -Marker $script:Marker)[0]).Count | Should -Be 2
        }

        It 'ignores hooks that are not ours' {
            $settings = @{ hooks = @{ Stop = @($script:ForeignHook) } }
            @(Get-SideCrabHookEvent -Settings $settings -Marker $script:Marker).Count | Should -Be 0
        }

        It 'returns empty for settings with no hooks key, and for null' {
            @(Get-SideCrabHookEvent -Settings @{ model = 'opus' }).Count | Should -Be 0
            @(Get-SideCrabHookEvent -Settings $null).Count                | Should -Be 0
        }

        It 'survives a hooks value of the wrong shape' {
            @(Get-SideCrabHookEvent -Settings @{ hooks = 'nonsense' }).Count | Should -Be 0
            @(Get-SideCrabHookEvent -Settings @{ hooks = @{ Stop = 'nonsense' } }).Count | Should -Be 0
        }

        It 'finds every event in the shipped hook fragment (read-only, via a temp copy)' {
            $fragment = Join-Path $script:RepoRoot 'hooks\settings-hooks-fragment.json'
            if (-not (Test-Path -LiteralPath $fragment)) { throw "hook fragment missing at $fragment" }
            $temp = Join-Path ([IO.Path]::GetTempPath()) ("sidecrab-frag-{0}.json" -f [guid]::NewGuid())
            Copy-Item -LiteralPath $fragment -Destination $temp -Force
            try {
                $settings = Get-Content -LiteralPath $temp -Raw -Encoding utf8 |
                            ConvertFrom-Json -AsHashtable -Depth 40
                $events = @(Get-SideCrabHookEvent -Settings $settings -Marker $script:Marker)
                $events.Count | Should -Be @($settings['hooks'].Keys).Count
                ($events | ForEach-Object { $_.Event }) | Should -Contain 'SessionStart'
            } finally {
                Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Context 'script contracts' {

        BeforeAll {
            $script:InstallAst   = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast
            $script:UninstallAst = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1')).Ast
            $script:UpdateAst    = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Update-SideCrab.ps1')).Ast

            function script:Get-ParamName {
                param($Ast)
                @($Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            }
            function script:Test-SupportsShouldProcess {
                param($Ast)
                $text = "$($Ast.ParamBlock.Attributes.Extent.Text)"
                $text -match 'SupportsShouldProcess'
            }
        }

        It 'Install exposes -WithGlow, -WithToast and -Status' {
            $p = script:Get-ParamName $script:InstallAst
            $p | Should -Contain 'WithGlow'
            $p | Should -Contain 'WithToast'
            $p | Should -Contain 'Status'
        }

        It 'Install keeps its existing switches' {
            $p = script:Get-ParamName $script:InstallAst
            $p | Should -Contain 'SkipTask'
            $p | Should -Contain 'SkipHooks'
            $p | Should -Contain 'SettingsPath'
        }

        It 'all three scripts support ShouldProcess' {
            (script:Test-SupportsShouldProcess $script:InstallAst)   | Should -BeTrue
            (script:Test-SupportsShouldProcess $script:UninstallAst) | Should -BeTrue
            (script:Test-SupportsShouldProcess $script:UpdateAst)    | Should -BeTrue
        }

        It 'all three scripts dot-source the shared helpers' {
            foreach ($f in 'Install-SideCrab.ps1', 'Uninstall-SideCrab.ps1', 'Update-SideCrab.ps1') {
                $text = Get-Content -LiteralPath (Join-Path $script:SetupDir $f) -Raw -Encoding utf8
                ($text -match [regex]::Escape("SideCrab.Common.ps1")) | Should -BeTrue
            }
        }

        It 'Update pulls fast-forward only and warns about the iCUE-only widget path' {
            $text = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Update-SideCrab.ps1') -Raw -Encoding utf8
            ($text -match '--ff-only')       | Should -BeTrue
            ($text -match 'iCUE')            | Should -BeTrue
            ($text -match 'Write-Warning')   | Should -BeTrue
        }

        It 'every diagnostic that POSTs to crabd sends the panel header' {
            # crabd 0.31.0 and later answers a POST without X-SideCrab-Panel with 403. These
            # three POST on their own account, and each reads a non-answer as evidence about
            # crabd rather than about itself: without the header the smoke test reports the
            # hook ingest broken and both permission probes report the route unreachable, on
            # a perfectly healthy host. Source text, because the POST cannot be run here.
            foreach ($f in 'Test-SideCrab.ps1', 'Repair-SideCrab.ps1', 'Verify-PanelApproval.ps1') {
                $text = Get-Content -LiteralPath (Join-Path $script:SetupDir $f) -Raw -Encoding utf8
                ($text -match [regex]::Escape('X-SideCrab-Panel')) | Should -BeTrue
            }
        }

        It 'the hooks README still documents the curl traps' {
            $readme = Join-Path $script:RepoRoot 'hooks\README.md'
            $text = Get-Content -LiteralPath $readme -Raw -Encoding utf8
            ($text -match 'curl\.exe')     | Should -BeTrue
            ($text -match '-m 2')          | Should -BeTrue
            ($text -match 'exit 0')        | Should -BeTrue
            ($text -match '--data-binary') | Should -BeTrue
        }

        It 'the hooks README documents the PermissionRequest shape the binary actually accepts' {
            # claude.exe v2.1.246's zod schema: hookSpecificOutput.decision.{behavior: allow|deny}.
            # There is no "ask", and pass-through is omitting hookSpecificOutput entirely. The
            # PreToolUse-style `permissionDecision` string is a DIFFERENT hook's shape, and a
            # docs-based reading of it misled this repo once - it may appear here only as the
            # correction, never as the contract.
            $text = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'hooks\README.md') -Raw -Encoding utf8
            ($text -match 'hookSpecificOutput')                  | Should -BeTrue
            ($text -match '"hookEventName":"PermissionRequest"') | Should -BeTrue
            ($text -match '"behavior":"allow"\|"deny"')          | Should -BeTrue
            foreach ($line in @($text -split "`n" | Where-Object { $_ -match 'permissionDecision' })) {
                ($line -match 'NOT|corrected|earlier') | Should -BeTrue
            }
        }
    }

    Context 'toast app identity' {

        BeforeAll {
            # Self-contained: this context reads its own ASTs and text rather than depending
            # on another context's BeforeAll having run first.
            $script:AumidSpec    = Get-SideCrabAumidSpec -RepoRoot $script:FakeRoot
            $script:CommonText   = Get-Content -LiteralPath $script:Common -Raw -Encoding utf8
            $script:InstallText  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1') -Raw -Encoding utf8
            $script:UninstText   = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') -Raw -Encoding utf8
            $script:RegisterPath = Join-Path $script:SetupDir 'Register-SideCrabAumid.ps1'
            $script:RegisterAst  = (script:Get-ScriptAst -Path $script:RegisterPath).Ast

            function script:Get-Params {
                param($Ast)
                @($Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            }
            function script:Find-Function {
                param($Ast, [string] $Name)
                $Ast.FindAll({
                    param($node)
                    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
                }, $true) | Where-Object { $_.Name -eq $Name } | Select-Object -First 1
            }
        }

        It 'names the AUMID SideCrab.Notifier' {
            $script:AumidSpec.Aumid | Should -Be 'SideCrab.Notifier'
        }

        It 'registers under HKCU only - never HKLM, which would need elevation' {
            $script:AumidSpec.RegistryPath |
                Should -Be 'HKCU:\SOFTWARE\Classes\AppUserModelId\SideCrab.Notifier'
            # Asserted over string LITERALS, not the file text: the comments explain why HKLM
            # is not used, and a text match would fail on the explanation instead of on a path.
            $hklm = @((script:Get-ScriptAst -Path $script:Common).Ast.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.StringConstantExpressionAst]
            }, $true) | Where-Object { $_.Value -match '^HKLM' })
            $hklm.Count | Should -Be 0
        }

        It 'keys the registry path on the AUMID itself' {
            # A key name that differs from the AUMID string is registered but never matched.
            ($script:AumidSpec.RegistryPath -like "*\$($script:AumidSpec.Aumid)") | Should -BeTrue
        }

        It 'points IconUri at the generated ico under the supplied repo root' {
            $script:AumidSpec.IconUri | Should -Be 'C:\Fake\sidecrab\notifier\sidecrab.ico'
        }

        It 'ties the identity to a real catalogue component' {
            $script:AumidSpec.ComponentKey | Should -Be 'toast'
            (($script:Spec | ForEach-Object { $_.Key }) -contains $script:AumidSpec.ComponentKey) |
                Should -BeTrue
        }

        It 'ships the icon the spec points at' {
            (Test-Path -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab.ico')) | Should -BeTrue
        }

        It 'agrees with the notifier on the AUMID string and the registry subkey' {
            # Cross-component contract: CreateToastNotifier matches the registered AUMID verbatim,
            # so a rename on one side alone sends every toast back to the borrowed identity.
            $toast = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_toast.py') `
                                 -Raw -Encoding utf8
            ($toast -match 'SIDECRAB_AUMID\s*=\s*"SideCrab\.Notifier"') | Should -BeTrue
            ($toast -match 'AUMID_REGISTRY_SUBKEY\s*=\s*r"SOFTWARE\\Classes\\AppUserModelId\\SideCrab\.Notifier"') |
                Should -BeTrue
        }

        It 'gates both registry writers behind ShouldProcess' {
            $commonAst = (script:Get-ScriptAst -Path $script:Common).Ast
            foreach ($name in 'Set-SideCrabAumid', 'Remove-SideCrabAumid') {
                $fn = script:Find-Function $commonAst $name
                ($null -ne $fn)         | Should -BeTrue
                $fn.Extent.Text | Should -Match 'SupportsShouldProcess'
                $fn.Extent.Text | Should -Match 'ShouldProcess\('
            }
        }

        It 'reads the current values before writing - the idempotence claim' {
            $fn = script:Find-Function (script:Get-ScriptAst -Path $script:Common).Ast 'Set-SideCrabAumid'
            $fn.Extent.Text | Should -Match 'Get-SideCrabAumidState'
            $fn.Extent.Text | Should -Match 'unchanged'
        }

        It 'removes only our key, never the shared parent' {
            # The AppUserModelId key above ours is shared with every other app on the machine,
            # so the delete has to be bound to the spec's full path and nothing else.
            $fn = script:Find-Function (script:Get-ScriptAst -Path $script:Common).Ast 'Remove-SideCrabAumid'
            $fn.Extent.Text | Should -Match 'Remove-Item -LiteralPath \$spec\.RegistryPath'
        }

        It 'exposes -Remove and -Status on the registration script, and ShouldProcess' {
            $p = script:Get-Params $script:RegisterAst
            $p | Should -Contain 'Remove'
            $p | Should -Contain 'Status'
            $p | Should -Contain 'RepoRoot'
            "$($script:RegisterAst.ParamBlock.Attributes.Extent.Text)" | Should -Match 'SupportsShouldProcess'
        }

        It 'registers the identity WITH the toast component, not unconditionally' {
            $installAst = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast
            $gated = @($installAst.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.IfStatementAst]
            }, $true) | Where-Object {
                $_.Clauses[0].Item1.Extent.Text -match "Key\s+-eq\s+'toast'" -and
                $_.Clauses[0].Item2.Extent.Text -match 'Set-SideCrabAumid'
            })
            $gated.Count | Should -Be 1
        }

        It 'removes the identity on uninstall, and offers -KeepAumid' {
            ($script:UninstText -match 'Remove-SideCrabAumid') | Should -BeTrue
            $uninstAst = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1')).Ast
            (script:Get-Params $uninstAst) | Should -Contain 'KeepAumid'
        }

        It 'reports a stale identity on update without re-writing it' {
            $updateText = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Update-SideCrab.ps1') -Raw -Encoding utf8
            ($updateText -match 'Get-SideCrabAumidState') | Should -BeTrue
            ($updateText -match 'Set-SideCrabAumid')      | Should -BeFalse
            ($updateText -match 'Test-SideCrab\.ps1')     | Should -BeTrue
        }

        It 'reports the identity in -Status without writing it' {
            # Show-Status is the read-only path; a writer reached from there would make
            # `-Status` a change.
            $fn = script:Find-Function (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast 'Show-Status'
            $fn.Extent.Text | Should -Match 'Get-SideCrabAumidState'
            ($fn.Extent.Text -match 'Set-SideCrabAumid|New-ItemProperty|New-Item ') | Should -BeFalse
        }
    }

    Context 'toast action protocol' {

        BeforeAll {
            # Self-contained, like the AUMID context: this reads its own ASTs and text.
            # The spec is a TABLE now (one row per scheme); $ProtoSpec stays the ack row so the
            # rows below keep reading as "the Acknowledge scheme still looks like this".
            $script:ProtoSpecs    = @(Get-SideCrabProtocolSpec -RepoRoot $script:FakeRoot)
            $script:ProtoSpec     = @($script:ProtoSpecs | Where-Object { $_.Key -eq 'ack' })[0]
            $script:SnoozeSpec    = @($script:ProtoSpecs | Where-Object { $_.Key -eq 'snooze' })[0]
            $script:CommonAst2    = (script:Get-ScriptAst -Path $script:Common).Ast
            $script:ProtoRegPath  = Join-Path $script:SetupDir 'Register-SideCrabProtocol.ps1'
            $script:ProtoRegAst   = (script:Get-ScriptAst -Path $script:ProtoRegPath).Ast
            $script:InstallText2  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1') -Raw -Encoding utf8
            $script:UninstText2   = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') -Raw -Encoding utf8

            function script:Get-Params2 {
                param($Ast)
                @($Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            }
            function script:Find-Function2 {
                param($Ast, [string] $Name)
                $Ast.FindAll({
                    param($node)
                    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
                }, $true) | Where-Object { $_.Name -eq $Name } | Select-Object -First 1
            }
        }

        It 'names the scheme sidecrab-ack' {
            $script:ProtoSpec.Scheme | Should -Be 'sidecrab-ack'
        }

        It 'returns BOTH toast schemes, Acknowledge first' {
            # Order is the toast's button order: the leftmost button is the one hit from a lock
            # screen by someone barely reading, and that one is the answer, not the deferral.
            ($script:ProtoSpecs | ForEach-Object { $_.Scheme }) -join ',' |
                Should -Be 'sidecrab-ack,sidecrab-snooze'
            ($script:ProtoSpecs | ForEach-Object { $_.Key }) -join ',' | Should -Be 'ack,snooze'
        }

        It 'gives the snooze scheme its own key, description and handler' {
            $script:SnoozeSpec.RegistryPath | Should -Be 'HKCU:\SOFTWARE\Classes\sidecrab-snooze'
            $script:SnoozeSpec.CommandPath  | Should -Be 'HKCU:\SOFTWARE\Classes\sidecrab-snooze\shell\open\command'
            $script:SnoozeSpec.Description  | Should -Be 'URL:SideCrab Snooze'
            $script:SnoozeSpec.Handler      | Should -Be 'C:\Fake\sidecrab\notifier\sidecrab_snooze_handler.pyw'
            $script:SnoozeSpec.Button       | Should -Be 'Snooze 30m'
        }

        It 'ships the snooze handler the spec points at' {
            # Registering a scheme at a handler that is not there is a shell error dialog on
            # every press - the one outcome worse than the inert button this replaces.
            (Test-Path -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_snooze_handler.pyw')) |
                Should -BeTrue
        }

        It 'agrees with the notifier and the snooze handler on the snooze scheme string' {
            # Three files, one string - same contract as the ack scheme below: the notifier
            # writes the URI, this registers it, the handler parses it.
            $handler = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_snooze_handler.pyw') `
                                   -Raw -Encoding utf8
            $toast   = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_toast.py') `
                                   -Raw -Encoding utf8
            ($handler -match 'SNOOZE_SCHEME\s*=\s*"sidecrab-snooze"') | Should -BeTrue
            ($toast   -match 'SNOOZE_SCHEME\s*=\s*"sidecrab-snooze"') | Should -BeTrue
        }

        It 'ties BOTH schemes to the toast component, so an install without toast leaves neither' {
            foreach ($s in $script:ProtoSpecs) {
                $s.ComponentKey | Should -Be 'toast'
            }
        }

        It 'gives every scheme a distinct key path - two schemes cannot share one class key' {
            @(@($script:ProtoSpecs | ForEach-Object { $_.RegistryPath }) | Sort-Object -Unique).Count |
                Should -Be @($script:ProtoSpecs).Count
            foreach ($s in $script:ProtoSpecs) {
                ($s.RegistryPath -like "*\$($s.Scheme)") | Should -BeTrue
                ($s.CommandPath -like "$($s.RegistryPath)\shell\open\command") | Should -BeTrue
            }
        }

        It 'registers under HKCU only - HKLM would need elevation for a per-user notifier' {
            $script:ProtoSpec.RegistryPath | Should -Be 'HKCU:\SOFTWARE\Classes\sidecrab-ack'
            $script:ProtoSpec.CommandPath  | Should -Be 'HKCU:\SOFTWARE\Classes\sidecrab-ack\shell\open\command'
        }

        It 'keys the class path on the scheme itself' {
            # A class key whose name differs from the scheme is registered and never routed.
            ($script:ProtoSpec.RegistryPath -like "*\$($script:ProtoSpec.Scheme)") | Should -BeTrue
            ($script:ProtoSpec.CommandPath -like "$($script:ProtoSpec.RegistryPath)\shell\open\command") | Should -BeTrue
        }

        It 'points the handler at the .pyw under the supplied repo root' {
            $script:ProtoSpec.Handler | Should -Be 'C:\Fake\sidecrab\notifier\sidecrab_ack_handler.pyw'
        }

        It 'ties the scheme to a real catalogue component' {
            $script:ProtoSpec.ComponentKey | Should -Be 'toast'
            (($script:Spec | ForEach-Object { $_.Key }) -contains $script:ProtoSpec.ComponentKey) | Should -BeTrue
        }

        It 'ships the handler the spec points at' {
            (Test-Path -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_ack_handler.pyw')) |
                Should -BeTrue
        }

        It 'quotes both paths and passes the URI as "%1"' {
            # Unquoted paths break on "Program Files"; a bare %1 splits a URI on any space.
            $cmd = Get-SideCrabProtocolCommand -PythonExe 'C:\Program Files\Python313\pythonw.exe' `
                                               -HandlerPath 'C:\Dev\side crab\notifier\sidecrab_ack_handler.pyw'
            $cmd | Should -Be '"C:\Program Files\Python313\pythonw.exe" "C:\Dev\side crab\notifier\sidecrab_ack_handler.pyw" "%1"'
        }

        It 'agrees with the handler and the notifier on the scheme string' {
            # Three files, one string: the notifier writes the URI, this registers it, the
            # handler parses it. Any one of them renaming alone is a button that does nothing.
            $handler = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_ack_handler.pyw') `
                                   -Raw -Encoding utf8
            $toast   = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_toast.py') `
                                   -Raw -Encoding utf8
            ($handler -match 'ACK_SCHEME\s*=\s*"sidecrab-ack"') | Should -BeTrue
            ($toast   -match 'ACK_SCHEME\s*=\s*"sidecrab-ack"') | Should -BeTrue
        }

        It 'agrees with both python files on the session-id charset' {
            $handler = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_ack_handler.pyw') `
                                   -Raw -Encoding utf8
            $toast   = Get-Content -LiteralPath (Join-Path $script:RepoRoot 'notifier\sidecrab_toast.py') `
                                   -Raw -Encoding utf8
            foreach ($t in @($handler, $toast)) {
                ($t -match 'SESSION_ID_PATTERN\s*=\s*r"\^\[A-Za-z0-9-\]\{1,64\}\$"') | Should -BeTrue
            }
        }

        It 'gates both registry writers behind ShouldProcess' {
            foreach ($name in 'Set-SideCrabProtocol', 'Remove-SideCrabProtocol') {
                $fn = script:Find-Function2 $script:CommonAst2 $name
                ($null -ne $fn)  | Should -BeTrue
                $fn.Extent.Text  | Should -Match 'SupportsShouldProcess'
                $fn.Extent.Text  | Should -Match 'ShouldProcess\('
            }
        }

        It 'reads the current command before writing - the idempotence claim' {
            $fn = script:Find-Function2 $script:CommonAst2 'Set-SideCrabProtocol'
            $fn.Extent.Text | Should -Match 'Get-SideCrabProtocolState'
            $fn.Extent.Text | Should -Match 'unchanged'
        }

        It 'refuses to register a scheme at a handler that is not there' {
            # A registered scheme with no handler is a shell ERROR dialog on every press -
            # louder and less useful than no button at all.
            $fn = script:Find-Function2 $script:CommonAst2 'Set-SideCrabProtocol'
            $fn.Extent.Text | Should -Match 'HandlerPresent'
            $fn.Extent.Text | Should -Match 'throw'
        }

        It 'writes the URL Protocol flag Windows actually gates on' {
            $fn = script:Find-Function2 $script:CommonAst2 'Set-SideCrabProtocol'
            $fn.Extent.Text | Should -Match "'URL Protocol'"
        }

        It 'reports a command that lost its %1 rather than calling it registered' {
            # Without %1 the handler starts with no URI, logs a refusal and exits: the
            # button looks wired and acks nothing.
            $fn = script:Find-Function2 $script:CommonAst2 'Get-SideCrabProtocolState'
            $fn.Extent.Text | Should -Match 'CarriesArgument'
        }

        It 'removes only the scheme key, bound to the spec path' {
            $fn = script:Find-Function2 $script:CommonAst2 'Remove-SideCrabProtocol'
            $fn.Extent.Text | Should -Match 'Remove-Item -LiteralPath \$spec\.RegistryPath'
        }

        It 'never probes or writes anything outside HKCU' {
            $hklm = @($script:CommonAst2.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.StringConstantExpressionAst]
            }, $true) | Where-Object { $_.Value -match '^HK(LM|CR|U):' })
            $hklm.Count | Should -Be 0
        }

        It 'exposes -Remove, -Status and -RepoRoot on the registration script, and ShouldProcess' {
            $p = script:Get-Params2 $script:ProtoRegAst
            $p | Should -Contain 'Remove'
            $p | Should -Contain 'Status'
            $p | Should -Contain 'RepoRoot'
            "$($script:ProtoRegAst.ParamBlock.Attributes.Extent.Text)" | Should -Match 'SupportsShouldProcess'
        }

        It 'dot-sources the shared helpers rather than re-deriving the paths' {
            $text = Get-Content -LiteralPath $script:ProtoRegPath -Raw -Encoding utf8
            ($text -match [regex]::Escape('SideCrab.Common.ps1')) | Should -BeTrue
            ($text -match 'Set-SideCrabProtocol')                 | Should -BeTrue
            ($text -match 'Remove-SideCrabProtocol')              | Should -BeTrue
        }

        It 'registers the scheme WITH the toast component, not unconditionally' {
            $installAst = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast
            $gated = @($installAst.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.IfStatementAst]
            }, $true) | Where-Object {
                $_.Clauses[0].Item1.Extent.Text -match "Key\s+-eq\s+'toast'" -and
                $_.Clauses[0].Item2.Extent.Text -match 'Set-SideCrabProtocol'
            })
            $gated.Count | Should -Be 1
        }

        It 'removes the scheme on uninstall, and offers -KeepProtocol' {
            ($script:UninstText2 -match 'Remove-SideCrabProtocol') | Should -BeTrue
            $uninstAst = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1')).Ast
            (script:Get-Params2 $uninstAst) | Should -Contain 'KeepProtocol'
        }

        It 'keeps the scheme when a narrowed -TaskName run does not own the toast component' {
            # Was pinned on the local $ownsToast flag; that flag is now one field of the shared
            # Get-SideCrabUninstallScope table, which covers the hooks and the status line too.
            ($script:UninstText2 -match 'Get-SideCrabUninstallScope') | Should -BeTrue
            ($script:UninstText2 -match '\$scope\.Protocol')          | Should -BeTrue
            ($script:UninstText2 -match '\$scope\.Aumid')             | Should -BeTrue
        }

        It 'reports the scheme in -Status without writing it' {
            $fn = script:Find-Function2 (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast 'Show-Status'
            $fn.Extent.Text | Should -Match 'Get-SideCrabProtocolState'
            ($fn.Extent.Text -match 'Set-SideCrabProtocol|Set-ItemProperty|New-Item ') | Should -BeFalse
        }

        It 'reports a stale registration on update without re-writing it' {
            $updateText = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Update-SideCrab.ps1') -Raw -Encoding utf8
            ($updateText -match 'Get-SideCrabProtocolState') | Should -BeTrue
            ($updateText -match 'Set-SideCrabProtocol')      | Should -BeFalse
        }

        It 'the smoke test checks the registration read-only' {
            $smoke = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Test-SideCrab.ps1') -Raw -Encoding utf8
            ($smoke -match 'Get-SideCrabProtocolState') | Should -BeTrue
            ($smoke -match 'Set-SideCrabProtocol')      | Should -BeFalse
            ($smoke -match 'toast action')              | Should -BeTrue
        }

        It 'builds a quoted, %1-carrying command for EVERY scheme' {
            # Same three load-bearing quotes per scheme: an unquoted interpreter or handler
            # breaks on "Program Files", and a bare %1 splits a URI on any space.
            foreach ($s in $script:ProtoSpecs) {
                $cmd = Get-SideCrabProtocolCommand -PythonExe 'C:\Program Files\Python313\pythonw.exe' `
                                                   -HandlerPath $s.Handler
                $cmd | Should -Match '^"C:\\Program Files\\Python313\\pythonw\.exe" "'
                $cmd | Should -Match '"%1"$'
                ($cmd -match [regex]::Escape($s.Handler)) | Should -BeTrue
            }
        }

        It 'the three registry callers LOOP over the spec instead of assuming one scheme' {
            # The defect this closes: the spec returned ONE object and all three callers read
            # .Scheme straight off it, so adding a second scheme would have registered nothing
            # while every status line still said "registered".
            foreach ($name in 'Get-SideCrabProtocolState', 'Set-SideCrabProtocol', 'Remove-SideCrabProtocol') {
                $fn = script:Find-Function2 $script:CommonAst2 $name
                ($null -ne $fn) | Should -BeTrue
                $loops = @($fn.FindAll({
                    param($n) $n -is [System.Management.Automation.Language.ForEachStatementAst]
                }, $true))
                ($loops.Count -ge 1) | Should -BeTrue
                # and what it loops over is the spec table, not a list of its own
                ($fn.Extent.Text -match 'Get-SideCrabProtocolSpec') | Should -BeTrue
            }
        }

        It 'a missing handler skips ONLY that scheme, and throws only when none is present' {
            # Registering a scheme at an absent handler is a shell error dialog on every press.
            # Aborting the whole toast install over it would be worse still: the ack button has
            # shipped working for waves, and a missing snooze handler is a gap, not a fault.
            $fn = script:Find-Function2 $script:CommonAst2 'Set-SideCrabProtocol'
            $fn.Extent.Text | Should -Match 'handler-missing'
            $fn.Extent.Text | Should -Match 'Write-Warning'
            $fn.Extent.Text | Should -Match '\$present\.Count -eq 0'
            $fn.Extent.Text | Should -Match 'throw'
        }

        It 'every consumer of the protocol state loops it - install, uninstall, update, smoke, doctor, register' {
            # A consumer that keeps reading .Scheme off the return value now silently reports the
            # FIRST scheme's state as if it covered both. Each of these was converted.
            $loop = 'foreach \(\$\w+ in @\((Get-SideCrabProtocolState|Set-SideCrabProtocol|Remove-SideCrabProtocol)'
            foreach ($f in 'Install-SideCrab.ps1', 'Uninstall-SideCrab.ps1', 'Update-SideCrab.ps1',
                           'Test-SideCrab.ps1', 'Register-SideCrabProtocol.ps1') {
                $text = Get-Content -LiteralPath (Join-Path $script:SetupDir $f) -Raw -Encoding utf8
                ($text -match $loop) | Should -BeTrue
            }
            # The doctor reads the states once and loops the variable (it needs them twice).
            $repair = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1') -Raw -Encoding utf8
            ($repair -match '\$protoStates = @\(Get-SideCrabProtocolState') | Should -BeTrue
            ($repair -match 'foreach \(\$ps in \$protoStates\)')            | Should -BeTrue
        }

        It 'the installer registers the schemes with the toast component and uninstall removes them all' {
            # -KeepProtocol is still the only way to leave them behind, and it holds BOTH.
            ($script:InstallText2 -match 'foreach \(\$proto in @\(Set-SideCrabProtocol') | Should -BeTrue
            ($script:UninstText2  -match 'foreach \(\$result in @\(Remove-SideCrabProtocol') | Should -BeTrue
            $uninstAst = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1')).Ast
            (script:Get-Params2 $uninstAst) | Should -Contain 'KeepProtocol'
        }

        It 'the doctor can fix ONE scheme without re-writing the other' {
            # -Scheme is what makes the per-row fix honest: the row said sidecrab-snooze:, so the
            # fix registers sidecrab-snooze:.
            $fn = script:Find-Function2 $script:CommonAst2 'Set-SideCrabProtocol'
            $fn.Extent.Text | Should -Match '\[string\[\]\] \$Scheme'
            $repair = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1') -Raw -Encoding utf8
            ($repair -match 'Set-SideCrabProtocol -RepoRoot .+ -Scheme') | Should -BeTrue
        }
    }

    Context 'smoke test contracts' {

        BeforeAll {
            $script:SmokePath = Join-Path $script:SetupDir 'Test-SideCrab.ps1'
            $script:SmokeAst  = (script:Get-ScriptAst -Path $script:SmokePath).Ast
            $script:SmokeText = Get-Content -LiteralPath $script:SmokePath -Raw -Encoding utf8
        }

        It 'exposes the knobs a run needs' {
            $p = @($script:SmokeAst.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            foreach ($n in 'RepoRoot', 'BaseUri', 'ConfigPath', 'SessionId', 'SkipHookCycle') {
                $p | Should -Contain $n
            }
        }

        It 'checks all three components rather than a hardcoded task list' {
            ($script:SmokeText -match 'Get-SideCrabComponentSpec') | Should -BeTrue
            ($script:SmokeText -match 'Get-SideCrabTaskState')     | Should -BeTrue
            ($script:SmokeText -match "'Running'")                 | Should -BeTrue
        }

        It 'checks health, schema, freshness, config and the toast identity' {
            ($script:SmokeText -match '/v1/health')            | Should -BeTrue
            ($script:SmokeText -match '/v1/state')             | Should -BeTrue
            ($script:SmokeText -match 'generatedAt')           | Should -BeTrue
            ($script:SmokeText -match 'Get-SideCrabAumidState')| Should -BeTrue
            ($script:SmokeText -match 'config\.json|ConfigPath') | Should -BeTrue
        }

        It 'uses the contract''s own 30s staleness limit' {
            # Same number the widget calls stale at; a laxer one here would pass a feed the
            # widget is already showing as dead.
            ($script:SmokeText -match '\$MaxLagSec\s*=\s*30') | Should -BeTrue
        }

        It 'posts a full SessionStart -> Notification -> SessionEnd cycle' {
            foreach ($e in 'SessionStart', 'Notification', 'SessionEnd') {
                ($script:SmokeText -match "'$e'") | Should -BeTrue
            }
            ($script:SmokeText -match '/v1/hook') | Should -BeTrue
        }

        It 'posts SessionEnd from a finally block so a failed run cannot strand a session' {
            $finallies = @($script:SmokeAst.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.TryStatementAst]
            }, $true) | Where-Object { $_.Finally -and $_.Finally.Extent.Text -match "SessionEnd" })
            $finallies.Count | Should -Be 1
        }

        It 'never touches a scheduled task - it is a test, not an installer' {
            foreach ($verb in 'Register-ScheduledTask', 'Unregister-ScheduledTask',
                              'Start-ScheduledTask', 'Stop-ScheduledTask') {
                ($script:SmokeText -match [regex]::Escape($verb)) | Should -BeFalse
            }
        }

        It 'never writes the registry, settings.json or config.json' {
            foreach ($writer in 'Set-SideCrabAumid', 'New-ItemProperty', 'Set-ItemProperty',
                                'Set-Content', 'Out-File', 'Remove-Item') {
                ($script:SmokeText -match [regex]::Escape($writer)) | Should -BeFalse
            }
        }

        It 'checks the notifier still accepts the schema crabd serves' {
            # The failure this exists for: crabd moved to schema 4, the notifier's
            # SUPPORTED_SCHEMAS did not, and it stopped toasting while its task stayed
            # Running. "task Running" is not a test that notifications work.
            ($script:SmokeText -match 'SUPPORTED_SCHEMAS') | Should -BeTrue
            ($script:SmokeText -match 'notifier schema')   | Should -BeTrue
        }

        It 'exits non-zero when any check fails' {
            ($script:SmokeText -match 'exit \(\[int\]\(\$failed\.Count -gt 0\)\)') | Should -BeTrue
        }

        It 'refuses to raise a real toast about its own fake session' {
            # A live thresholdSec shorter than the run would let the smoke session mature.
            ($script:SmokeText -match 'MinSafeThresholdSec') | Should -BeTrue
            ($script:SmokeText -match 'thresholdSec')        | Should -BeTrue
        }
    }

    Context 'status line command (pure)' {

        BeforeAll {
            $script:SlSpec = Get-SideCrabStatusLineSpec -RepoRoot $script:FakeRoot
        }

        It 'roots the status-line script under the supplied repo root' {
            $script:SlSpec.Script | Should -Be 'C:\Fake\sidecrab\hooks\sidecrab_statusline.py'
        }

        It 'ships the status-line script the spec points at' {
            (Test-Path -LiteralPath (Join-Path $script:RepoRoot 'hooks\sidecrab_statusline.py')) |
                Should -BeTrue
        }

        It 'quotes both the interpreter and the script path' {
            # Unquoted paths break on the space in "Program Files".
            $cmd = Get-SideCrabStatusLineCommand -PythonExe 'C:\Program Files\Python313\python.exe' `
                                                 -ScriptPath 'C:\Dev\side crab\hooks\sidecrab_statusline.py'
            $cmd | Should -Be '"C:\Program Files\Python313\python.exe" "C:\Dev\side crab\hooks\sidecrab_statusline.py"'
        }

        It 'recognises our own status-line command and rejects a foreign one' {
            (Test-SideCrabStatusLineIsOurs -Command '"C:\Python\python.exe" "C:\r\hooks\sidecrab_statusline.py"') |
                Should -BeTrue
            (Test-SideCrabStatusLineIsOurs -Command 'starship prompt') | Should -BeFalse
            (Test-SideCrabStatusLineIsOurs -Command '')                | Should -BeFalse
            (Test-SideCrabStatusLineIsOurs -Command $null)             | Should -BeFalse
        }
    }

    Context 'hook fragment shape (v0.12.0)' {

        BeforeAll {
            $script:Marker2  = '127.0.0.1:9999/v1/hook'
            $script:FragPath = Join-Path $script:RepoRoot 'hooks\settings-hooks-fragment.json'
            $temp = Join-Path ([IO.Path]::GetTempPath()) ("sidecrab-frag-shape-{0}.json" -f [guid]::NewGuid())
            Copy-Item -LiteralPath $script:FragPath -Destination $temp -Force
            try {
                $script:Frag = Get-Content -LiteralPath $temp -Raw -Encoding utf8 |
                               ConvertFrom-Json -AsHashtable -Depth 40
            } finally { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
            $script:FragHooks = $script:Frag['hooks']
        }

        It 'keeps the five curl ingest hooks as command hooks on /v1/hook' {
            foreach ($e in 'SessionStart', 'UserPromptSubmit', 'Notification', 'SubagentStop', 'SessionEnd') {
                $h = $script:FragHooks[$e][0]['hooks'][0]
                $h['type']    | Should -Be 'command'
                $h['command'] | Should -Match 'curl\.exe'
                $h['command'] | Should -Match '127\.0\.0\.1:9999/v1/hook'
            }
        }

        It 'wires Stop as a type-http hook at /v1/hook/stop' {
            $h = $script:FragHooks['Stop'][0]['hooks'][0]
            $h['type'] | Should -Be 'http'
            $h['url']  | Should -Be 'http://127.0.0.1:9999/v1/hook/stop'
            # A short timeout: crabd answers within ~2 s (docs\STATE-CONTRACT.md v0.12.0 item 3).
            ([int] $h['timeout'] -le 10) | Should -BeTrue
        }

        It 'wires PermissionRequest as a type-http hook at /v1/hook/permission' {
            $h = $script:FragHooks['PermissionRequest'][0]['hooks'][0]
            $h['type'] | Should -Be 'http'
            $h['url']  | Should -Be 'http://127.0.0.1:9999/v1/hook/permission'
            # Past crabd's 55 s long-poll (docs\STATE-CONTRACT.md v0.12.0 item 4).
            ([int] $h['timeout'] -ge 55) | Should -BeTrue
        }

        It 'no http hook is wired on SessionStart or Setup (the binary skips those)' {
            # Verified against claude.exe v2.1.246: HTTP hooks are skipped for SessionStart
            # and Setup only. Wiring one there would silently never fire.
            foreach ($e in 'SessionStart', 'Setup') {
                if ($script:FragHooks.Contains($e)) {
                    foreach ($m in @($script:FragHooks[$e])) {
                        foreach ($h in @($m['hooks'])) { $h['type'] | Should -Not -Be 'http' }
                    }
                }
            }
        }

        It 'Get-SideCrabHookEvent matches the http hooks by their url, not just command' {
            # The marker lives in the url of an http hook (no command field); the detector
            # must find it there or Stop/PermissionRequest would look un-installed.
            $events = @(Get-SideCrabHookEvent -Settings $script:Frag -Marker $script:Marker2)
            ($events | ForEach-Object { $_.Event }) | Should -Contain 'Stop'
            ($events | ForEach-Object { $_.Event }) | Should -Contain 'PermissionRequest'
            $events.Count | Should -Be @($script:FragHooks.Keys).Count
        }

        It 'the http URLs still contain the merge/remove marker as a prefix' {
            $script:FragHooks['Stop'][0]['hooks'][0]['url']              | Should -Match ([regex]::Escape($script:Marker2))
            $script:FragHooks['PermissionRequest'][0]['hooks'][0]['url'] | Should -Match ([regex]::Escape($script:Marker2))
        }
    }

    Context 'v0.12.0 install / uninstall / smoke contracts' {

        BeforeAll {
            $script:InstallText3 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1') -Raw -Encoding utf8
            $script:UninstText3  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') -Raw -Encoding utf8
            $script:SmokeText3   = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Test-SideCrab.ps1') -Raw -Encoding utf8
            $script:CommonText3  = Get-Content -LiteralPath $script:Common -Raw -Encoding utf8
            $script:InstallAst3  = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast
            $script:UninstAst3   = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1')).Ast
            function script:Params3 { param($Ast) @($Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) }
            function script:Fn3 {
                param($Ast, [string] $Name)
                $Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
                    Where-Object { $_.Name -eq $Name } | Select-Object -First 1
            }
        }

        It 'Install exposes -WithApprovals, -SkipStatusLine, ConfigPath and ChainPath' {
            $p = script:Params3 $script:InstallAst3
            $p | Should -Contain 'WithApprovals'
            $p | Should -Contain 'SkipStatusLine'
            $p | Should -Contain 'ConfigPath'
            $p | Should -Contain 'ChainPath'
        }

        It 'Install defaults panel approvals OFF - never auto-enables' {
            # -WithApprovals is the only path that writes enabled=$true.
            ($script:InstallText3 -match 'Set-SideCrabPanelApprovals -ConfigPath \$ConfigPath -Enabled \$true') | Should -BeTrue
            ($script:InstallText3 -match 'WithApprovals')      | Should -BeTrue
            ($script:InstallText3 -match 'SECURITY: panel approvals are ON') | Should -BeTrue
        }

        It 'Install saves the prior status line only when it is not already ours' {
            ($script:InstallText3 -match 'Save-SideCrabPriorStatusLine') | Should -BeTrue
            ($script:InstallText3 -match 'Test-SideCrabStatusLineIsOurs') | Should -BeTrue
        }

        It 'Install uses a CONSOLE python for the status line, not pythonw' {
            ($script:InstallText3 -match 'Resolve-SideCrabPythonConsole') | Should -BeTrue
        }

        It 'Uninstall exposes -KeepStatusLine, -KeepApprovals, ConfigPath and ChainPath' {
            $p = script:Params3 $script:UninstAst3
            $p | Should -Contain 'KeepStatusLine'
            $p | Should -Contain 'KeepApprovals'
            $p | Should -Contain 'ConfigPath'
            $p | Should -Contain 'ChainPath'
        }

        It 'Uninstall restores the saved status line and clears the approvals key' {
            ($script:UninstText3 -match 'Get-SideCrabSavedStatusLine')   | Should -BeTrue
            ($script:UninstText3 -match 'Clear-SideCrabPanelApprovals')  | Should -BeTrue
        }

        It 'Show-Status reports the status line and approvals WITHOUT writing them' {
            $fn = script:Fn3 $script:InstallAst3 'Show-Status'
            $fn.Extent.Text | Should -Match 'Get-SideCrabPanelApprovalsState'
            $fn.Extent.Text | Should -Match 'Get-SideCrabSavedStatusLine'
            ($fn.Extent.Text -match 'Set-SideCrabPanelApprovals|Save-SideCrabPriorStatusLine|Set-Content') | Should -BeFalse
        }

        It 'the smoke test reports the status-line chain and approvals read-only' {
            ($script:SmokeText3 -match 'statusline chain')                | Should -BeTrue
            ($script:SmokeText3 -match 'panel approvals')                 | Should -BeTrue
            ($script:SmokeText3 -match 'Get-SideCrabPanelApprovalsState') | Should -BeTrue
            # still no writers (the row 'never writes ...' guarantee extends to the new rows)
            ($script:SmokeText3 -match 'Set-SideCrabPanelApprovals|Save-SideCrabPriorStatusLine') | Should -BeFalse
        }

        It 'the config writers preserve other keys (whole-file rewrite, like POST /v1/config)' {
            foreach ($fn in 'Set-SideCrabPanelApprovals', 'Clear-SideCrabPanelApprovals') {
                $f = script:Fn3 (script:Get-ScriptAst -Path $script:Common).Ast $fn
                ($null -ne $f) | Should -BeTrue
                $f.Extent.Text | Should -Match 'ConvertFrom-Json'   # reads the existing file first
            }
        }
    }

    Context 'disabled tasks survive a re-run (v0.14.0)' {

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @('Get-SideCrabTaskEnableDecision')
            $script:InstallText4 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1') -Raw -Encoding utf8
            $script:UpdateText4  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Update-SideCrab.ps1') -Raw -Encoding utf8
            $script:CommonAst4   = (script:Get-ScriptAst -Path $script:Common).Ast
            $script:InstallAst4  = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast
        }

        It 'leaves a DISABLED task disabled and unstarted on a plain re-run' {
            # The defect: the installer resurrected SideCrab-glow, which is parked because the
            # Corsair SDK crashes headless (docs/BACKLOG.md), and started it into that crash.
            $d = Get-SideCrabTaskEnableDecision -Registered $true -PriorState 'Disabled' -ForceEnable $false
            $d.WasDisabled   | Should -BeTrue
            $d.LeaveDisabled | Should -BeTrue
            $d.Start         | Should -BeFalse
            $d.Reason        | Should -Match 'ForceEnable'
        }

        It 're-enables and starts a disabled task ONLY under -ForceEnable' {
            $d = Get-SideCrabTaskEnableDecision -Registered $true -PriorState 'Disabled' -ForceEnable $true
            $d.LeaveDisabled | Should -BeFalse
            $d.Start         | Should -BeTrue
            $d.Reason        | Should -Match 're-enabled'
        }

        It 'starts a task that was Ready, Running, or not registered at all' {
            foreach ($prior in 'Ready', 'Running') {
                $d = Get-SideCrabTaskEnableDecision -Registered $true -PriorState $prior -ForceEnable $false
                $d.LeaveDisabled | Should -BeFalse
                $d.Start         | Should -BeTrue
            }
            $new = Get-SideCrabTaskEnableDecision -Registered $false -PriorState '' -ForceEnable $false
            $new.WasDisabled   | Should -BeFalse
            $new.Start         | Should -BeTrue
            $new.Reason        | Should -Match 'newly registered'
        }

        It 'Register-SideCrabTask reads the prior state BEFORE the -Force write and restores Disabled' {
            # Order is load-bearing: Register-ScheduledTask -Force always writes an ENABLED
            # task, so the disabled flag is gone by the time the write returns.
            $fn = ($script:CommonAst4.FindAll({
                       param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
                   }, $true) | Where-Object { $_.Name -eq 'Register-SideCrabTask' } | Select-Object -First 1)
            ($null -ne $fn) | Should -BeTrue
            $text = $fn.Extent.Text
            $text | Should -Match 'Get-SideCrabTaskEnableDecision'
            $text | Should -Match 'Disable-ScheduledTask'
            # The invocations, not the doc comment that also names them.
            $probe    = $text.IndexOf('Get-SideCrabTaskState -TaskName')
            $register = $text.IndexOf('Register-ScheduledTask -TaskName')
            $disable  = $text.IndexOf('Disable-ScheduledTask -TaskName')
            ($probe -ge 0 -and $register -gt $probe -and $disable -gt $register) | Should -BeTrue
        }

        It 'Install exposes -ForceEnable and starts only what the decision says to start' {
            @($script:InstallAst4.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) |
                Should -Contain 'ForceEnable'
            ($script:InstallText4 -match '-ForceEnable:\$ForceEnable') | Should -BeTrue
            ($script:InstallText4 -match 'if \(\$reg\.Start\)')        | Should -BeTrue
            ($script:InstallText4 -match 'LEFT DISABLED')              | Should -BeTrue
        }

        It 'Update leaves a disabled task alone rather than restarting it into a crash' {
            ($script:UpdateText4 -match "\`$s\.State -eq 'Disabled'") | Should -BeTrue
        }
    }

    Context 'panel-approval verification script (v0.14.0)' {

        BeforeAll {
            $script:VerifyPath = Join-Path $script:SetupDir 'Verify-PanelApproval.ps1'
            $script:VerifyText = Get-Content -LiteralPath $script:VerifyPath -Raw -Encoding utf8
            $script:VerifyAst  = (script:Get-ScriptAst -Path $script:VerifyPath).Ast
        }

        It 'exposes -DryRun and the paths it reads' {
            $p = @($script:VerifyAst.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            foreach ($n in 'DryRun', 'ConfigPath', 'SettingsPath', 'BaseUri', 'RepoRoot') {
                $p | Should -Contain $n
            }
        }

        It 'NEVER enables approvals and never decides for the operator' {
            # The whole point of panel approval is that a human taps. A script that could
            # POST a decide, or flip the config, would be the hole it exists to close.
            foreach ($forbidden in 'Set-SideCrabPanelApprovals', 'Clear-SideCrabPanelApprovals',
                                   'Set-Content', 'Out-File', 'New-Item',
                                   'New-ItemProperty', 'Set-ItemProperty', 'Remove-Item') {
                ($script:VerifyText -match [regex]::Escape($forbidden)) | Should -BeFalse
            }
            # /v1/action may only ever be PRINTED for the operator to run - never called. Any
            # line naming it that is not a Write-Host is this script deciding for a human.
            $acting = @($script:VerifyText -split "`n" |
                        Where-Object { $_ -match '/v1/action' -and $_.TrimStart() -notmatch '^(Write-Host|#)' })
            ($acting -join ' | ') | Should -Be ''
            ($script:VerifyText -match 'Get-SideCrabPanelApprovalsState') | Should -BeTrue
        }

        It 'touches no scheduled task' {
            foreach ($verb in 'Register-ScheduledTask', 'Unregister-ScheduledTask',
                              'Start-ScheduledTask', 'Stop-ScheduledTask',
                              'Enable-ScheduledTask', 'Disable-ScheduledTask') {
                ($script:VerifyText -match [regex]::Escape($verb)) | Should -BeFalse
            }
        }

        It 'checks the wiring the live run depends on' {
            ($script:VerifyText -match '/v1/hook/permission')       | Should -BeTrue
            ($script:VerifyText -match 'allowedHttpHookUrls')       | Should -BeTrue
            ($script:VerifyText -match 'Get-SideCrabHookEvent')     | Should -BeTrue
            ($script:VerifyText -match 'Get-SideCrabHealth')        | Should -BeTrue
        }

        It 'prints the enable, approve, confirm and deny steps' {
            ($script:VerifyText -match '-WithApprovals')            | Should -BeTrue
            ($script:VerifyText -match '"decision":"allow"')        | Should -BeTrue
            ($script:VerifyText -match 'decision":"deny"')          | Should -BeTrue
            ($script:VerifyText -match 'approved from panel')       | Should -BeTrue
            ($script:VerifyText -match 'denied from the SideCrab panel') | Should -BeTrue
            ($script:VerifyText -match 'DISPOSABLE')                | Should -BeTrue
        }

        It 'makes -DryRun change nothing AND make no request' {
            # A "dry run" that still POSTs would be a lie in the one script whose whole
            # contract is "this does not act for you".
            $dry = ([regex]::Match($script:VerifyText, '(?s)if \(\$DryRun\) \{.*?\} else \{')).Value
            ($dry -match 'Invoke-WebRequest|Invoke-RestMethod') | Should -BeFalse
            ($script:VerifyText -match 'nothing was checked and nothing was changed') | Should -BeTrue
        }

        It 'exits non-zero when a blocker stands' {
            ($script:VerifyText -match 'exit 1')     | Should -BeTrue
            ($script:VerifyText -match 'Blockers')   | Should -BeTrue
        }

        It 'the smoke test points at it rather than passing the posture silently' {
            $smoke = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Test-SideCrab.ps1') -Raw -Encoding utf8
            ($smoke -match 'Verify-PanelApproval\.ps1') | Should -BeTrue
        }
    }

    Context 'smoke rows say something (v0.14.0)' {

        BeforeAll {
            $script:SmokeText5 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Test-SideCrab.ps1') -Raw -Encoding utf8
        }

        It 'fails a status line that points outside this repo, not just a missing marker' {
            # Test-SideCrabStatusLineIsOurs matches the script NAME, so another checkout's
            # command passes it while running a script this run never checked.
            ($script:SmokeText5 -match '\$pointsHere') | Should -BeTrue
            ($script:SmokeText5 -match 'points OUTSIDE this repo') | Should -BeTrue
        }

        It 'fails an installed status line with no chain file - uninstall could not restore' {
            ($script:SmokeText5 -match 'NO chain file') | Should -BeTrue
        }

        It 'names the prior command it chains to, not just "saved-prior present"' {
            ($script:SmokeText5 -match 'chains to saved prior') | Should -BeTrue
        }

        It 'judges panel approvals when ON and reports the hook wiring either way' {
            ($script:SmokeText5 -match 'PermissionRequest hook \$\(if \(\$permWired\)') | Should -BeTrue
            ($script:SmokeText5 -match 'BLOCKED by allowedHttpHookUrls') | Should -BeTrue
            ($script:SmokeText5 -match '\$permWired -and \$permAllowed -ne \$false') | Should -BeTrue
        }
    }

    Context 'config and chain helpers (temp files)' {

        BeforeAll {
            # Same AST lift the rest of the suite uses - bind just these helpers' bodies, so
            # the setup scripts' top-level code is never reached and nothing is installed.
            # They touch only the temp files created below.
            script:Import-AstFunction -Path $script:Common -Name @(
                'Get-SideCrabPanelApprovalsState', 'Set-SideCrabPanelApprovals',
                'Clear-SideCrabPanelApprovals', 'Get-SideCrabPanelToken',
                'Get-SideCrabLimitsTokenState', 'Set-SideCrabLimitsToken',
                'Backup-SideCrabFile', 'Write-SideCrabFileAtomic', 'Get-SideCrabBackupFile',
                'Get-SideCrabBackupPattern', 'Read-SideCrabBackupStamp',
                'Save-SideCrabPriorStatusLine', 'Get-SideCrabSavedStatusLine'
            )
            $script:Tmp = Join-Path ([IO.Path]::GetTempPath()) ("sidecrab-cfg-{0}" -f [guid]::NewGuid())
            New-Item -ItemType Directory -Force -Path $script:Tmp | Out-Null
        }
        AfterAll {
            if ($script:Tmp -and (Test-Path -LiteralPath $script:Tmp)) {
                Remove-Item -LiteralPath $script:Tmp -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        It 'reads the pairing code (crabd 0.29.0) in display form, or Present=false' {
            $missing = Join-Path $script:Tmp 'no-token'
            (Get-SideCrabPanelToken -TokenPath $missing).Present | Should -BeFalse
            $tokFile = Join-Path $script:Tmp 'panel-token'
            Set-Content -LiteralPath $tokFile -Value "k7qxm-2pdab`n" -Encoding utf8NoBOM
            $t = Get-SideCrabPanelToken -TokenPath $tokFile
            $t.Present | Should -BeTrue
            $t.Code    | Should -Be 'K7QXM-2PDAB'
            Set-Content -LiteralPath $tokFile -Value 'abc' -Encoding utf8NoBOM        # unusable
            (Get-SideCrabPanelToken -TokenPath $tokFile).Present | Should -BeFalse
        }

        It 'the installer never prints the pairing code on -Status, only on -PairingCode' {
            $text = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1') -Raw
            ($text -match '\[switch\] \$PairingCode') | Should -BeTrue
            # the status row names presence and the path, never $tok.Code
            $statusLine = ($text -split "`n" | Where-Object { $_ -match 'pairing: code present' })
            $statusLine | Should -Not -BeNullOrEmpty
            ($statusLine -match '\$\(\$tok\.Code\)') | Should -BeFalse
        }

        It 'stores the long-lived limits token DPAPI-protected and reports presence only (crabd 0.30.0)' {
            $lt = Join-Path $script:Tmp 'limits-token.dpapi'
            (Get-SideCrabLimitsTokenState -TokenPath $lt).Present | Should -BeFalse
            $secure = ConvertTo-SecureString 'sk-ant-oat01-testtoken' -AsPlainText -Force
            $res = Set-SideCrabLimitsToken -TokenPath $lt -Token $secure
            $res.Bytes | Should -BeGreaterThan 40
            (Get-SideCrabLimitsTokenState -TokenPath $lt).Present | Should -BeTrue
            $blob = [IO.File]::ReadAllBytes($lt)
            ([Text.Encoding]::UTF8.GetString($blob)) | Should -Not -Match 'sk-ant-oat01'     # never plaintext on disk
            Add-Type -AssemblyName System.Security -ErrorAction SilentlyContinue
            $back = [Security.Cryptography.ProtectedData]::Unprotect($blob, $null, 'CurrentUser')
            [Text.Encoding]::UTF8.GetString($back) | Should -Be 'sk-ant-oat01-testtoken'
            { Set-SideCrabLimitsToken -TokenPath $lt -Token (ConvertTo-SecureString 'nope' -AsPlainText -Force) } | Should -Throw
        }

        It 'reads panelApprovals as null when the config file is absent' {
            $missing = Join-Path $script:Tmp 'nope.json'
            (Get-SideCrabPanelApprovalsState -ConfigPath $missing).Enabled | Should -BeNullOrEmpty
        }

        It 'sets panelApprovals.enabled while PRESERVING every other key' {
            $cfg = Join-Path $script:Tmp 'config.json'
            @{ quietHours = $null; toast = @{ thresholdSec = 120; enabled = $true }; recapRepos = @('C:\Dev\sidecrab') } |
                ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $cfg -Encoding utf8NoBOM
            Set-SideCrabPanelApprovals -ConfigPath $cfg -Enabled $true | Out-Null
            $back = Get-Content -LiteralPath $cfg -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable -Depth 10
            $back['panelApprovals']['enabled'] | Should -BeTrue
            $back['toast']['thresholdSec']     | Should -Be 120           # untouched
            @($back['recapRepos'])[0]          | Should -Be 'C:\Dev\sidecrab'
            (Get-SideCrabPanelApprovalsState -ConfigPath $cfg).Enabled | Should -BeTrue
        }

        It 'clears panelApprovals while leaving the rest of the config intact' {
            $cfg = Join-Path $script:Tmp 'config2.json'
            @{ toast = @{ enabled = $true }; panelApprovals = @{ enabled = $true } } |
                ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $cfg -Encoding utf8NoBOM
            (Clear-SideCrabPanelApprovals -ConfigPath $cfg).Action | Should -Be 'removed'
            $back = Get-Content -LiteralPath $cfg -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable -Depth 10
            $back.Contains('panelApprovals') | Should -BeFalse
            $back['toast']['enabled']        | Should -BeTrue
        }

        # -- SET-a2: config.json gets the same pre-write backup + atomic write settings.json has.

        It 'Set-SideCrabPanelApprovals backs up the EXISTING config before rewriting it (SET-a2)' {
            $cfg = Join-Path $script:Tmp 'cfg-backup.json'
            @{ toast = @{ thresholdSec = 90 } } | ConvertTo-Json -Depth 10 |
                Set-Content -LiteralPath $cfg -Encoding utf8NoBOM
            $res = Set-SideCrabPanelApprovals -ConfigPath $cfg -Enabled $true
            $res.Backup | Should -Not -BeNullOrEmpty
            (Test-Path -LiteralPath $res.Backup) | Should -BeTrue
            (Split-Path -Leaf $res.Backup) | Should -Match 'cfg-backup\.json\.sidecrab-bak-\d{8}-\d{6}'
            # the backup is the PRE-write state - it must NOT carry the key we just added
            $bakRaw = Get-Content -LiteralPath $res.Backup -Raw -Encoding utf8
            ($bakRaw -match 'panelApprovals') | Should -BeFalse
            ($bakRaw | ConvertFrom-Json -AsHashtable -Depth 10)['toast']['thresholdSec'] | Should -Be 90
        }

        It 'a first-ever config write has no backup to make, but still writes (SET-a2)' {
            $cfg = Join-Path $script:Tmp 'cfg-first.json'
            $res = Set-SideCrabPanelApprovals -ConfigPath $cfg -Enabled $false
            $res.Backup | Should -BeNullOrEmpty
            (Test-Path -LiteralPath $cfg) | Should -BeTrue
        }

        It 'Clear-SideCrabPanelApprovals backs up before removing the key (SET-a2)' {
            $cfg = Join-Path $script:Tmp 'cfg-clear.json'
            @{ toast = @{ enabled = $true }; panelApprovals = @{ enabled = $true } } |
                ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $cfg -Encoding utf8NoBOM
            $res = Clear-SideCrabPanelApprovals -ConfigPath $cfg
            $res.Action | Should -Be 'removed'
            $res.Backup | Should -Not -BeNullOrEmpty
            # the backup still HAS the key that the clear removed
            (Get-Content -LiteralPath $res.Backup -Raw -Encoding utf8) -match 'panelApprovals' | Should -BeTrue
        }

        It 'the config write is atomic: it replaces via temp+rename and leaves the new bytes, no temp (SET-a2)' {
            $f = Join-Path $script:Tmp 'atomic.json'
            'OLDOLDOLD' | Set-Content -LiteralPath $f -Encoding utf8NoBOM
            Write-SideCrabFileAtomic -Path $f -Content 'NEWCONTENT'
            (Get-Content -LiteralPath $f -Raw -Encoding utf8).Trim() | Should -Be 'NEWCONTENT'
            # the whole point: no half-written scratch file is left where a reader could see it,
            # and the temp infix is NOT the backup infix so it could never be mistaken for one.
            @(Get-ChildItem -LiteralPath $script:Tmp -Filter 'atomic.json.sidecrab-tmp-*' -File).Count | Should -Be 0
        }

        It 'the atomic write creates the parent directory and a fresh file (SET-a2)' {
            $f = Join-Path $script:Tmp 'nested\deeper\new.json'
            Write-SideCrabFileAtomic -Path $f -Content '{"a":1}'
            (Test-Path -LiteralPath $f) | Should -BeTrue
            (Get-Content -LiteralPath $f -Raw -Encoding utf8).Trim() | Should -Be '{"a":1}'
        }

        It 'Backup-SideCrabFile makes a restorable, convention-named config backup (SET-a2)' {
            $cfg = Join-Path $script:Tmp 'rbak.json'
            '{"x":1}' | Set-Content -LiteralPath $cfg -Encoding utf8NoBOM
            $bak = Backup-SideCrabFile -Path $cfg
            $bak | Should -Not -BeNullOrEmpty
            (Read-SideCrabBackupStamp -Name (Split-Path -Leaf $bak)) | Should -Not -BeNullOrEmpty
            @(Get-SideCrabBackupFile -TargetPath $cfg).Count | Should -Be 1
            # nothing to copy for a file that is not there yet
            Backup-SideCrabFile -Path (Join-Path $script:Tmp 'no-such-file.json') | Should -BeNullOrEmpty
        }

        It 'Get-SideCrabBackupFile finds config backups newest-first (SET-a2 - Restore covers config)' {
            $cfg = Join-Path $script:Tmp 'rlist.json'
            '{"a":1}' | Set-Content -LiteralPath $cfg -Encoding utf8NoBOM
            '{"a":1}' | Set-Content -LiteralPath "$cfg.sidecrab-bak-20260101-000000" -Encoding utf8NoBOM
            '{"a":2}' | Set-Content -LiteralPath "$cfg.sidecrab-bak-20260102-000000" -Encoding utf8NoBOM
            $rows = @(Get-SideCrabBackupFile -TargetPath $cfg)
            $rows.Count | Should -Be 2
            (Split-Path -Leaf $rows[0].Path) | Should -Match '20260102'   # newest first, by NAME
        }

        It 'round-trips a saved prior status line, and reads a null prior as present-with-null' {
            $chain = Join-Path $script:Tmp 'statusline-chain.json'
            $prior = @{ type = 'command'; command = 'starship prompt'; padding = 0 }
            Save-SideCrabPriorStatusLine -ChainPath $chain -PriorStatusLine $prior | Out-Null
            $got = Get-SideCrabSavedStatusLine -ChainPath $chain
            $got.Present               | Should -BeTrue
            $got.StatusLine['command'] | Should -Be 'starship prompt'

            Save-SideCrabPriorStatusLine -ChainPath $chain -PriorStatusLine $null | Out-Null
            $none = Get-SideCrabSavedStatusLine -ChainPath $chain
            $none.Present    | Should -BeTrue
            $none.StatusLine | Should -BeNullOrEmpty
        }

        It 'reports Present=false when the chain file is absent' {
            (Get-SideCrabSavedStatusLine -ChainPath (Join-Path $script:Tmp 'no-chain.json')).Present |
                Should -BeFalse
        }
    }

    Context 'backup + restore decisions (v0.15.0)' {

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Get-SideCrabBackupPattern', 'Read-SideCrabBackupStamp',
                'Get-SideCrabCanonicalValue', 'ConvertTo-SideCrabCanonicalJson',
                'Test-SideCrabHookMatcherIsOurs', 'Split-SideCrabHookMatcher',
                'Split-SideCrabSettings',
                'Compare-SideCrabSettingsPair', 'Get-SideCrabPruneDecision',
                'Get-SideCrabResidueSpec'
            )

            $script:OurStop = @{ hooks = @(@{ type = 'http'; url = 'http://127.0.0.1:9999/v1/hook/stop'; headers = @{ 'X-SideCrab-Panel' = '1' } }) }
            $script:TheirStop = @{ hooks = @(@{ type = 'command'; command = 'echo other' }) }
            $script:OurSl   = @{ type = 'command'; command = '"py.exe" "C:\Dev\sidecrab\hooks\sidecrab_statusline.py"' }

            function script:Doc {
                param([hashtable] $Extra = @{}, [switch] $WithOurs)
                $d = @{ theme = 'dark'; permissions = @{ allow = @('Bash') } }
                if ($WithOurs) {
                    $d['hooks'] = @{ Stop = @($script:OurStop) }
                    $d['statusLine'] = $script:OurSl
                }
                foreach ($k in $Extra.Keys) { $d[$k] = $Extra[$k] }
                $d
            }
        }

        It 'reads the instant out of the installer backup name' {
            $stamp = Read-SideCrabBackupStamp -Name 'settings.json.sidecrab-bak-20260826-213414'
            $stamp.Year   | Should -Be 2026
            $stamp.Month  | Should -Be 8
            $stamp.Day    | Should -Be 26
            $stamp.Hour   | Should -Be 21
            $stamp.Minute | Should -Be 34
            $stamp.Second | Should -Be 14
        }

        It 'returns null for a name that carries no well-formed stamp' {
            Read-SideCrabBackupStamp -Name 'settings.json'                          | Should -BeNullOrEmpty
            Read-SideCrabBackupStamp -Name 'settings.json.sidecrab-bak-yesterday'    | Should -BeNullOrEmpty
            Read-SideCrabBackupStamp -Name 'settings.json.sidecrab-bak-20261301-000000' | Should -BeNullOrEmpty
        }

        It 'builds the backup wildcard from the settings file NAME, not its path' {
            Get-SideCrabBackupPattern -SettingsPath 'C:\Users\me\.claude\settings.json' |
                Should -Be 'settings.json.sidecrab-bak-*'
        }

        It 'canonical JSON ignores key order but respects array order' {
            $a = ConvertTo-SideCrabCanonicalJson -Value @{ b = 1; a = 2 }
            $b = ConvertTo-SideCrabCanonicalJson -Value @{ a = 2; b = 1 }
            $a | Should -Be $b
            (ConvertTo-SideCrabCanonicalJson -Value @(1, 2)) |
                Should -Not -Be (ConvertTo-SideCrabCanonicalJson -Value @(2, 1))
        }

        It 'keeps a one-element array an ARRAY' {
            # Without the comma-wrap in Get-SideCrabCanonicalValue the pipeline unrolls it to a
            # scalar, and a hooks event with one matcher compares equal to that bare matcher.
            (ConvertTo-SideCrabCanonicalJson -Value @(@{ a = 1 })) | Should -Match '^\['
        }

        It 'splits our hook entries from theirs, per event' {
            $doc = script:Doc
            $doc['hooks'] = @{ Stop = @($script:TheirStop, $script:OurStop); Notification = @($script:TheirStop) }
            $split = Split-SideCrabSettings -Settings $doc
            @($split.Ours['hooks']['Stop']).Count       | Should -Be 1
            $split.Ours['hooks'].Contains('Notification') | Should -BeFalse
            @($split.Foreign['hooks']['Stop']).Count    | Should -Be 1
            @($split.Foreign['hooks']['Notification']).Count | Should -Be 1
        }

        It 'claims OUR status line and disclaims someone else''s' {
            (Split-SideCrabSettings -Settings (script:Doc -WithOurs)).Ours['statusLine'] | Should -Not -BeNullOrEmpty
            $theirs = script:Doc -Extra @{ statusLine = @{ type = 'command'; command = 'starship prompt' } }
            $split  = Split-SideCrabSettings -Settings $theirs
            $split.Ours['statusLine']    | Should -BeNullOrEmpty
            $split.Foreign['statusLine'] | Should -Not -BeNullOrEmpty
        }

        It 'does NOT gate a restore that only puts SideCrab wiring back' {
            # The whole point of a restore. A guard that fires here would make the safety net
            # unusable and train the operator to pass -Force by reflex.
            $cmp = Compare-SideCrabSettingsPair -Backup (script:Doc) -Current (script:Doc -WithOurs)
            $cmp.ForeignChanged     | Should -BeFalse
            @($cmp.SideCrabDiff).Count | Should -Be 2      # hooks/Stop and statusLine
        }

        It 'gates a restore that would revert, delete or resurrect a foreign key' {
            $backup  = script:Doc
            $current = script:Doc -Extra @{ theme = 'light'; enabledPlugins = @('x') } -WithOurs
            $current.Remove('permissions')
            $cmp = Compare-SideCrabSettingsPair -Backup $backup -Current $current
            $cmp.ForeignChanged | Should -BeTrue
            $states = @{}
            foreach ($d in $cmp.ForeignDiff) { $states[$d.Key] = $d.State }
            $states['theme']          | Should -Match 'REVERTS'
            $states['enabledPlugins'] | Should -Match 'DELETES'
            $states['permissions']    | Should -Match 'BRINGS IT BACK'
        }

        It 'calls two documents that differ only in key order identical' {
            $a = [ordered]@{ theme = 'dark'; permissions = @{ allow = @('Bash') } }
            $b = [ordered]@{ permissions = @{ allow = @('Bash') }; theme = 'dark' }
            (Compare-SideCrabSettingsPair -Backup $a -Current $b).Identical | Should -BeTrue
        }

        It 'never prunes the newest backup, whatever the cutoff' {
            $now  = Get-Date
            $rows = @(
                [pscustomobject]@{ Path = 'a'; Stamp = $now.AddDays(-400) }
                [pscustomobject]@{ Path = 'b'; Stamp = $now.AddDays(-900) }
            )
            $d = @(Get-SideCrabPruneDecision -Backup $rows -OlderThanDays 0 -Now $now)
            ($d | Where-Object { $_.Path -eq 'a' }).Delete | Should -BeFalse
            ($d | Where-Object { $_.Path -eq 'b' }).Delete | Should -BeTrue
        }

        It 'prunes only what is past the cutoff' {
            $now  = Get-Date
            $rows = @(
                [pscustomobject]@{ Path = 'newest'; Stamp = $now.AddDays(-1) }
                [pscustomobject]@{ Path = 'inside'; Stamp = $now.AddDays(-10) }
                [pscustomobject]@{ Path = 'past';   Stamp = $now.AddDays(-45) }
            )
            $d = @(Get-SideCrabPruneDecision -Backup $rows -OlderThanDays 30 -Now $now)
            @($d | Where-Object { $_.Delete }).Count | Should -Be 1
            ($d | Where-Object { $_.Delete }).Path   | Should -Be 'past'
        }

        It 'handles an empty backup pile without throwing' {
            @(Get-SideCrabPruneDecision -Backup @() -OlderThanDays 30 -Now (Get-Date)).Count | Should -Be 0
        }
    }

    Context 'uninstall residue decisions (v0.15.0)' {

        BeforeAll {
            # Self-contained: lift what this context needs rather than depending on another
            # context's BeforeAll having run first.
            script:Import-AstFunction -Path $script:Common -Name @(
                'Get-SideCrabBackupPattern', 'Get-SideCrabResidueSpec'
            )
            $script:Residue = @(Get-SideCrabResidueSpec `
                -SettingsPath 'C:\Fake\.claude\settings.json' `
                -ConfigPath   'C:\Fake\.sidecrab\config.json' `
                -ChainPath    'C:\Fake\.sidecrab\statusline-chain.json')
            function script:Res { param([string] $Key) $script:Residue | Where-Object { $_.Key -eq $Key } | Select-Object -First 1 }
            $script:UninstallText2 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') -Raw -Encoding utf8
        }

        It 'names every file an install leaves outside the repo' {
            foreach ($k in 'chain', 'config', 'history', 'toaststate', 'limitscache', 'glowlog', 'logs', 'backups') {
                (script:Res $k) | Should -Not -BeNullOrEmpty
            }
        }

        It 'treats the status-line chain file as WIRING - removed by a plain uninstall' {
            (script:Res 'chain').Kind        | Should -Be 'wiring'
            (script:Res 'chain').Disposition | Should -Be 'uninstall'
        }

        It 'treats the operator''s own files as DATA - kept unless -Purge' {
            foreach ($k in 'config', 'history', 'toaststate') {
                (script:Res $k).Disposition | Should -Be 'purge'
            }
        }

        It 'NEVER disposes of the settings backups at any switch' {
            # The moment you most need last week's settings.json is the moment after an
            # uninstall went wrong. -Purge must not reach them either.
            (script:Res 'backups').Disposition | Should -Be 'keep'
            @($script:Residue | Where-Object { $_.Kind -eq 'backup' -and $_.Disposition -ne 'keep' }).Count | Should -Be 0
        }

        It 'removes the state directory only when purging emptied it' {
            (script:Res 'statedir').Disposition | Should -Be 'purge-if-empty'
            ($script:UninstallText2 -match 'not ours')  | Should -BeTrue
        }

        It 'gives every row a reason a reader can act on' {
            foreach ($r in $script:Residue) { ($r.Why.Length -gt 20) | Should -BeTrue }
        }

        It 'drops the chain file even when the settings restore never ran' {
            # The orphan this fixed: with no settings.json there was nothing to restore INTO,
            # the whole block was skipped, and the chain file was stranded forever.
            ($script:UninstallText2 -match 'Remove the status-line chain file') | Should -BeTrue
            ($script:UninstallText2 -match 'for your records')                  | Should -BeTrue
        }

        It 'exposes -Purge and reads the residue table instead of its own list' {
            $ast = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1')).Ast
            @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) | Should -Contain 'Purge'
            ($script:UninstallText2 -match 'Get-SideCrabResidueSpec') | Should -BeTrue
        }

        It 'documents the keep/remove decision in the script header' {
            ($script:UninstallText2 -match 'removes WIRING and keeps DATA') | Should -BeTrue
            ($script:UninstallText2 -match 'KEPT AT EVERY SWITCH')          | Should -BeTrue
        }
    }

    Context 'doctor decisions (v0.15.0)' {

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Get-SideCrabStaleCodeDecision', 'Test-SideCrabHookUrlAllowed',
                'Get-SideCrabCommandPath', 'Get-SideCrabPathOwnership'
            )
            $script:Started = [datetime] '2026-08-26 21:34:14'
        }

        It 'calls a served version that differs from the file on disk STALE' {
            $d = Get-SideCrabStaleCodeDecision -State 'Running' -LastRunTime $script:Started `
                    -ScriptWriteTime $script:Started.AddMinutes(-10) `
                    -ReportedVersion '0.13.0' -FileVersion '0.14.0'
            $d.Stale  | Should -BeTrue
            $d.Reason | Should -Match '0\.13\.0'
        }

        It 'calls a script written after the task started STALE' {
            $d = Get-SideCrabStaleCodeDecision -State 'Running' -LastRunTime $script:Started `
                    -ScriptWriteTime $script:Started.AddMinutes(92)
            $d.Stale  | Should -BeTrue
            $d.Reason | Should -Match '92 min'
        }

        It 'calls a process started after the last edit CURRENT' {
            (Get-SideCrabStaleCodeDecision -State 'Running' -LastRunTime $script:Started `
                -ScriptWriteTime $script:Started.AddHours(-3)).Stale | Should -BeFalse
        }

        It 'never calls a task that is not Running stale' {
            # A DISABLED glow is a decision, not a fault - reporting it as stale code would
            # invite a "fix" that starts it into the SDK crash (docs/BACKLOG.md).
            (Get-SideCrabStaleCodeDecision -State 'Disabled' -LastRunTime $script:Started `
                -ScriptWriteTime $script:Started.AddYears(1)).Stale | Should -BeFalse
            (Get-SideCrabStaleCodeDecision -State 'Ready' -LastRunTime $null -ScriptWriteTime $null).Verdict |
                Should -Be 'not-running'
        }

        It 'reports UNKNOWN rather than inventing a fault when a timestamp is missing' {
            $d = Get-SideCrabStaleCodeDecision -State 'Running' -LastRunTime $null -ScriptWriteTime $script:Started
            $d.Verdict | Should -Be 'unknown'
            $d.Stale   | Should -BeFalse
        }

        It 'treats an UNSET allow-list as allow-everything and an EMPTY one as allow-nothing' {
            Test-SideCrabHookUrlAllowed -Url 'http://127.0.0.1:9999/v1/hook/permission' -Patterns $null | Should -BeTrue
            Test-SideCrabHookUrlAllowed -Url 'http://127.0.0.1:9999/v1/hook/permission' -Patterns @()   | Should -BeFalse
        }

        It 'matches the allow-list by wildcard, the way the CLI does' {
            $u = 'http://127.0.0.1:9999/v1/hook/permission'
            Test-SideCrabHookUrlAllowed -Url $u -Patterns @('http://127.0.0.1:9999/*') | Should -BeTrue
            Test-SideCrabHookUrlAllowed -Url $u -Patterns @('http://localhost:9999/*')  | Should -BeFalse
        }

        It 'pulls the script path out of a quoted command string' {
            $cmd = '"C:\Python313\python.exe" "C:\Dev\sidecrab\hooks\sidecrab_statusline.py"'
            @(Get-SideCrabCommandPath -Command $cmd) | Should -Contain 'C:\Dev\sidecrab\hooks\sidecrab_statusline.py'
            @(Get-SideCrabCommandPath -Command '').Count | Should -Be 0
        }

        It 'spots wiring that names ANOTHER sidecrab checkout' {
            Get-SideCrabPathOwnership -Path 'C:\Dev\sidecrab\companion\crabd.py' -RepoRoot 'C:\Dev\sidecrab' | Should -Be 'inside'
            Get-SideCrabPathOwnership -Path 'C:\Old\sidecrab\companion\crabd.py' -RepoRoot 'C:\Dev\sidecrab' | Should -Be 'foreign-checkout'
            Get-SideCrabPathOwnership -Path 'C:\Python313\python.exe'             -RepoRoot 'C:\Dev\sidecrab' | Should -Be 'unrelated'
        }

        It 'does not read sidecrab2 as inside sidecrab' {
            Get-SideCrabPathOwnership -Path 'C:\Dev\sidecrab2\companion\crabd.py' -RepoRoot 'C:\Dev\sidecrab' |
                Should -Be 'foreign-checkout'
        }
    }

    Context 'restore + doctor script contracts (v0.15.0)' {

        BeforeAll {
            $script:RestoreAst  = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Restore-SideCrab.ps1')).Ast
            $script:RepairAst   = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Repair-SideCrab.ps1')).Ast
            $script:RestoreText = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Restore-SideCrab.ps1') -Raw -Encoding utf8
            $script:RepairText  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1')  -Raw -Encoding utf8

            # The commands a script actually INVOKES, from the AST. A command name inside a
            # string literal - a suggestion printed for the operator - is not a CommandAst, so
            # this distinguishes "tells you to run it" from "runs it".
            function script:Get-InvokedCommand {
                param($Ast)
                @($Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
                  ForEach-Object { $_.GetCommandName() } |
                  Where-Object { $_ } | Sort-Object -Unique)
            }
        }

        It 'both new scripts support ShouldProcess and dot-source the shared helpers' {
            foreach ($a in $script:RestoreAst, $script:RepairAst) {
                ("$($a.ParamBlock.Attributes.Extent.Text)" -match 'SupportsShouldProcess') | Should -BeTrue
            }
            ($script:RestoreText -match 'SideCrab\.Common\.ps1') | Should -BeTrue
            ($script:RepairText  -match 'SideCrab\.Common\.ps1') | Should -BeTrue
        }

        It 'Restore exposes -Latest, -Backup, -List, -PruneOlderThan and -Force' {
            $p = @($script:RestoreAst.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            foreach ($n in 'Latest', 'Backup', 'List', 'PruneOlderThan', 'Force') { $p | Should -Contain $n }
        }

        It 'Restore refuses a foreign-edit clobber and names -Force as the only override' {
            ($script:RestoreText -match 'REFUSED - nothing was changed') | Should -BeTrue
            ($script:RestoreText -match 'If those edits are meant to go, re-run with -Force') | Should -BeTrue
        }

        It 'Restore backs up the file it is about to overwrite' {
            # A restore that is not itself reversible is a second way to lose settings.json.
            ($script:RestoreText -match 'the state you are restoring OVER') | Should -BeTrue
            (script:Get-InvokedCommand $script:RestoreAst) | Should -Contain 'Copy-Item'
        }

        It 'Restore touches no task, no registry and no config' {
            $invoked = script:Get-InvokedCommand $script:RestoreAst
            foreach ($bad in 'Start-ScheduledTask', 'Stop-ScheduledTask', 'Register-ScheduledTask',
                             'Unregister-ScheduledTask', 'Enable-ScheduledTask', 'New-ItemProperty',
                             'Set-ItemProperty', 'Set-SideCrabPanelApprovals') {
                $invoked | Should -Not -Contain $bad
            }
        }

        It 'Repair defaults to report-only: it writes no file at all' {
            # The doctor's whole value is that running it is never a decision. Set-Content or
            # Out-File anywhere in it would make "just run the doctor" a state change.
            $invoked = script:Get-InvokedCommand $script:RepairAst
            foreach ($bad in 'Set-Content', 'Out-File', 'Add-Content', 'Remove-Item', 'Copy-Item') {
                $invoked | Should -Not -Contain $bad
            }
        }

        It 'Repair never enables a deliberately-disabled task, and never arms approvals' {
            # Enable-ScheduledTask appears in the report as a SUGGESTION; invoking it would
            # start the parked glow into the SDK crash (docs/BACKLOG.md).
            $invoked = script:Get-InvokedCommand $script:RepairAst
            $invoked | Should -Not -Contain 'Enable-ScheduledTask'
            $invoked | Should -Not -Contain 'Set-SideCrabPanelApprovals'
            $invoked | Should -Not -Contain 'Clear-SideCrabPanelApprovals'
            ($script:RepairText -match 'Enable-ScheduledTask -TaskName SideCrab-crabd') | Should -BeTrue
        }

        It 'Repair gates every fix behind ShouldProcess and applies none without -Fix' {
            ($script:RepairText -match '\$PSCmdlet\.ShouldProcess\(\$f\.Title, \$f\.FixLabel\)') | Should -BeTrue
            ($script:RepairText -match 'Report only - nothing was changed')                      | Should -BeTrue
            @($script:RepairAst.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) |
                Should -Contain 'Fix'
        }

        It 'Repair checks the failure modes this project actually hit' {
            foreach ($marker in 'STALE code', 'lastStatuslineAgeSec', 'allowedHttpHookUrls',
                                'schema pin', 'foreign-checkout', 'config.json') {
                ($script:RepairText -match [regex]::Escape($marker)) | Should -BeTrue
            }
        }

        It 'Repair explains every non-ok row and prints a command for it' {
            # A doctor that says "FAIL" and nothing else is a worse smoke test.
            ($script:RepairText -match 'why: \$\(\$c\.Why\)')     | Should -BeTrue
            ($script:RepairText -match 'fix: \$\(\$c\.Command\)') | Should -BeTrue
        }

        # -- SET-a1: the doctor surfaces the panel-approval posture and, when ON, verifies wiring.

        It 'Repair carries a panel-approval posture row read from config' {
            ($script:RepairText -match "Id 'panel-approvals'")            | Should -BeTrue
            ($script:RepairText -match "Title 'panel approvals'")         | Should -BeTrue
            ($script:RepairText -match 'Get-SideCrabPanelApprovalsState') | Should -BeTrue
        }

        It 'Repair reports an OFF posture as info (a valid, safe state), never a fault' {
            $block = ([regex]::Match($script:RepairText, '(?s)-- 8b\..*?-- 9\.')).Value
            ($block -match "if \(-not \`$pa\.Enabled\)")            | Should -BeTrue
            ($block -match "-Status 'info'")                        | Should -BeTrue
            ($block -match 'CANNOT decide tool permissions')        | Should -BeTrue
        }

        It 'Repair FAILS an ON posture whose PermissionRequest hook cannot reach crabd' {
            $block = ([regex]::Match($script:RepairText, '(?s)-- 8b\..*?-- 9\.')).Value
            ($block -match 'PermissionRequest')                     | Should -BeTrue
            ($block -match 'allowedHttpHookUrls|permAllowed')       | Should -BeTrue
            ($block -match 'approvals silently never arm')          | Should -BeTrue
            ($block -match "-Status 'fail'")                        | Should -BeTrue
        }

        It 'Repair verifies the /v1/hook/permission route the way Test/Verify do (404 = fail)' {
            $block = ([regex]::Match($script:RepairText, '(?s)-- 8b\..*?-- 9\.')).Value
            ($script:RepairText -match 'function Get-PermissionRouteState') | Should -BeTrue
            ($script:RepairText -match '/v1/hook/permission')              | Should -BeTrue
            ($block -match 'Get-PermissionRouteState')                     | Should -BeTrue
            ($block -match '\.Is404')                                      | Should -BeTrue
            ($block -match 'predates panel approval')                      | Should -BeTrue
        }

        It 'Repair says out loud when an ON posture is correctly armed (never invisible)' {
            $block = ([regex]::Match($script:RepairText, '(?s)-- 8b\..*?-- 9\.')).Value
            ($block -match 'taps decide real permissions') | Should -BeTrue
        }

        It 'Repair NEVER offers a -Fix that changes the approval posture' {
            # Enabling/disabling approvals is the operator's call. The row is diagnosis only.
            $block = ([regex]::Match($script:RepairText, '(?s)-- 8b\..*?-- 9\.')).Value
            ($block -match '-FixAction') | Should -BeFalse
            ($block -match '-FixLabel')  | Should -BeFalse
            # and the posture probe is read-only - no config writer anywhere in the doctor.
            $invoked = script:Get-InvokedCommand $script:RepairAst
            $invoked | Should -Not -Contain 'Set-SideCrabPanelApprovals'
            $invoked | Should -Not -Contain 'Clear-SideCrabPanelApprovals'
        }

        # -- SET-a1 mirror: Install -Status surfaces the same posture + wiring.

        It 'Install -Status surfaces the posture and, when ON, the hook wiring' {
            $ast = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast
            $def = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
                   Where-Object { $_.Name -eq 'Show-Status' } | Select-Object -First 1
            $status = $def.Body.Extent.Text
            ($status -match 'panelApprovals ENABLED')  | Should -BeTrue
            ($status -match 'PermissionRequest hook')  | Should -BeTrue
            ($status -match 'approvals never arm')     | Should -BeTrue
        }

        # -- SET-a2: Restore covers config.json backups as well as settings.json's.

        It 'Restore exposes -Config and -ConfigPath' {
            $p = @($script:RestoreAst.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            $p | Should -Contain 'Config'
            $p | Should -Contain 'ConfigPath'
        }

        It 'Restore targets config.json for list, restore and prune under -Config' {
            ($script:RestoreText -match '\$targetPath = if \(\$Config\)')     | Should -BeTrue
            ($script:RestoreText -match 'Get-SideCrabBackupFile')             | Should -BeTrue
            # config restores skip the settings-only foreign-key split and still Copy-Item back
            ($script:RestoreText -match 'if \(\$Config\)')                    | Should -BeTrue
            (script:Get-InvokedCommand $script:RestoreAst) | Should -Contain 'Copy-Item'
        }

        It 'Set/Clear route the config write through the atomic helper, not a bare Set-Content' {
            $ast = (script:Get-ScriptAst -Path $script:Common).Ast
            $defs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
            foreach ($fn in 'Set-SideCrabPanelApprovals', 'Clear-SideCrabPanelApprovals') {
                $body = ($defs | Where-Object { $_.Name -eq $fn } | Select-Object -First 1).Body.Extent.Text
                ($body -match 'Backup-SideCrabFile')      | Should -BeTrue
                ($body -match 'Write-SideCrabFileAtomic') | Should -BeTrue
                # a truncate+write is exactly what SET-a2 removes - the whole file goes through
                # the temp+rename helper now, never Set-Content on the live config.
                ($body -match 'Set-Content')              | Should -BeFalse
            }
            $atomic = ($defs | Where-Object { $_.Name -eq 'Write-SideCrabFileAtomic' } | Select-Object -First 1).Body.Extent.Text
            ($atomic -match 'sidecrab-tmp-')              | Should -BeTrue
            ($atomic -match '\[System\.IO\.File\]::Move') | Should -BeTrue
        }
    }

    Context 'doctor health retry (v0.16.0)' {

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Test-SideCrabHealthOk', 'Get-SideCrabHealthProbe'
            )
            $script:RepairText6 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1') `
                                              -Raw -Encoding utf8

            # A probe that answers from a list instead of from a socket: no request is made, no
            # port is touched, and the operator's live crabd on 9999 is never contacted.
            function script:New-Probe {
                param([object[]] $Answer)
                $calls = [pscustomobject]@{ Count = 0 }
                $list  = @($Answer)
                $block = {
                    $i = $calls.Count
                    $calls.Count++
                    if ($i -lt $list.Count) { $list[$i] } else { $list[-1] }
                }.GetNewClosure()
                [pscustomobject]@{ Block = $block; Calls = $calls }
            }
            $script:NoWait  = { param($s) }                      # the backoff, neutered
            $script:Healthy = @{ ok = $true; version = '0.15.0' }
        }

        It 'a probe that answers first time is never retried' {
            $p = script:New-Probe -Answer @($script:Healthy)
            $r = Get-SideCrabHealthProbe -Probe $p.Block -Wait $script:NoWait
            $r.Ok               | Should -BeTrue
            $r.Attempts         | Should -Be 1
            $r.RecoveredOnRetry | Should -BeFalse
            $p.Calls.Count      | Should -Be 1
        }

        It 'fail-then-succeed reads OK, and SAYS it recovered on retry' {
            # Measured live 2026-08-27: FAIL, then OK 3 s later, PID listening throughout. The
            # retry stops the false alarm; RecoveredOnRetry stops the fix from hiding a symptom.
            $p = script:New-Probe -Answer @($null, $script:Healthy)
            $r = Get-SideCrabHealthProbe -Probe $p.Block -Wait $script:NoWait
            $r.Ok               | Should -BeTrue
            $r.RecoveredOnRetry | Should -BeTrue
            $r.Attempts         | Should -Be 2
            $p.Calls.Count      | Should -Be 2
            # and the DOCUMENT handed on is the retry's, so the version/statusline checks
            # downstream read the answer that arrived rather than the handshake that did not
            $r.Document['version'] | Should -Be '0.15.0'
        }

        It 'fail-fail stays FAIL - the retry is one extra chance, not a loop' {
            $p = script:New-Probe -Answer @($null, $null)
            $r = Get-SideCrabHealthProbe -Probe $p.Block -Wait $script:NoWait
            $r.Ok               | Should -BeFalse
            $r.RecoveredOnRetry | Should -BeFalse
            $r.Attempts         | Should -Be 2
            $p.Calls.Count      | Should -Be 2
        }

        It 'a reachable-but-not-ok crabd is retried too, and still not called ok' {
            $p = script:New-Probe -Answer @(@{ ok = $false }, @{ ok = $false })
            $r = Get-SideCrabHealthProbe -Probe $p.Block -Wait $script:NoWait
            $r.Ok          | Should -BeFalse
            $p.Calls.Count | Should -Be 2
        }

        It 'the ok test is total: null, no ok field, and ok:false are all "no"' {
            Test-SideCrabHealthOk -Document $null              | Should -BeFalse
            Test-SideCrabHealthOk -Document @{ version = '1' } | Should -BeFalse
            Test-SideCrabHealthOk -Document @{ ok = $false }   | Should -BeFalse
            Test-SideCrabHealthOk -Document @{ ok = $true }    | Should -BeTrue
            Test-SideCrabHealthOk -Document ([pscustomobject]@{ ok = $true }) | Should -BeTrue
        }

        It 'the doctor probes through it, and its FAIL row admits how many attempts it made' {
            ($script:RepairText6 -match 'Get-SideCrabHealthProbe')                | Should -BeTrue
            ($script:RepairText6 -match 'HealthRetryDelaySec')                    | Should -BeTrue
            ($script:RepairText6 -match 'RECOVERED ON RETRY')                     | Should -BeTrue
            ($script:RepairText6 -match 'attempts \$\(\$healthProbe\.DelaySec\)s apart') | Should -BeTrue
        }

        It 'the backoff is injectable, so the suite never sleeps and the doctor never spins' {
            $fn = ((script:Get-ScriptAst -Path $script:Common).Ast.FindAll({
                       param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
                   }, $true) | Where-Object { $_.Name -eq 'Get-SideCrabHealthProbe' } | Select-Object -First 1)
            $fn.Extent.Text | Should -Match '\[scriptblock\] \$Wait'
            $fn.Extent.Text | Should -Match 'Start-Sleep -Seconds \$RetryDelaySec'
        }
    }

    Context 'matcher-level hook ownership (v0.16.0)' {

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Split-SideCrabHookMatcher', 'Test-SideCrabHookMatcherIsOurs',
                'Split-SideCrabSettings', 'Compare-SideCrabSettingsPair',
                'Get-SideCrabCanonicalValue', 'ConvertTo-SideCrabCanonicalJson',
                'Test-SideCrabStatusLineIsOurs'
            )
            # Lifted from the uninstaller: only the function BODY is evaluated, so nothing the
            # script does at top level (removing tasks, writing settings.json) can run here.
            script:Import-AstFunction -Path (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') `
                                      -Name @('Remove-HookEntries')
            # Remove-HookEntries reads the script-level marker of the file it came from.
            $global:HookUrlMarker = '127.0.0.1:9999/v1/hook'
            $script:UninstallText6 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') `
                                                 -Raw -Encoding utf8

            function script:OurHook   { @{ type = 'command'; command = 'curl.exe -s -m 2 -X POST -H "X-SideCrab-Panel: 1" --data-binary @- http://127.0.0.1:9999/v1/hook || exit 0' } }
            function script:TheirHook { @{ type = 'command'; command = 'echo mine, hand-merged' } }
            function script:OursOnly  { @{ matcher = '*'; hooks = @(script:OurHook) } }
            function script:TheirsOnly{ @{ matcher = '*'; hooks = @(script:TheirHook) } }
            function script:Shared    { @{ matcher = '*'; hooks = @((script:OurHook), (script:TheirHook)) } }
        }
        AfterAll {
            Remove-Variable -Name HookUrlMarker -Scope Global -ErrorAction SilentlyContinue
        }

        It 'splits a SHARED matcher into our entries and theirs' {
            $part = Split-SideCrabHookMatcher -Matcher (script:Shared)
            $part.OurCount     | Should -Be 1
            $part.ForeignCount | Should -Be 1
            @($part.Ours['hooks']).Count    | Should -Be 1
            @($part.Foreign['hooks']).Count | Should -Be 1
            $part.Foreign['hooks'][0]['command'] | Should -Match 'hand-merged'
            # every other key of the matcher rides along on BOTH halves
            $part.Ours['matcher']    | Should -Be '*'
            $part.Foreign['matcher'] | Should -Be '*'
        }

        It 'hands back the ORIGINAL object when a matcher is all ours or all theirs' {
            # The unshared paths are the common ones, and they must be byte-identical to before:
            # rebuilding them would make a restore diff report churn that never happened.
            $ours = script:OursOnly
            $p1 = Split-SideCrabHookMatcher -Matcher $ours
            [object]::ReferenceEquals($p1.Ours, $ours) | Should -BeTrue
            $p1.Foreign | Should -BeNullOrEmpty

            $theirs = script:TheirsOnly
            $p2 = Split-SideCrabHookMatcher -Matcher $theirs
            [object]::ReferenceEquals($p2.Foreign, $theirs) | Should -BeTrue
            $p2.Ours | Should -BeNullOrEmpty
        }

        It 'passes an unrecognisable matcher through as foreign rather than dropping it' {
            (Split-SideCrabHookMatcher -Matcher 'nonsense').Foreign          | Should -Be 'nonsense'
            (Split-SideCrabHookMatcher -Matcher @{ matcher = '*' }).OurCount | Should -Be 0
            (Split-SideCrabHookMatcher -Matcher @{ matcher = '*'; hooks = @() }).Ours | Should -BeNullOrEmpty
        }

        It 'uninstall removes OUR entry from a shared matcher and leaves the foreign hook in place' {
            # The finding (QA-Audit-2026-08-27, SETUP MED): matcher-level ownership deleted a
            # hook a human hand-merged into one of ours.
            $settings = @{ hooks = @{ SessionStart = @(script:Shared) } }
            (Remove-HookEntries -Settings $settings) | Should -Be 1
            @($settings['hooks']['SessionStart']).Count | Should -Be 1
            $kept = @($settings['hooks']['SessionStart'])[0]
            @($kept['hooks']).Count            | Should -Be 1
            $kept['hooks'][0]['command']       | Should -Match 'hand-merged'
            $kept['matcher']                   | Should -Be '*'
        }

        It 'drops a matcher only once it is EMPTY, and the event with it' {
            $settings = @{ hooks = @{ Stop = @(script:OursOnly); SessionStart = @(script:Shared) } }
            (Remove-HookEntries -Settings $settings) | Should -Be 2
            $settings['hooks'].ContainsKey('Stop')         | Should -BeFalse
            $settings['hooks'].ContainsKey('SessionStart') | Should -BeTrue
        }

        It 'still removes the whole hooks key when nothing of anyone else''s is left' {
            $settings = @{ hooks = @{ Stop = @(script:OursOnly) } }
            (Remove-HookEntries -Settings $settings) | Should -Be 1
            $settings.ContainsKey('hooks') | Should -BeFalse
        }

        It 'leaves a matcher that is entirely someone else''s exactly as it was' {
            $theirs   = script:TheirsOnly
            $settings = @{ hooks = @{ Stop = @($theirs) } }
            (Remove-HookEntries -Settings $settings) | Should -Be 0
            [object]::ReferenceEquals(@($settings['hooks']['Stop'])[0], $theirs) | Should -BeTrue
        }

        It 'the uninstaller reads the shared splitter rather than deciding again' {
            ($script:UninstallText6 -match 'Split-SideCrabHookMatcher') | Should -BeTrue
        }

        It 'the restore guard SEES a foreign entry hand-merged into a SideCrab matcher' {
            # Before: that hook counted as ours, so a restore that would delete it passed the
            # guard silently - the exact edit the guard exists to protect.
            $backup  = @{ theme = 'dark'; hooks = @{ SessionStart = @(script:OursOnly) } }
            $current = @{ theme = 'dark'; hooks = @{ SessionStart = @(script:Shared) } }
            $cmp = Compare-SideCrabSettingsPair -Backup $backup -Current $current
            $cmp.ForeignChanged | Should -BeTrue
            @($cmp.ForeignDiff | Where-Object { $_.Key -eq 'hooks' }).Count | Should -Be 1
            @($cmp.ForeignDiff | Where-Object { $_.Key -eq 'hooks' })[0].State | Should -Match 'DELETES'
        }

        It 'and still does NOT gate a restore that only puts SideCrab wiring back' {
            # A guard that fires on our own hooks would train the operator to pass -Force by
            # reflex, which is worse than no guard.
            $cmp = Compare-SideCrabSettingsPair -Backup @{ theme = 'dark' } `
                                                -Current @{ theme = 'dark'; hooks = @{ SessionStart = @(script:OursOnly) } }
            $cmp.ForeignChanged | Should -BeFalse
        }

        It 'classifies a shared matcher into BOTH halves of the split' {
            $split = Split-SideCrabSettings -Settings @{ hooks = @{ Stop = @(script:Shared) } }
            @($split.Ours['hooks']['Stop']).Count    | Should -Be 1
            @($split.Foreign['hooks']['Stop']).Count | Should -Be 1
        }

        It 'leaves the unshared split byte-identical to before the change' {
            $theirs = script:TheirsOnly
            $ours   = script:OursOnly
            $split  = Split-SideCrabSettings -Settings @{ hooks = @{ Stop = @($theirs, $ours) } }
            (ConvertTo-SideCrabCanonicalJson -Value $split.Foreign['hooks']['Stop']) |
                Should -Be (ConvertTo-SideCrabCanonicalJson -Value @($theirs))
            (ConvertTo-SideCrabCanonicalJson -Value $split.Ours['hooks']['Stop']) |
                Should -Be (ConvertTo-SideCrabCanonicalJson -Value @($ours))
        }
    }

    Context 'entry-level hook ownership: re-install and the doctor' {

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Split-SideCrabHookMatcher', 'ConvertTo-SideCrabCanonicalJson',
                'Get-SideCrabCanonicalValue', 'Get-SideCrabCommandPath',
                'Get-SideCrabPathOwnership'
            )
            # Function bodies only - nothing either script does at top level runs here.
            script:Import-AstFunction -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1') `
                                      -Name @('Merge-HookFragment')
            script:Import-AstFunction -Path (Join-Path $script:SetupDir 'Repair-SideCrab.ps1') `
                                      -Name @('Get-HookWiringPath')
            # Both bodies read the script-level marker of the file they came from.
            $global:HookUrlMarker = '127.0.0.1:9999/v1/hook'
            $script:InstallText7  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1') -Raw -Encoding utf8
            $script:RepairText7   = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1')  -Raw -Encoding utf8

            function script:OurHook7   { @{ type = 'command'; command = 'curl.exe -s -m 2 -X POST -H "X-SideCrab-Panel: 1" --data-binary @- http://127.0.0.1:9999/v1/hook || exit 0'; timeout = 3 } }
            function script:TheirHook7 { @{ type = 'command'; command = 'echo mine, hand-merged' } }
            function script:Fragment7  { @{ SessionStart = @(@{ hooks = @(script:OurHook7) }) } }
        }
        AfterAll {
            Remove-Variable -Name HookUrlMarker -Scope Global -ErrorAction SilentlyContinue
        }

        It 'a RE-INSTALL keeps a foreign hook hand-merged into a SideCrab matcher' {
            # The defect: the prior matcher was dropped WHOLE, so every re-install ate the
            # operator's hook - the same hole uninstall and the restore guard were fixed for.
            $settings = @{ hooks = @{ SessionStart = @(@{ matcher = '*'; hooks = @((script:OurHook7), (script:TheirHook7)) }) } }
            Merge-HookFragment -Settings $settings -Fragment (script:Fragment7) | Out-Null

            $kept = @($settings['hooks']['SessionStart'])
            $kept.Count | Should -Be 2                       # their half, then our fresh matcher
            @($kept[0]['hooks']).Count      | Should -Be 1
            $kept[0]['hooks'][0]['command'] | Should -Match 'hand-merged'
            $kept[0]['matcher']             | Should -Be '*' # the matcher's other keys ride along
            # and exactly one SideCrab entry survives the merge, not two
            @($kept | ForEach-Object { $_['hooks'] } | Where-Object { "$($_['command'])" -like "*$global:HookUrlMarker*" }).Count |
                Should -Be 1
        }

        It 'still DEDUPLICATES our own entries, so a second run does not double the hooks' {
            $settings = @{ hooks = @{} }
            Merge-HookFragment -Settings $settings -Fragment (script:Fragment7) | Out-Null
            Merge-HookFragment -Settings $settings -Fragment (script:Fragment7) | Out-Null
            Merge-HookFragment -Settings $settings -Fragment (script:Fragment7) | Out-Null
            @($settings['hooks']['SessionStart']).Count | Should -Be 1
        }

        It 'leaves the unshared re-install path byte-identical to before the change' {
            # The common path: a foreign matcher survives untouched (same object), our own prior
            # matcher is replaced by the fragment's. Canonical JSON is the comparison form.
            $theirs   = @{ matcher = 'Bash'; hooks = @(script:TheirHook7) }
            $settings = @{ hooks = @{ SessionStart = @($theirs, @{ hooks = @(script:OurHook7) }) } }
            Merge-HookFragment -Settings $settings -Fragment (script:Fragment7) | Out-Null

            $kept = @($settings['hooks']['SessionStart'])
            [object]::ReferenceEquals($kept[0], $theirs) | Should -BeTrue
            (ConvertTo-SideCrabCanonicalJson -Value $kept) |
                Should -Be (ConvertTo-SideCrabCanonicalJson -Value @($theirs, @{ hooks = @(script:OurHook7) }))
        }

        It 'the installer reads the shared splitter rather than deciding again' {
            ($script:InstallText7 -match 'Split-SideCrabHookMatcher') | Should -BeTrue
            ($script:RepairText7  -match 'Split-SideCrabHookMatcher') | Should -BeTrue
        }

        It 'the doctor does NOT attribute a foreign entry''s path to SideCrab wiring' {
            # Matcher-level attribution swept the whole matcher, so a hand-merged hook naming
            # another checkout was reported as OUR stray wiring - and the offered fix (re-run
            # the installer) does not own that hook and would not move it.
            $settings = @{ hooks = @{ Stop = @(@{ matcher = '*'; hooks = @(
                            (script:OurHook7),
                            @{ type = 'command'; command = 'pwsh -File "C:\Other\sidecrab\hooks\Their-Own.ps1"' }
                        ) }) } }
            $paths = @(Get-HookWiringPath -Settings $settings -Marker $global:HookUrlMarker)
            @($paths | Where-Object { $_.Path -match 'Their-Own' }).Count | Should -Be 0
            # ...and it was genuinely a foreign-checkout path, i.e. the test can fail
            (Get-SideCrabPathOwnership -Path 'C:\Other\sidecrab\hooks\Their-Own.ps1' -RepoRoot 'C:\Dev\sidecrab') |
                Should -Be 'foreign-checkout'
        }

        It 'but a genuinely stray SideCrab entry IS still flagged, shared matcher or not' {
            $stray = @{ type = 'command'; command = 'pwsh -File "C:\Other\sidecrab\hooks\Send-Hook.ps1" -Url http://127.0.0.1:9999/v1/hook' }
            foreach ($matcher in @(@{ matcher = '*'; hooks = @($stray) },
                                   @{ matcher = '*'; hooks = @($stray, (script:TheirHook7)) })) {
                $paths = @(Get-HookWiringPath -Settings @{ hooks = @{ Stop = @($matcher) } } -Marker $global:HookUrlMarker)
                @($paths | Where-Object { $_.Path -eq 'C:\Other\sidecrab\hooks\Send-Hook.ps1' }).Count | Should -Be 1
                $paths[0].Source | Should -Be 'settings.json hooks/Stop'
            }
        }

        It 'returns an EMPTY set, not a one-element one, on every no-result path' {
            # A `, $found` return here reads as one empty array to @(), so 'nothing found'
            # would count as 1 and the doctor would report a path it never saw.
            @(Get-HookWiringPath -Settings @{ theme = 'dark' }).Count | Should -Be 0
            @(Get-HookWiringPath -Settings $null).Count               | Should -Be 0
            # hooks present, none of them ours, and none of ours carrying a path
            @(Get-HookWiringPath -Settings @{ hooks = @{ Stop = @(@{ hooks = @(script:TheirHook7) }) } }).Count | Should -Be 0
            @(Get-HookWiringPath -Settings @{ hooks = @{ Stop = @(@{ hooks = @(script:OurHook7) }) } }).Count | Should -Be 0
        }
    }

    Context 'the restart port race (v0.20.0)' {

        # THE INCIDENT, measured live 2026-08-27: Update-SideCrab.ps1 -SkipPull restarted
        # SideCrab-crabd; the NEW instance lost the bind race against the not-yet-dead OLD
        # process, exited 1, and the task sat in Ready with LastTaskResult=1 - panel dark ~6
        # minutes. Nothing below opens a socket or touches the scheduler: the port poll, the
        # task-state read and the process lookup are all injected, so the operator's live
        # crabd on 9999 is never contacted and no task is ever stopped or started.

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Get-SideCrabPortHolder', 'Format-SideCrabPortHolder',
                'Wait-SideCrabPortRelease', 'Restart-SideCrabTask',
                'Get-SideCrabServiceVerdict'
            )
            $script:CommonAst8 = (script:Get-ScriptAst -Path $script:Common).Ast
            $script:UpdateAst8 = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Update-SideCrab.ps1')).Ast
            $script:RepairAst8 = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Repair-SideCrab.ps1')).Ast
            $script:UpdateText8 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Update-SideCrab.ps1') -Raw -Encoding utf8
            $script:RepairText8 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1')  -Raw -Encoding utf8

            $script:Holder8 = [pscustomobject]@{
                Port = 9999; ProcessId = 4242; ProcessName = 'pythonw'; Path = 'C:\Py\pythonw.exe'
            }

            # A port poll that answers from a LIST instead of from the kernel. Each element is
            # one reading: $null = the port is free, a row = that process still holds it.
            function script:New-HolderProbe {
                param([object[]] $Answer)
                $calls = [pscustomobject]@{ Count = 0 }
                $list  = @($Answer)
                $block = {
                    param($p)
                    $i = $calls.Count
                    $calls.Count++
                    $item = if ($i -lt $list.Count) { $list[$i] } else { $list[-1] }
                    if ($null -eq $item) { @() } else { @($item) }
                }.GetNewClosure()
                [pscustomobject]@{ Block = $block; Calls = $calls }
            }

            # A scheduler that only counts. Nothing is stopped, started or read for real.
            function script:New-FakeScheduler {
                param([string[]] $State = @('Ready'))
                $log  = [System.Collections.Generic.List[string]]::new()
                $seq  = @($State)
                $reads = [pscustomobject]@{ Count = 0 }
                [pscustomobject]@{
                    Log   = $log
                    Reads = $reads
                    Stop  = { param($n) $log.Add("stop:$n") }.GetNewClosure()
                    Start = { param($n) $log.Add("start:$n") }.GetNewClosure()
                    State = {
                        param($n)
                        $i = $reads.Count
                        $reads.Count++
                        $log.Add("state:$n")
                        if ($i -lt $seq.Count) { $seq[$i] } else { $seq[-1] }
                    }.GetNewClosure()
                }
            }
            $script:NoWait8 = { param($s) }        # the backoff, neutered - the suite never sleeps

            # Re-declared rather than borrowed from the earlier context: a helper this block
            # depends on must not be hostage to another Context's fixtures still being in scope.
            function script:Get-InvokedCommand {
                param($Ast)
                @($Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) |
                  ForEach-Object { $_.GetCommandName() } |
                  Where-Object { $_ } | Sort-Object -Unique)
            }
        }

        It 'the catalogue says WHICH component owns a port, so no caller has to guess' {
            (script:One $script:Spec 'crabd').Port | Should -Be 9999
            (script:One $script:Spec 'glow').Port  | Should -Be 0
            (script:One $script:Spec 'toast').Port | Should -Be 0
        }

        It 'a plan row carries the port through the selection step' {
            # A plan that dropped Port would send the installer/updater back to guessing.
            (script:One (script:Plan -Present @{ crabd = $true }) 'crabd').Port | Should -Be 9999
        }

        It 'names the PID and process holding the port - health cannot tell WHO answered' {
            $rows = @(Get-SideCrabPortHolder -Port 9999 `
                        -Probe         { param($p) @([pscustomobject]@{ OwningProcess = 4242 }) } `
                        -ProcessLookup { param($id) [pscustomobject]@{ ProcessName = 'pythonw'; Path = 'C:\Py\pythonw.exe' } })
            $rows.Count            | Should -Be 1
            $rows[0].ProcessId     | Should -Be 4242
            $rows[0].ProcessName   | Should -Be 'pythonw'
            (Format-SideCrabPortHolder -Holder $rows -Port 9999) | Should -Match 'PID 4242 \(pythonw'
        }

        It 'a free port is an EMPTY set, not a one-element one' {
            # @(one empty array) counts as 1, which would read as "something is holding it"
            # and hang every restart on this workstation for the full timeout.
            @(Get-SideCrabPortHolder -Port 9999 -Probe { param($p) @() }).Count | Should -Be 0
            (Format-SideCrabPortHolder -Holder @() -Port 9999) | Should -Match 'no listener found'
        }

        It 'a probe that throws reports "free", loudly documented, rather than crashing a restart' {
            # NetTCPIP absent or locked down: fails OPEN on purpose - the alternative is a host
            # where no SideCrab restart can ever run. Pinned so the choice stays deliberate.
            @(Get-SideCrabPortHolder -Port 9999 -Probe { param($p) throw 'no NetTCPIP here' }).Count | Should -Be 0
        }

        It 'an already-free port is not waited on at all' {
            $p = script:New-HolderProbe -Answer @($null)
            $r = Wait-SideCrabPortRelease -Port 9999 -HolderProbe $p.Block -Wait $script:NoWait8
            $r.Released    | Should -BeTrue
            $r.Attempts    | Should -Be 1
            $r.WaitedSec   | Should -Be 0
            $p.Calls.Count | Should -Be 1
        }

        It 'held-then-released: it waits, sees the port free, and says how long it took' {
            $p = script:New-HolderProbe -Answer @($script:Holder8, $script:Holder8, $null)
            $r = Wait-SideCrabPortRelease -Port 9999 -PollIntervalSec 0.5 `
                                          -HolderProbe $p.Block -Wait $script:NoWait8
            $r.Released    | Should -BeTrue
            $r.Attempts    | Should -Be 3
            $r.WaitedSec   | Should -Be 1        # two backoffs of 0.5 s
            $r.Reason      | Should -Match 'freed after'
            @($r.Holder).Count | Should -Be 0
        }

        It 'never released: it gives up inside the budget and names the PID still holding it' {
            $p = script:New-HolderProbe -Answer @($script:Holder8)
            $r = Wait-SideCrabPortRelease -Port 9999 -TimeoutSec 10 -PollIntervalSec 0.5 `
                                          -HolderProbe $p.Block -Wait $script:NoWait8
            $r.Released         | Should -BeFalse
            $r.Attempts         | Should -Be 20        # ceil(10 / 0.5) - a BUDGET, not a spin
            $p.Calls.Count      | Should -Be 20
            @($r.Holder)[0].ProcessId | Should -Be 4242
            $r.Reason           | Should -Match 'still held'
            $r.Reason           | Should -Match 'PID 4242'
        }

        It 'the budget is counted in polls, so an injected sleep can never spin forever' {
            # A wall-clock deadline under a neutered sleep either loops for ever or gives up
            # after one attempt; both make this untestable, which is how the race survived.
            $fn = ($script:CommonAst8.FindAll({
                       param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
                   }, $true) | Where-Object { $_.Name -eq 'Wait-SideCrabPortRelease' } | Select-Object -First 1)
            $fn.Extent.Text | Should -Match '\[scriptblock\] \$Wait'
            $fn.Extent.Text | Should -Match 'Start-Sleep -Seconds \$PollIntervalSec'
            $fn.Extent.Text | Should -Not -Match 'Get-Date'
        }

        It 'the clean restart stops, waits for the task to go, waits for the port, then starts' {
            $s = script:New-FakeScheduler -State @('Running', 'Ready')
            $p = script:New-HolderProbe -Answer @($script:Holder8, $null)
            $r = Restart-SideCrabTask -TaskName 'SideCrab-crabd' -Port 9999 `
                                      -StopTask $s.Stop -StartTask $s.Start -StateProbe $s.State `
                                      -HolderProbe $p.Block -Wait $script:NoWait8
            $r.Started      | Should -BeTrue
            $r.PortReleased | Should -BeTrue
            $r.LeftRunning  | Should -BeTrue
            # order is the whole point: stop, THEN read state, THEN start
            ($s.Log -join ',') | Should -Be 'stop:SideCrab-crabd,state:SideCrab-crabd,state:SideCrab-crabd,start:SideCrab-crabd'
            $p.Calls.Count  | Should -Be 2
        }

        It 'a port that never frees THROWS and the task is NEVER started' {
            # The fix, in one assertion. Starting blind is what produced a Ready task with
            # LastTaskResult=1 and a dark panel: the restart "succeeded" and served nothing.
            $s = script:New-FakeScheduler -State @('Ready')
            $p = script:New-HolderProbe -Answer @($script:Holder8)
            $message = ''
            try {
                Restart-SideCrabTask -TaskName 'SideCrab-crabd' -Port 9999 -PortWaitSec 2 -PollIntervalSec 1 `
                                     -StopTask $s.Stop -StartTask $s.Start -StateProbe $s.State `
                                     -HolderProbe $p.Block -Wait $script:NoWait8 | Out-Null
            } catch { $message = "$($_.Exception.Message)" }

            ($s.Log -contains 'start:SideCrab-crabd') | Should -BeFalse
            $message | Should -Match 'NOT restarted'
            $message | Should -Match 'PID 4242'
            $message | Should -Match 'Stop-Process'
        }

        It 'a component that owns no port is never made to wait on one' {
            # glow and toast bind nothing; a port poll for them would be 10 s of pure delay.
            $s = script:New-FakeScheduler -State @('Ready')
            $p = script:New-HolderProbe -Answer @($script:Holder8)
            $r = Restart-SideCrabTask -TaskName 'SideCrab-glow' -Port 0 `
                                      -StopTask $s.Stop -StartTask $s.Start -StateProbe $s.State `
                                      -HolderProbe $p.Block -Wait $script:NoWait8
            $r.Started      | Should -BeTrue
            $r.PortReleased | Should -BeNullOrEmpty      # not asked, not guessed
            $p.Calls.Count  | Should -Be 0
            ($s.Log -contains 'start:SideCrab-glow') | Should -BeTrue
        }

        It 'a task that will not leave Running still stops at its budget rather than hanging' {
            $s = script:New-FakeScheduler -State @('Running')
            $p = script:New-HolderProbe -Answer @($null)
            $r = Restart-SideCrabTask -TaskName 'SideCrab-crabd' -Port 9999 -StopWaitSec 2 -PollIntervalSec 1 `
                                      -StopTask $s.Stop -StartTask $s.Start -StateProbe $s.State `
                                      -HolderProbe $p.Block -Wait $script:NoWait8
            $r.LeftRunning | Should -BeFalse
            $s.Reads.Count | Should -Be 2          # ceil(2 / 1), then the port has the last word
            $r.Started     | Should -BeTrue        # the port WAS free - that is the binding test
        }

        It 'answering AND Running is the only "ok"' {
            $v = Get-SideCrabServiceVerdict -HealthOk $true -TaskState 'Running' -Port 9999
            $v.Verdict | Should -Be 'ok'
            $v.Ok      | Should -BeTrue
        }

        It 'an answer with the task NOT Running is a FAIL naming the foreign holder' {
            # Measured 2026-08-27: a stray process answered /v1/health convincingly while
            # SideCrab-crabd was dead in Ready. Health alone called that green.
            $v = Get-SideCrabServiceVerdict -HealthOk $true -TaskState 'Ready' -LastTaskResult 1 `
                                            -Holder @($script:Holder8) -Port 9999
            $v.Verdict | Should -Be 'foreign-answerer'
            $v.Ok      | Should -BeFalse
            $v.Reason  | Should -Match 'PID 4242'
            $v.Reason  | Should -Match '0x00000001'
        }

        It 'Running with no answer stays the ordinary retry case, not a foreign process' {
            $v = Get-SideCrabServiceVerdict -HealthOk $false -TaskState 'Running' -Port 9999
            $v.Verdict | Should -Be 'not-answering'
            $v.Ok      | Should -BeFalse
            $v.Reason  | Should -Not -Match 'foreign'
        }

        It 'neither answering nor Running is "down", and it reports the last task result' {
            $v = Get-SideCrabServiceVerdict -HealthOk $false -TaskState 'Ready' -LastTaskResult 1 -Port 9999
            $v.Verdict | Should -Be 'down'
            $v.Reason  | Should -Match '0x00000001'
            $unreg = Get-SideCrabServiceVerdict -HealthOk $false -TaskState '' -Port 9999
            $unreg.Reason | Should -Match 'not registered'
        }

        It 'there is exactly ONE restart path - neither script keeps a copy of its own' {
            # Two copies is how the race outlived the first fix: Update and the doctor each had
            # their own stop/wait/start, and only one of them would ever have been corrected.
            foreach ($a in $script:UpdateAst8, $script:RepairAst8) {
                $defs = @($a.FindAll({
                             param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]
                         }, $true) | Where-Object { $_.Name -match 'Restart-SideCrab' })
                $defs.Count | Should -Be 0
            }
            ($script:UpdateText8 -match 'Restart-SideCrabTask -TaskName \$s\.TaskName -Port \$port') | Should -BeTrue
            ($script:RepairText8 -match "Restart-SideCrabTask -TaskName '\`$taskName' -Port \`$taskPort") | Should -BeTrue
        }

        It 'neither script polls the port by hand - both read the shared helper' {
            # Get-NetTCPConnection appears in both as a printed SUGGESTION; invoking it would be
            # a second, differently-behaved reading of the same fact.
            foreach ($a in $script:UpdateAst8, $script:RepairAst8) {
                (script:Get-InvokedCommand $a) | Should -Not -Contain 'Get-NetTCPConnection'
            }
            ($script:UpdateText8 -match 'Get-SideCrabPortHolder') | Should -BeTrue
            ($script:RepairText8 -match 'Get-SideCrabPortHolder') | Should -BeTrue
        }

        It 'the updater verifies the TASK as well as the health answer, and exits non-zero' {
            ($script:UpdateText8 -match 'Get-SideCrabServiceVerdict')     | Should -BeTrue
            ($script:UpdateText8 -match 'Get-SideCrabTaskState -TaskName \$crabd\.TaskName') | Should -BeTrue
            ($script:UpdateText8 -match 'foreign-answerer')               | Should -BeTrue
            ($script:UpdateText8 -match 'exit \(\[int\] \$verifyFailed\)') | Should -BeTrue
        }

        It 'the updater reuses the health-probe retry rather than growing a second one' {
            ($script:UpdateText8 -match 'Get-SideCrabHealthProbe') | Should -BeTrue
            # the old hand-rolled poll, with its own deadline and its own sleep, is gone
            ($script:UpdateText8 -match 'Start-Sleep -Milliseconds 500') | Should -BeFalse
        }

        It 'the doctor carries the four-way row and still refuses to kill anything' {
            ($script:RepairText8 -match 'crabd owns its port')     | Should -BeTrue
            ($script:RepairText8 -match "Id 'port-owner'")         | Should -BeTrue
            ($script:RepairText8 -match 'Get-SideCrabServiceVerdict') | Should -BeTrue
            # -Fix may start a task; stopping an unidentified process is the operator's call.
            (script:Get-InvokedCommand $script:RepairAst8) | Should -Not -Contain 'Stop-Process'
        }

        It 'the doctor reports the wrong-answerer as its OWN fail and defers the other two' {
            # One fault must not read as two FAILs: "nothing answers" is the health row's story.
            $row = ([regex]::Match($script:RepairText8, "(?s)-- 1b\..*?-- 2\.")).Value
            ($row -match "\`$owner\.Verdict -eq 'foreign-answerer'") | Should -BeTrue
            ($row -match "-Status 'fail'")                          | Should -BeTrue
            ($row -match "-Status 'warn'")                          | Should -BeTrue
            ($row -match "see the 'crabd answering' row")           | Should -BeTrue
        }
    }

    Context 'targeted uninstall, ownership and effective repair (v0.20.1)' {

        # The QA-audit wave: CD-02/03 (an uninstall that removed more than it was asked to, and
        # restored over a status line that was not ours), CD-17/18/19/20/21/24/25 and the
        # backlog carry-over. Nothing here touches a Scheduled Task, the registry, the network
        # or the real ~/.claude - the decisions are pure and lifted by AST; the two filesystem
        # helpers get temp files of their own.

        BeforeAll {
            script:Import-AstFunction -Path $script:Common -Name @(
                'Get-SideCrabUninstallScope', 'Get-SideCrabStatusLineRestoreDecision',
                'Get-SideCrabPullPreflight',  'Get-SideCrabGlowPreflight',
                'Get-SideCrabAumidIconDecision', 'Get-SideCrabRunStateDecision',
                'Get-SideCrabWatchedWriteTime'
            )
            $script:InstallText9 = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Install-SideCrab.ps1')   -Raw -Encoding utf8
            $script:UninstText9  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Uninstall-SideCrab.ps1') -Raw -Encoding utf8
            $script:UpdateText9  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Update-SideCrab.ps1')    -Raw -Encoding utf8
            $script:RepairText9  = Get-Content -LiteralPath (Join-Path $script:SetupDir 'Repair-SideCrab.ps1')    -Raw -Encoding utf8
            $script:CommonText9  = Get-Content -LiteralPath $script:Common -Raw -Encoding utf8
            $script:InstallAst9  = (script:Get-ScriptAst -Path (Join-Path $script:SetupDir 'Install-SideCrab.ps1')).Ast

            function script:Get-ParamNames9 {
                param($Ast)
                @($Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
            }
            $script:TempDir9 = Join-Path ([IO.Path]::GetTempPath()) "sidecrab-tests-$([guid]::NewGuid().ToString('N'))"
            New-Item -ItemType Directory -Force -Path $script:TempDir9 | Out-Null
        }

        AfterAll {
            if ($script:TempDir9 -and (Test-Path -LiteralPath $script:TempDir9)) {
                Remove-Item -LiteralPath $script:TempDir9 -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        # ---- CD-02: a narrowed uninstall removes only the named component's surface --------

        It 'a full sweep owns every surface' {
            $s = Get-SideCrabUninstallScope -Spec $script:Spec
            $s.Narrowed | Should -BeFalse
            foreach ($f in 'Tasks', 'Aumid', 'Protocol', 'Hooks', 'StatusLine', 'Approvals') {
                $s.$f | Should -BeTrue
            }
        }

        It '-TaskName SideCrab-glow removes the glow task and NOTHING else' {
            # THE DEFECT: this narrowed the task deletion only, then went on to strip the hooks,
            # restore the status line and clear panelApprovals of a crabd install nobody asked
            # about. Every one of these five must be false.
            $s = Get-SideCrabUninstallScope -Spec $script:Spec -TaskName 'SideCrab-glow'
            $s.Narrowed     | Should -BeTrue
            $s.ComponentKey | Should -Be 'glow'
            $s.Tasks        | Should -BeTrue
            $s.Hooks        | Should -BeFalse
            $s.StatusLine   | Should -BeFalse
            $s.Approvals    | Should -BeFalse
            $s.Aumid        | Should -BeFalse
            $s.Protocol     | Should -BeFalse
        }

        It '-TaskName SideCrab-toast owns the two HKCU registrations and not the CLI wiring' {
            $s = Get-SideCrabUninstallScope -Spec $script:Spec -TaskName 'SideCrab-toast'
            $s.Aumid      | Should -BeTrue
            $s.Protocol   | Should -BeTrue
            $s.Hooks      | Should -BeFalse
            $s.StatusLine | Should -BeFalse
            $s.Approvals  | Should -BeFalse
        }

        It '-TaskName SideCrab-crabd owns the hooks, the status line and approvals - not the toast keys' {
            $s = Get-SideCrabUninstallScope -Spec $script:Spec -TaskName 'SideCrab-crabd'
            $s.Hooks      | Should -BeTrue
            $s.StatusLine | Should -BeTrue
            $s.Approvals  | Should -BeTrue
            $s.Aumid      | Should -BeFalse
            $s.Protocol   | Should -BeFalse
        }

        It 'a -TaskName the catalogue does not know owns nothing beyond that task' {
            # Guessing what an unrecognised name implies is how a targeted uninstall becomes a
            # full one.
            $s = Get-SideCrabUninstallScope -Spec $script:Spec -TaskName 'SideCrab-something-else'
            $s.UnknownTask | Should -BeTrue
            $s.Tasks       | Should -BeTrue
            foreach ($f in 'Aumid', 'Protocol', 'Hooks', 'StatusLine', 'Approvals') {
                $s.$f | Should -BeFalse
            }
        }

        It 'the uninstaller reads the scope table for every surface, not just the task' {
            ($script:UninstText9 -match 'Get-SideCrabUninstallScope') | Should -BeTrue
            foreach ($f in '\$scope\.Hooks', '\$scope\.StatusLine', '\$scope\.Approvals',
                           '\$scope\.Aumid', '\$scope\.Protocol') {
                ($script:UninstText9 -match $f) | Should -BeTrue
            }
        }

        # ---- CD-03: never restore over a status line that is not ours ---------------------

        It 'a foreign status line installed after us is PRESERVED, not overwritten' {
            # Install SideCrab, then install status line B, then uninstall: B used to be
            # silently replaced by the line SideCrab displaced months earlier.
            $d = Get-SideCrabStatusLineRestoreDecision -CurrentCommand 'python other_statusline.py' `
                     -CurrentIsOurs $false -SavedPresent $true `
                     -SavedStatusLine @{ type = 'command'; command = 'the-old-one' }
            $d.Action  | Should -Be 'preserve-foreign'
            $d.Changed | Should -BeFalse
        }

        It 'a foreign status line is preserved even when NO prior was saved' {
            # The worse half: with a null prior the old code called Remove('statusLine') and
            # deleted somebody else's line outright rather than merely overwriting it.
            $d = Get-SideCrabStatusLineRestoreDecision -CurrentCommand 'python other_statusline.py' `
                     -CurrentIsOurs $false -SavedPresent $true -SavedStatusLine $null
            $d.Action  | Should -Be 'preserve-foreign'
            $d.Changed | Should -BeFalse
        }

        It 'our own status line is still restored to the saved prior' {
            $d = Get-SideCrabStatusLineRestoreDecision -CurrentCommand 'python sidecrab_statusline.py' `
                     -CurrentIsOurs $true -SavedPresent $true `
                     -SavedStatusLine @{ type = 'command'; command = 'the-old-one' }
            $d.Action  | Should -Be 'restore'
            $d.Changed | Should -BeTrue
        }

        It 'ours with no prior saved is removed, and the slot goes back to empty' {
            $ours = Get-SideCrabStatusLineRestoreDecision -CurrentCommand 'python sidecrab_statusline.py' `
                        -CurrentIsOurs $true -SavedPresent $true -SavedStatusLine $null
            $ours.Action | Should -Be 'remove'
            $noChain = Get-SideCrabStatusLineRestoreDecision -CurrentCommand 'python sidecrab_statusline.py' `
                           -CurrentIsOurs $true -SavedPresent $false -SavedStatusLine $null
            $noChain.Action | Should -Be 'remove'
        }

        It 'an EMPTY slot is not a foreign one - the saved prior goes back in' {
            $d = Get-SideCrabStatusLineRestoreDecision -CurrentCommand '' -CurrentIsOurs $false `
                     -SavedPresent $true -SavedStatusLine @{ type = 'command'; command = 'the-old-one' }
            $d.Action | Should -Be 'restore'
        }

        It 'nothing ours, nothing saved, nothing done' {
            (Get-SideCrabStatusLineRestoreDecision -CurrentCommand '' -CurrentIsOurs $false `
                 -SavedPresent $false -SavedStatusLine $null).Action | Should -Be 'none'
        }

        It 'the uninstaller carries out the decision instead of writing over the slot itself' {
            ($script:UninstText9 -match 'Get-SideCrabStatusLineRestoreDecision') | Should -BeTrue
            ($script:UninstText9 -match "'preserve-foreign'")                    | Should -BeTrue
            # the unconditional overwrite is gone: the assignment now lives under the restore arm
            ($script:UninstText9 -match "(?s)'restore'\s*\{\s*\r?\n\s*\`$settings\['statusLine'\] = \`$saved\.StatusLine") | Should -BeTrue
        }

        # ---- CD-17: the dirty-tree promise is now kept ------------------------------------

        It 'a tracked modification BLOCKS the pull' {
            # Measured on this very tree 2026-08-27: " M README.md" standing, and --ff-only
            # would have fast-forwarded straight over it - no incoming commit touches README.md.
            $t = Get-SideCrabPullPreflight -StatusPorcelain " M README.md"
            $t.Clean        | Should -BeFalse
            $t.Blocked      | Should -BeTrue
            $t.TrackedCount | Should -Be 1
            $t.Changed[0]   | Should -Be 'README.md'
            $t.Reason       | Should -Match 'README\.md'
        }

        It 'staged, deleted and renamed files all count as tracked changes' {
            $t = Get-SideCrabPullPreflight -StatusPorcelain "M  setup/a.ps1`n D setup/b.ps1`nR  c -> d"
            $t.Blocked      | Should -BeTrue
            $t.TrackedCount | Should -Be 3
        }

        It 'untracked files WARN and do not block' {
            # A stray log file must not make the updater unusable: --ff-only leaves untracked
            # files alone unless an incoming commit adds that exact path, and says so by name.
            $t = Get-SideCrabPullPreflight -StatusPorcelain "?? notes.txt`n?? lighting/scratch.py"
            $t.Blocked        | Should -BeFalse
            $t.Clean          | Should -BeFalse
            $t.Untracked.Count | Should -Be 2
            $t.Untracked[0]   | Should -Be 'notes.txt'
        }

        It 'a genuinely clean tree is clean' {
            $t = Get-SideCrabPullPreflight -StatusPorcelain ''
            $t.Clean   | Should -BeTrue
            $t.Blocked | Should -BeFalse
            $t.Reason  | Should -Be 'clean'
        }

        It 'the updater asks BEFORE it pulls, and throws without restarting anything' {
            ($script:UpdateText9 -match "'status', '--porcelain'")   | Should -BeTrue
            ($script:UpdateText9 -match 'Get-SideCrabPullPreflight') | Should -BeTrue
            ($script:UpdateText9 -match 'No pull was attempted')     | Should -BeTrue
            # the preflight must precede the pull in the file, or it is not a preflight
            $preflight = $script:UpdateText9.IndexOf('Get-SideCrabPullPreflight')
            $pull      = $script:UpdateText9.IndexOf("'pull', '--ff-only'")
            ($preflight -lt $pull) | Should -BeTrue
        }

        # ---- CD-05 residual: a MISSING registration is reported, not omitted --------------

        It 'the updater names a scheme that is not registered at all' {
            # Stale had a row and missing had none, so an absent scheme produced NO output and a
            # silent report read as a clean one - which is how Snooze shipped inert.
            ($script:UpdateText9 -match 'NOT REGISTERED')     | Should -BeTrue
            ($script:UpdateText9 -match '\$toastRegistered')  | Should -BeTrue
            # both loops: the AUMID had the identical hole
            (@([regex]::Matches($script:UpdateText9, 'NOT REGISTERED')).Count -ge 2) | Should -BeTrue
        }

        # ---- CD-18: the -TaskName footgun is gone from the installer ----------------------

        It 'the installer exposes NO task-name override' {
            # It renamed the crabd task alone, and Update/Repair/Test all discover by the
            # catalogue's names - so all it could ever produce was an install the rest of the
            # toolchain could not see.
            (script:Get-ParamNames9 $script:InstallAst9) | Should -Not -Contain 'TaskName'
            ($script:InstallText9 -match 'CrabdTaskName') | Should -BeFalse
        }

        It 'SideCrab is single-instance by construction, which is why the override was a lie' {
            # One fixed port in the catalogue: a second install could never have run beside the
            # first no matter what its task was called.
            @($script:Spec | Where-Object { [int] $_.Port -gt 0 }).Count | Should -Be 1
            (script:One $script:Spec 'crabd').Port | Should -Be 9999
        }

        # ---- CD-19: a stopped helper task is a FAIL, not a green row ----------------------

        It 'a registered, enabled, not-Running logon daemon is a fault' {
            $r = Get-SideCrabRunStateDecision -Registered $true -State 'Ready'
            $r.Fault   | Should -BeTrue
            $r.Verdict | Should -Be 'stopped'
            $r.Reason  | Should -Match 'not Running'
        }

        It 'Running is fine, and disabled/unregistered are states rather than faults' {
            (Get-SideCrabRunStateDecision -Registered $true  -State 'Running').Fault  | Should -BeFalse
            # a disabled task is a stated decision (the glow is parked, docs/BACKLOG.md)
            (Get-SideCrabRunStateDecision -Registered $true  -State 'Disabled').Fault | Should -BeFalse
            (Get-SideCrabRunStateDecision -Registered $false -State '').Fault         | Should -BeFalse
        }

        It 'the doctor rows the helpers'' run state and excludes crabd from it' {
            ($script:RepairText9 -match 'Get-SideCrabRunStateDecision') | Should -BeTrue
            ($script:RepairText9 -match "running-\`$\(\`$c\.Key\)")     | Should -BeTrue
            # crabd's liveness is the health + port-owner rows; a third row would make one
            # fault read as three.
            ($script:RepairText9 -match "\`$_\.Key -ne 'crabd'")        | Should -BeTrue
        }

        It 'the freshness row no longer calls "nothing is executing" an OK' {
            ($script:RepairText9 -match "\`$verdict\.Verdict -eq 'not-running'") | Should -BeTrue
            ($script:RepairText9 -match "'info'")                               | Should -BeTrue
        }

        # ---- CD-20 + the backlog carry-over: fixes are verified, and gated on the port ----

        It 'a fix counts only when a re-measurement says the fault is gone' {
            ($script:RepairText9 -match 'FixVerify')                       | Should -BeTrue
            ($script:RepairText9 -match '\$f\.Fixed = \[bool\] \(& \$f\.FixVerify\)') | Should -BeTrue
            ($script:RepairText9 -match 'NOT FIXED')                       | Should -BeTrue
        }

        It 'the doctor never starts crabd with a bare Start-ScheduledTask' {
            # THE BACKLOG CARRY-OVER: with a foreign process on 9999 that reproduces the
            # 2026-08-27 incident - the new instance loses the bind, exits 1, task back to Ready.
            $fixActions = @([regex]::Matches($script:RepairText9, '-FixAction[^\r\n]*'))
            @($fixActions | Where-Object { $_.Value -match 'Start-ScheduledTask' }).Count | Should -Be 0
            # every start goes through the port-waiting shared restart instead
            ($script:RepairText9 -match 'Restart-SideCrabTask') | Should -BeTrue
        }

        It 'the start verification is a named function, not a built string or a closure' {
            # A here-string through [scriptblock]::Create only fails to parse once -Fix runs -
            # i.e. in the incident, never in a test. .GetNewClosure() is the other trap: it
            # rebinds the block to a module scope where this script's own Get-HealthDocument
            # does not resolve, so the verify would throw and every fix would read as failed.
            ($script:RepairText9 -match 'function Test-CrabdIsServing') | Should -BeTrue
            ($script:RepairText9 -match 'Test-CrabdIsServing -Port \$crabdPort') | Should -BeTrue
            # the CALL, not the word - the comment above it names both traps on purpose
            ($script:RepairText9 -match '\}\s*\.GetNewClosure\(\)')     | Should -BeFalse
            # it re-measures BOTH halves from scratch - a health answer alone can be a foreign
            # process, and a Running task may never have bound the port
            $fn = ([regex]::Match($script:RepairText9, '(?s)function Test-CrabdIsServing \{.*?\n\}')).Value
            ($fn -match 'Get-SideCrabHealthProbe')    | Should -BeTrue
            ($fn -match 'Get-SideCrabTaskState')      | Should -BeTrue
            ($fn -match 'Get-SideCrabServiceVerdict') | Should -BeTrue
        }

        It 'the start fix is not offered at all while something holds the port' {
            ($script:RepairText9 -match '\$portHeld')                                  | Should -BeTrue
            ($script:RepairText9 -match '\$portHeld.*\{ \$null \}')                    | Should -BeTrue
            # and the holder is read BEFORE the health row that gates on it
            $read = $script:RepairText9.IndexOf('$crabdHolder = @(Get-SideCrabPortHolder')
            $row  = $script:RepairText9.IndexOf("-- 1. is crabd answering")
            ($read -lt $row) | Should -BeTrue
        }

        # ---- CD-21: glow is not installed green when its SDK will not import --------------

        It 'an auto-detected glow whose cuesdk will not import is SKIPPED, with the pip line' {
            $p = Get-SideCrabGlowPreflight -Selected $true -Requested $false -Importable $false `
                                           -RequirementsPath 'C:\r\lighting\requirements.txt'
            $p.Install | Should -BeFalse
            $p.Status  | Should -Be 'skipped'
            $p.Command | Should -Match 'pip install -r'
            $p.Reason  | Should -Match 'never light'
        }

        It 'an explicit -WithGlow installs anyway, loudly - a switch is an instruction' {
            # Failing the whole install (crabd included) over a lighting dependency would be the
            # worse outcome.
            $p = Get-SideCrabGlowPreflight -Selected $true -Requested $true -Importable $false
            $p.Install | Should -BeTrue
            $p.Status  | Should -Be 'requested-broken'
            $p.Reason  | Should -Match 'does NOT import'
        }

        It 'an importable glow installs silently - the gate cannot fire on a healthy box' {
            # Measured 2026-08-27 on this host: `import cuesdk` succeeds, so a healthy install
            # sees no new output at all. A gate that fires on a healthy night is worse than none.
            $p = Get-SideCrabGlowPreflight -Selected $true -Requested $false -Importable $true
            $p.Install | Should -BeTrue
            $p.Status  | Should -Be 'ok'
            $p.Command | Should -Be ''
        }

        It 'only glow declares an import to check, and it names the pinned requirements file' {
            (script:One $script:Spec 'glow').PyImport  | Should -Be 'cuesdk'
            (script:One $script:Spec 'crabd').PyImport | Should -BeNullOrEmpty
            (script:One $script:Spec 'toast').PyImport | Should -BeNullOrEmpty
            (script:One $script:Spec 'glow').PyRequires | Should -Match 'lighting.requirements\.txt$'
        }

        It 'the installer runs the import under the SAME interpreter the task will use' {
            # A different python on PATH is a different set of site-packages, so checking with
            # anything but $python would answer a question nobody asked.
            ($script:InstallText9 -match 'Get-SideCrabGlowPreflight')            | Should -BeTrue
            ($script:InstallText9 -match '& \$python -c "import \$\(\$c\.PyImport\)"') | Should -BeTrue
        }

        # ---- CD-24: freshness watches the code, not just the entry point ------------------

        It 'glow watches the modules that actually change, not only its launcher' {
            $glow = @((script:One $script:Spec 'glow').WatchFiles)
            $glow.Count | Should -Be 4
            foreach ($n in 'glow_launcher.pyw', 'sidecrab_glow.py', 'icue.py', 'decision.py') {
                @($glow | Where-Object { $_ -like "*$n" }).Count | Should -Be 1
            }
        }

        It 'the toast handlers are deliberately NOT watched' {
            # The shell launches them as their own processes on a button press; their mtime says
            # nothing about the age of the running toast daemon.
            $toast = @((script:One $script:Spec 'toast').WatchFiles)
            @($toast | Where-Object { $_ -like '*_handler.pyw' }).Count | Should -Be 0
        }

        It 'a plan row carries WatchFiles through, so the doctor cannot silently re-narrow' {
            @((script:One (script:Plan -Present @{ glow = $true }) 'glow').WatchFiles).Count | Should -Be 4
        }

        It 'the newest watched file wins, and names itself' {
            $old = Join-Path $script:TempDir9 'launcher.pyw'
            $new = Join-Path $script:TempDir9 'glow.py'
            Set-Content -LiteralPath $old -Value 'x' -Encoding utf8NoBOM
            Set-Content -LiteralPath $new -Value 'y' -Encoding utf8NoBOM
            (Get-Item -LiteralPath $old).LastWriteTime = [datetime]'2020-01-01T00:00:00'
            (Get-Item -LiteralPath $new).LastWriteTime = [datetime]'2026-08-27T00:00:00'
            $w = Get-SideCrabWatchedWriteTime -Path @($old, $new)
            $w.WriteTime | Should -Be ([datetime]'2026-08-27T00:00:00')
            $w.Path      | Should -Be $new
            $w.Checked   | Should -Be 2
        }

        It 'a missing watched file is skipped, and all-missing reports no time rather than throwing' {
            $only = Join-Path $script:TempDir9 'only.py'
            Set-Content -LiteralPath $only -Value 'z' -Encoding utf8NoBOM
            (Get-SideCrabWatchedWriteTime -Path @((Join-Path $script:TempDir9 'gone.py'), $only)).Path | Should -Be $only
            # $null WriteTime is what Get-SideCrabStaleCodeDecision reads as 'unknown' - not stale
            (Get-SideCrabWatchedWriteTime -Path @((Join-Path $script:TempDir9 'gone.py'))).WriteTime | Should -BeNullOrEmpty
        }

        It 'the doctor reads the watched set, not the entry point' {
            ($script:RepairText9 -match 'Get-SideCrabWatchedWriteTime')                  | Should -BeTrue
            ($script:RepairText9 -match 'Get-Item -LiteralPath \$c\.Script')             | Should -BeFalse
        }

        # ---- CD-25: a dead IconUri is detected AND removed --------------------------------

        It 'an IconUri pointing at an icon that is not there is NOT current' {
            # The old test compared IconUri against the spec path and never asked whether the
            # file existed - so a repo move left a key that read healthy and rendered no icon.
            $d = Get-SideCrabAumidIconDecision -IconPresent $false -SpecIconUri 'C:\repo\notifier\sidecrab.ico' `
                                               -RegisteredIconUri 'C:\repo\notifier\sidecrab.ico'
            $d.Expected | Should -BeNullOrEmpty
            $d.Matches  | Should -BeFalse
            $d.Remove   | Should -BeTrue
            $d.Reason   | Should -Match 'dead pointer'
        }

        It 'a missing icon with no IconUri registered is the honest state, and is left alone' {
            $d = Get-SideCrabAumidIconDecision -IconPresent $false -SpecIconUri 'C:\repo\notifier\sidecrab.ico' `
                                               -RegisteredIconUri $null
            $d.Matches | Should -BeTrue
            $d.Remove  | Should -BeFalse
        }

        It 'a present icon expects its path, and a stale path is an update rather than a delete' {
            $ok = Get-SideCrabAumidIconDecision -IconPresent $true -SpecIconUri 'C:\new\sidecrab.ico' `
                                                -RegisteredIconUri 'C:\new\sidecrab.ico'
            $ok.Matches | Should -BeTrue
            $ok.Remove  | Should -BeFalse
            $moved = Get-SideCrabAumidIconDecision -IconPresent $true -SpecIconUri 'C:\new\sidecrab.ico' `
                                                   -RegisteredIconUri 'C:\old\sidecrab.ico'
            $moved.Matches  | Should -BeFalse
            $moved.Remove   | Should -BeFalse      # written over, not deleted
            $moved.Expected | Should -Be 'C:\new\sidecrab.ico'
        }

        It 'the re-registration DELETES a dead IconUri instead of writing around it' {
            # -Force overwrites the values it is given and touches no others, so writing
            # DisplayName only left the dead pointer exactly where it was - and re-running the
            # installer could never repair it.
            $fn = ([regex]::Match($script:CommonText9, '(?s)function Set-SideCrabAumid \{.*?\n\}')).Value
            ($fn -match 'Remove-ItemProperty')       | Should -BeTrue
            ($fn -match "-Name 'IconUri'")           | Should -BeTrue
            ($fn -match 'Get-SideCrabAumidIconDecision') | Should -BeTrue
        }

        It 'the state read routes its Current through the same icon decision' {
            $fn = ([regex]::Match($script:CommonText9, '(?s)function Get-SideCrabAumidState \{.*?\n\}')).Value
            ($fn -match 'Get-SideCrabAumidIconDecision') | Should -BeTrue
            # the naive comparison is gone
            ($fn -match '\$iconUri -eq \$spec\.IconUri')  | Should -BeFalse
        }
    }
}

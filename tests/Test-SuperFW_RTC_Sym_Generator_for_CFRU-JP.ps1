$ErrorActionPreference = 'Stop'

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function Get-BytesSha256 {
    param([byte[]]$Bytes)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($sha256.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Invoke-GeneratorProcess {
    param(
        [string]$EnginePath,
        [string]$GeneratorPath,
        [string]$InputPath,
        [string]$ManifestPath,
        [string]$OutputPath
    )

    $quoteArgument = {
        param([string]$Value)
        '"' + $Value.Replace('"', '\"') + '"'
    }
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File {0} -RomPath {1} -SignatureManifestPath {2}' -f `
        (& $quoteArgument $GeneratorPath), (& $quoteArgument $InputPath), (& $quoteArgument $ManifestPath)
    if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
        $arguments += ' -OutputPath {0}' -f (& $quoteArgument $OutputPath)
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $EnginePath
    $startInfo.Arguments = $arguments
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $null = $process.Start()
    $standardOutput = $process.StandardOutput.ReadToEnd()
    $standardError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $result = [pscustomobject]@{
        ExitCode = $process.ExitCode
        StandardOutput = $standardOutput
        StandardError = $standardError
    }
    $process.Dispose()
    return $result
}

function New-SyntheticRom {
    param(
        [string]$Path,
        [bool]$OmitLastSignature = $false,
        [bool]$DuplicateFirstSignature = $false
    )

    $bytes = [byte[]]::new(0x140)
    $titleBytes = [System.Text.Encoding]::ASCII.GetBytes('POKEMON FIRE')
    $gameCodeBytes = [System.Text.Encoding]::ASCII.GetBytes('BPRJ')
    [System.Array]::Copy($titleBytes, 0, $bytes, 0xA0, $titleBytes.Length)
    [System.Array]::Copy($gameCodeBytes, 0, $bytes, 0xAC, $gameCodeBytes.Length)

    $patterns = @(
        [byte[]]@(0xDE, 0xAD, 0xC0, 0x01),
        [byte[]]@(0xDE, 0xAD, 0xC0, 0x02),
        [byte[]]@(0xDE, 0xAD, 0xC0, 0x03),
        [byte[]]@(0xDE, 0xAD, 0xC0, 0x04)
    )
    $offsets = @(0xD0, 0xE0, 0xF0, 0x100)
    for ($i = 0; $i -lt $patterns.Count; $i++) {
        if (-not ($OmitLastSignature -and $i -eq 3)) {
            [System.Array]::Copy($patterns[$i], 0, $bytes, $offsets[$i], $patterns[$i].Length)
        }
    }
    if ($DuplicateFirstSignature) {
        [System.Array]::Copy($patterns[0], 0, $bytes, 0x110, $patterns[0].Length)
    }

    [System.IO.File]::WriteAllBytes($Path, $bytes)
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('superfw-sym-test-' + [guid]::NewGuid().ToString('N'))
$generatorPath = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $PSScriptRoot '..') 'SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1'))
$engineName = if ($env:OS -eq 'Windows_NT') { 'powershell.exe' } else { 'pwsh' }
$enginePath = (Get-Command $engineName -ErrorAction Stop).Source

try {
    [System.IO.Directory]::CreateDirectory($tempRoot) | Out-Null
    $manifestPath = Join-Path $tempRoot 'synthetic-signatures.json'
    $manifest = [ordered]@{
        schemaVersion = 1
        signatures = @(
            [ordered]@{ name = 'SiiRtcProbe'; size = 4; bytes = 'DEADC001' },
            [ordered]@{ name = 'SiiRtcReset'; size = 4; bytes = 'DEADC002' },
            [ordered]@{ name = 'SiiRtcGetStatus'; size = 4; bytes = 'DEADC003' },
            [ordered]@{ name = 'SiiRtcGetDateTime'; size = 4; bytes = 'DEADC004' }
        )
    }
    [System.IO.File]::WriteAllText($manifestPath, (ConvertTo-Json -InputObject $manifest -Depth 4), [System.Text.Encoding]::UTF8)

    $romPath = Join-Path $tempRoot 'synthetic-success.gba'
    New-SyntheticRom -Path $romPath
    $romHashBefore = Get-BytesSha256 ([System.IO.File]::ReadAllBytes($romPath))
    $success = Invoke-GeneratorProcess -EnginePath $enginePath -GeneratorPath $generatorPath -InputPath $romPath -ManifestPath $manifestPath
    Assert-True ($success.ExitCode -eq 0) "successful generation returned $($success.ExitCode): $($success.StandardError)"

    $symPath = [System.IO.Path]::ChangeExtension($romPath, '.sym')
    Assert-True ([System.IO.File]::Exists($symPath)) 'success output exists'
    $expectedLines = @(
        '080000d0 g 00000004 SiiRtcProbe',
        '080000e0 g 00000004 SiiRtcReset',
        '080000f0 g 00000004 SiiRtcGetStatus',
        '08000100 g 00000004 SiiRtcGetDateTime'
    )
    $expectedSym = ($expectedLines -join "`r`n") + "`r`n"
    $actualSymBytes = [System.IO.File]::ReadAllBytes($symPath)
    $actualSym = [System.Text.Encoding]::ASCII.GetString($actualSymBytes)
    Assert-True ($actualSym -ceq $expectedSym) 'symbol output matches the exact expected bytes and order'
    Assert-True ($actualSymBytes.Length -eq $expectedSym.Length) 'symbol output has no encoding preamble'
    Assert-True ((Get-BytesSha256 ([System.IO.File]::ReadAllBytes($romPath))) -ceq $romHashBefore) 'input ROM hash is unchanged'
    Assert-True ($success.StandardOutput.Contains("Output: $symPath")) 'success prints the output path'
    Assert-True ($success.StandardOutput.Contains("ROM SHA-256: $romHashBefore")) 'success prints the ROM hash'
    Assert-True ($success.StandardOutput.Contains("SYM SHA-256: $(Get-BytesSha256 $actualSymBytes)")) 'success prints the symbol hash'

    $refusal = Invoke-GeneratorProcess -EnginePath $enginePath -GeneratorPath $generatorPath -InputPath $romPath -ManifestPath $manifestPath
    Assert-True ($refusal.ExitCode -ne 0) 'existing output is refused without -Force'
    Assert-True (([System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($symPath))) -ceq $expectedSym) 'refusal leaves existing output unchanged'

    $customSymPath = Join-Path $tempRoot 'custom-output.sym'
    $customOutput = Invoke-GeneratorProcess -EnginePath $enginePath -GeneratorPath $generatorPath -InputPath $romPath -ManifestPath $manifestPath -OutputPath $customSymPath
    Assert-True ($customOutput.ExitCode -eq 0) "custom output generation returned $($customOutput.ExitCode): $($customOutput.StandardError)"
    Assert-True ([System.IO.File]::Exists($customSymPath)) 'custom output path is honored'
    Assert-True ([System.IO.File]::Exists($symPath)) 'custom output does not replace the default output'

    [System.IO.File]::Delete($symPath)
    New-SyntheticRom -Path $romPath -OmitLastSignature $true
    $missing = Invoke-GeneratorProcess -EnginePath $enginePath -GeneratorPath $generatorPath -InputPath $romPath -ManifestPath $manifestPath
    Assert-True ($missing.ExitCode -ne 0) 'missing signature returns nonzero'
    Assert-True (-not [System.IO.File]::Exists($symPath)) 'missing signature creates no output'

    New-SyntheticRom -Path $romPath -DuplicateFirstSignature $true
    $duplicate = Invoke-GeneratorProcess -EnginePath $enginePath -GeneratorPath $generatorPath -InputPath $romPath -ManifestPath $manifestPath
    Assert-True ($duplicate.ExitCode -ne 0) 'duplicate signature returns nonzero'
    Assert-True (-not [System.IO.File]::Exists($symPath)) 'duplicate signature creates no output'

    $fullFlowPath = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $PSScriptRoot '..') 'SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.ps1'))
    foreach ($sourcePath in @($generatorPath, $fullFlowPath, $PSCommandPath)) {
        $tokens = $null
        $parseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile($sourcePath, [ref]$tokens, [ref]$parseErrors) | Out-Null
        Assert-True ($parseErrors.Count -eq 0) "PowerShell AST parses $sourcePath"
    }

    $fullFlowAst = [System.Management.Automation.Language.Parser]::ParseFile($fullFlowPath, [ref]$tokens, [ref]$parseErrors)
    $pythonFinder = $fullFlowAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Find-PythonExecutable' }, $true)
    $powerShellResolver = $fullFlowAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-CurrentPowerShellExecutable' }, $true)
    $powerShellInvoker = $fullFlowAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-PowerShellCommand' }, $true)
    $outputPublisher = $fullFlowAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Publish-StagedOutputs' }, $true)
    Invoke-Expression $pythonFinder.Extent.Text
    Invoke-Expression $powerShellResolver.Extent.Text
    Invoke-Expression $powerShellInvoker.Extent.Text
    Invoke-Expression $outputPublisher.Extent.Text

    $publishRoot = Join-Path $tempRoot 'publish-tests'
    [System.IO.Directory]::CreateDirectory($publishRoot) | Out-Null
    $publishSymPath = Join-Path $publishRoot 'overwrite.sym'
    $publishPatchPath = Join-Path $publishRoot 'overwrite.patch'
    $publishSymStage = Join-Path $publishRoot 'staged.sym'
    $publishPatchStage = Join-Path $publishRoot 'staged.patch'
    [System.IO.File]::WriteAllText($publishSymPath, 'old sym')
    [System.IO.File]::WriteAllText($publishPatchPath, 'old patch')
    [System.IO.File]::WriteAllText($publishSymStage, 'new sym')
    [System.IO.File]::WriteAllText($publishPatchStage, 'new patch')
    Publish-StagedOutputs -SymStagedPath $publishSymStage -SymOutputPath $publishSymPath -PatchStagedPath $publishPatchStage -PatchOutputPath $publishPatchPath -Guid ([guid]::NewGuid().ToString('N'))
    Assert-True ([System.IO.File]::ReadAllText($publishSymPath) -ceq 'new sym') 'successful publication overwrites the existing symbol output'
    Assert-True ([System.IO.File]::ReadAllText($publishPatchPath) -ceq 'new patch') 'successful publication overwrites the existing patch output'
    Assert-True (@([System.IO.Directory]::GetFiles($publishRoot, '*.bak')).Count -eq 0) 'successful publication removes backups'
    Assert-True (-not [System.IO.File]::Exists($publishSymStage) -and -not [System.IO.File]::Exists($publishPatchStage)) 'successful publication consumes staged outputs'

    [System.IO.File]::WriteAllText($publishSymPath, 'preserved sym')
    [System.IO.File]::WriteAllText($publishPatchPath, 'preserved patch')
    [System.IO.File]::WriteAllText($publishSymStage, 'replacement sym')
    $publishFailure = $null
    try {
        Publish-StagedOutputs -SymStagedPath $publishSymStage -SymOutputPath $publishSymPath -PatchStagedPath $publishPatchStage -PatchOutputPath $publishPatchPath -Guid ([guid]::NewGuid().ToString('N'))
    }
    catch {
        $publishFailure = $_.Exception.Message
    }
    Assert-True (-not [string]::IsNullOrWhiteSpace($publishFailure)) 'failed pair publication is reported'
    Assert-True ([System.IO.File]::ReadAllText($publishSymPath) -ceq 'preserved sym') 'failed publication restores the previous symbol output'
    Assert-True ([System.IO.File]::ReadAllText($publishPatchPath) -ceq 'preserved patch') 'failed publication preserves the previous patch output'
    Assert-True (@([System.IO.Directory]::GetFiles($publishRoot, '*.bak')).Count -eq 0) 'rollback restores outputs without backup leftovers'

    $script:pythonProbeAttempts = @()
    $mockPowerShellInvoker = {
        param($CommandToken, $Arguments)
        $script:pythonProbeAttempts += [pscustomobject]@{ CommandToken = $CommandToken; Arguments = @($Arguments) }
        if ($CommandToken -eq 'py') { return [pscustomobject]@{ ExitCode = 1; Output = 'Synthetic py failure' } }
        [pscustomobject]@{ ExitCode = 0; Output = 'Python 3.14.8' }
    }
    $pythonSelection = Find-PythonExecutable -CommandInvoker $mockPowerShellInvoker
    Assert-True (($script:pythonProbeAttempts.CommandToken -join ',') -ceq 'py,python') 'failed py probe falls back to python'
    Assert-True ($pythonSelection.CommandToken -ceq 'python') 'PowerShell-invokable python is selected by command token, without resolving a Source path'
    Assert-True ($pythonSelection.Arguments.Count -eq 0) 'python is invoked without py launcher arguments'
    Assert-True (($script:pythonProbeAttempts[0].Arguments -join ',') -ceq '-3,-c,import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)') 'py probe includes -3'

    $pythonOnlyInvoker = {
        param($CommandToken, $Arguments)
        if ($CommandToken -ne 'python') { throw 'Synthetic py command is unavailable' }
        [pscustomobject]@{ ExitCode = 0; Output = 'Python 3.14.8' }
    }
    $pythonOnlySelection = Find-PythonExecutable -CommandInvoker $pythonOnlyInvoker
    Assert-True ($pythonOnlySelection.CommandToken -ceq 'python') 'mock PowerShell-native invoker selects python even when direct executable launch is unavailable'

    $pythonFailure = $null
    try {
        Find-PythonExecutable -CommandInvoker { param($CommandToken, $Arguments) [pscustomobject]@{ ExitCode = 1; Output = 'Synthetic command failure' } } | Out-Null
    }
    catch {
        $pythonFailure = $_.Exception.Message
    }
    Assert-True ($pythonFailure.Contains("Neither the 'py' launcher nor 'python' command")) 'no compatible Python candidate reports a clear error'
    Assert-True ($pythonFailure.Contains('Synthetic command failure')) 'Python probe failure output is retained in the diagnostic'

    if ($env:OS -eq 'Windows_NT') {
        $nativeStderr = Invoke-PowerShellCommand -CommandToken 'cmd.exe' -Arguments @('/c', 'echo native-stderr 1>&2')
        Assert-True ($nativeStderr.ExitCode -eq 0) 'captured native stderr does not terminate invocation under ErrorActionPreference Stop'
        Assert-True ($nativeStderr.Output.Contains('native-stderr')) 'native stderr is captured for reporting'
        Assert-True ($ErrorActionPreference -eq 'Stop') 'native invocation restores ErrorActionPreference'
    }

    $currentPowerShellPath = Get-CurrentPowerShellExecutable
    Assert-True ([System.IO.File]::Exists($currentPowerShellPath)) 'current PowerShell executable resolves to an existing file'

    [Console]::WriteLine('All synthetic SuperFW symbol tests passed.')
}
finally {
    if ([System.IO.Directory]::Exists($tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

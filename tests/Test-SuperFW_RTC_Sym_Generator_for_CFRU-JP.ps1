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
    Invoke-Expression $pythonFinder.Extent.Text
    Invoke-Expression $powerShellResolver.Extent.Text

    $script:pythonProbeAttempts = @()
    $mockLookup = { param($Name) [pscustomobject]@{ Source = $Name + '.exe' } }
    $mockInvoker = {
        param($FileName, $Arguments)
        $script:pythonProbeAttempts += $FileName
        if ($FileName -eq 'py.exe') { throw 'Synthetic Process.Start failure' }
        [pscustomobject]@{ ExitCode = 0 }
    }
    $pythonSelection = Find-PythonExecutable -CommandLookup $mockLookup -ProcessInvoker $mockInvoker
    Assert-True (($script:pythonProbeAttempts -join ',') -ceq 'py.exe,python.exe') 'failed py launch falls back to python'
    Assert-True ($pythonSelection.Path -ceq 'python.exe') 'python is selected after py launch failure'
    Assert-True ($pythonSelection.Arguments.Count -eq 0) 'python is invoked without py launcher arguments'

    $pythonFailure = $null
    try {
        Find-PythonExecutable -CommandLookup $mockLookup -ProcessInvoker { throw 'Synthetic Process.Start failure' } | Out-Null
    }
    catch {
        $pythonFailure = $_.Exception.Message
    }
    Assert-True ($pythonFailure.Contains("Neither the 'py' launcher nor 'python' command")) 'no launchable Python candidate reports a clear error'

    $currentPowerShellPath = Get-CurrentPowerShellExecutable
    Assert-True ([System.IO.File]::Exists($currentPowerShellPath)) 'current PowerShell executable resolves to an existing file'

    [Console]::WriteLine('All synthetic SuperFW symbol tests passed.')
}
finally {
    if ([System.IO.Directory]::Exists($tempRoot)) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

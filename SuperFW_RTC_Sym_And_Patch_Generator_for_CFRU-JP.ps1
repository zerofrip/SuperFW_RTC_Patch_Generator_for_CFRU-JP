[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$RomPath,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Quote-ProcessArgument {
    param([string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Invoke-CapturedProcess {
    param(
        [string]$FileName,
        [string[]]$Arguments
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FileName
    $startInfo.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $null = $process.Start()
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $result = [pscustomobject]@{
        ExitCode = $process.ExitCode
        StandardOutput = $stdout
        StandardError = $stderr
    }
    $process.Dispose()
    return $result
}

function Invoke-PowerShellCommand {
    param(
        [string]$CommandToken,
        [string[]]$Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $outputItems = @()
    $exitCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = $null
        $outputItems = @(& $CommandToken @Arguments 2>&1)
        if ($null -eq $global:LASTEXITCODE) {
            $exitCode = 0
        }
        else {
            $exitCode = [int]$global:LASTEXITCODE
        }
    }
    catch {
        $outputItems += $_.Exception.Message
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $capturedOutput = @($outputItems | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $capturedOutput
    }
}

function Find-PythonExecutable {
    param(
        [scriptblock]$CommandInvoker
    )

    if (-not $CommandInvoker) {
        $CommandInvoker = { param($CommandToken, $Arguments) Invoke-PowerShellCommand -CommandToken $CommandToken -Arguments $Arguments }
    }

    $failures = @()
    foreach ($candidateName in @('py', 'python')) {
        $arguments = @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)')
        if ($candidateName -eq 'py') { $arguments = @('-3') + $arguments }
        try {
            $pythonCheck = & $CommandInvoker $candidateName $arguments
        }
        catch {
            $failures += "$candidateName`: $($_.Exception.Message)"
            continue
        }

        if ($pythonCheck.ExitCode -eq 0) {
            $launcherArguments = @()
            if ($candidateName -eq 'py') { $launcherArguments = @('-3') }
            return [pscustomobject]@{
                CommandToken = $candidateName
                Arguments = $launcherArguments
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($pythonCheck.Output)) {
            $failures += "$candidateName`: $($pythonCheck.Output.Trim())"
        }
    }

    $diagnostic = "Python 3.8 or newer is required. Neither the 'py' launcher nor 'python' command could start a compatible interpreter; install Python 3 and enable one of these commands."
    if ($failures.Count -gt 0) { $diagnostic += ' ' + ($failures -join '; ') }
    throw $diagnostic
}

function Get-CurrentPowerShellExecutable {
    $process = [System.Diagnostics.Process]::GetCurrentProcess()
    try {
        $path = $process.MainModule.FileName
    }
    finally {
        $process.Dispose()
    }

    if ([string]::IsNullOrWhiteSpace($path) -or -not [System.IO.File]::Exists($path)) {
        throw 'Could not resolve the current PowerShell executable.'
    }
    return $path
}

function Publish-StagedOutputs {
    param(
        [string]$SymStagedPath,
        [string]$SymOutputPath,
        [string]$PatchStagedPath,
        [string]$PatchOutputPath,
        [string]$Guid
    )

    $backupPaths = @()
    $publishedPaths = @()
    try {
        foreach ($outputPath in @($SymOutputPath, $PatchOutputPath)) {
            if ([System.IO.File]::Exists($outputPath)) {
                $backupPath = $outputPath + '.' + $Guid + '.bak'
                [System.IO.File]::Move($outputPath, $backupPath)
                $backupPaths += [pscustomobject]@{ Original = $outputPath; Backup = $backupPath }
            }
        }

        [System.IO.File]::Move($SymStagedPath, $SymOutputPath)
        $publishedPaths += $SymOutputPath
        [System.IO.File]::Move($PatchStagedPath, $PatchOutputPath)
        $publishedPaths += $PatchOutputPath
    }
    catch {
        $publicationError = $_.Exception.Message
        $rollbackErrors = @()
        foreach ($outputPath in $publishedPaths) {
            try {
                [System.IO.File]::Delete($outputPath)
            }
            catch {
                $rollbackErrors += "Could not remove new output '$outputPath': $($_.Exception.Message)"
            }
        }
        foreach ($backup in $backupPaths) {
            try {
                [System.IO.File]::Move($backup.Backup, $backup.Original)
            }
            catch {
                $rollbackErrors += "Could not restore '$($backup.Original)' from '$($backup.Backup)': $($_.Exception.Message)"
            }
        }

        if ($rollbackErrors.Count -gt 0) {
            throw "Output replacement failed: $publicationError. Rollback was incomplete: $($rollbackErrors -join '; ')"
        }
        throw "Output replacement failed; existing outputs were restored: $publicationError"
    }

    foreach ($backup in $backupPaths) {
        try {
            [System.IO.File]::Delete($backup.Backup)
        }
        catch {
            [Console]::Error.WriteLine("WARNING: Could not remove backup file: $($backup.Backup)")
        }
    }
}

$tempPaths = @()
try {
    $resolvedRomPath = [System.IO.Path]::GetFullPath($RomPath)
    if ([System.IO.Path]::GetExtension($resolvedRomPath) -ine '.gba') {
        throw 'Input must have a .gba extension.'
    }
    if (-not [System.IO.File]::Exists($resolvedRomPath)) {
        throw 'Input ROM does not exist.'
    }

    $symPath = [System.IO.Path]::ChangeExtension($resolvedRomPath, '.sym')
    $patchPath = [System.IO.Path]::ChangeExtension($resolvedRomPath, '.patch')
    foreach ($outputPath in @($symPath, $patchPath)) {
        if ([System.IO.Directory]::Exists($outputPath)) {
            throw "Output path is a directory: $outputPath"
        }
    }

    $pythonCommand = $null
    $pythonArgs = @()
    $pythonSelection = Find-PythonExecutable
    $pythonCommand = $pythonSelection.CommandToken
    $pythonArgs = @($pythonSelection.Arguments)

    $guid = [guid]::NewGuid().ToString('N')
    $symStage = Join-Path ([System.IO.Path]::GetDirectoryName($symPath)) ('.' + [System.IO.Path]::GetFileNameWithoutExtension($symPath) + '.' + $guid + '.sym')
    $patchStage = Join-Path ([System.IO.Path]::GetDirectoryName($patchPath)) ('.' + [System.IO.Path]::GetFileNameWithoutExtension($patchPath) + '.' + $guid + '.patch')
    $tempPaths += $symStage
    $tempPaths += $patchStage

    $powerShellPath = Get-CurrentPowerShellExecutable
    $symArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $PSScriptRoot 'SuperFW_RTC_Sym_Generator_for_CFRU-JP.ps1'),
        '-RomPath', $resolvedRomPath, '-OutputPath', $symStage
    )
    $symResult = Invoke-CapturedProcess -FileName $powerShellPath -Arguments $symArgs
    if ($symResult.ExitCode -ne 0) {
        if ($symResult.StandardError) { [Console]::Error.WriteLine($symResult.StandardError.Trim()) }
        throw 'Symbol generation failed; no patch was created.'
    }

    $runnerArgs = @($pythonArgs) + @(
        (Join-Path $PSScriptRoot 'SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.py'),
        '--rom', $resolvedRomPath, '--sym', $symStage, '--output', $patchStage
    )
    $runnerResult = Invoke-PowerShellCommand -CommandToken $pythonCommand -Arguments $runnerArgs
    if ($runnerResult.ExitCode -ne 0) {
        if ($runnerResult.Output) { [Console]::Error.WriteLine($runnerResult.Output.Trim()) }
        throw 'Patch generation failed; existing outputs were not replaced.'
    }

    Publish-StagedOutputs -SymStagedPath $symStage -SymOutputPath $symPath -PatchStagedPath $patchStage -PatchOutputPath $patchPath -Guid $guid

    [Console]::WriteLine($runnerResult.Output.TrimEnd())
    [Console]::WriteLine("SYM: $symPath")
    [Console]::WriteLine("PATCH: $patchPath")
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}
finally {
    foreach ($tempPath in $tempPaths) {
        if ([System.IO.File]::Exists($tempPath)) {
            [System.IO.File]::Delete($tempPath)
        }
    }
}

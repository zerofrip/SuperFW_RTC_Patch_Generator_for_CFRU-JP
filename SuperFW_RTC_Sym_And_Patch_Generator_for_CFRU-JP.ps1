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

function Find-PythonExecutable {
    param(
        [scriptblock]$CommandLookup,
        [scriptblock]$ProcessInvoker
    )

    if (-not $CommandLookup) {
        $CommandLookup = { param($Name) Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue }
    }
    if (-not $ProcessInvoker) {
        $ProcessInvoker = { param($FileName, $Arguments) Invoke-CapturedProcess -FileName $FileName -Arguments $Arguments }
    }

    foreach ($candidateName in @('py', 'python')) {
        $candidate = & $CommandLookup $candidateName
        if (-not $candidate -or -not $candidate.Source) { continue }

        $arguments = @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)')
        if ($candidateName -eq 'py') { $arguments = @('-3') + $arguments }
        try {
            $pythonCheck = & $ProcessInvoker $candidate.Source $arguments
        }
        catch {
            continue
        }

        if ($pythonCheck.ExitCode -eq 0) {
            $launcherArguments = @()
            if ($candidateName -eq 'py') { $launcherArguments = @('-3') }
            return [pscustomobject]@{
                Path = $candidate.Source
                Arguments = $launcherArguments
            }
        }
    }

    throw "Python 3.8 or newer is required. Neither the 'py' launcher nor 'python' command could start a compatible interpreter; install Python 3 and enable one of these commands."
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

function Publish-StagedFile {
    param(
        [string]$StagedPath,
        [string]$OutputPath,
        [switch]$Force
    )

    if ([System.IO.File]::Exists($OutputPath)) {
        if (-not $Force) {
            throw "Output appeared during generation; refusing to overwrite: $OutputPath"
        }
        [System.IO.File]::Replace($StagedPath, $OutputPath, $null)
    }
    else {
        [System.IO.File]::Move($StagedPath, $OutputPath)
    }
}

$tempPaths = @()
$backupPaths = @()
$publishedPaths = @()
$publicationComplete = $false
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
        if ([System.IO.File]::Exists($outputPath) -and -not $Force) {
            throw "Output already exists; use -Force to replace .sym and .patch: $outputPath"
        }
    }

    $pythonPath = $null
    $pythonArgs = @()
    $pythonSelection = Find-PythonExecutable
    $pythonPath = $pythonSelection.Path
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
    $runnerResult = Invoke-CapturedProcess -FileName $pythonPath -Arguments $runnerArgs
    if ($runnerResult.ExitCode -ne 0) {
        if ($runnerResult.StandardError) { [Console]::Error.WriteLine($runnerResult.StandardError.Trim()) }
        throw 'Patch generation failed; existing outputs were not replaced.'
    }

    if ($Force) {
        foreach ($outputPath in @($symPath, $patchPath)) {
            if ([System.IO.File]::Exists($outputPath)) {
                $backupPath = $outputPath + '.' + $guid + '.bak'
                [System.IO.File]::Move($outputPath, $backupPath)
                $backupPaths += [pscustomobject]@{ Original = $outputPath; Backup = $backupPath }
            }
        }
    }

    Publish-StagedFile -StagedPath $symStage -OutputPath $symPath -Force:$Force
    $publishedPaths += $symPath
    Publish-StagedFile -StagedPath $patchStage -OutputPath $patchPath -Force:$Force
    $publishedPaths += $patchPath
    $publicationComplete = $true

    foreach ($backup in $backupPaths) {
        try {
            [System.IO.File]::Delete($backup.Backup)
        }
        catch {
            [Console]::Error.WriteLine("WARNING: Could not remove backup file: $($backup.Backup)")
        }
    }
    $backupPaths = @()

    [Console]::WriteLine($runnerResult.StandardOutput.TrimEnd())
    [Console]::WriteLine("SYM: $symPath")
    [Console]::WriteLine("PATCH: $patchPath")
    exit 0
}
catch {
    if (-not $publicationComplete) {
        foreach ($outputPath in $publishedPaths) {
            if ([System.IO.File]::Exists($outputPath)) {
                [System.IO.File]::Delete($outputPath)
            }
        }
        foreach ($backup in $backupPaths) {
            if ([System.IO.File]::Exists($backup.Backup)) {
                [System.IO.File]::Move($backup.Backup, $backup.Original)
            }
        }
    }
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

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$RomPath,

    [switch]$Force,

    [string]$SignatureManifestPath
)

$ErrorActionPreference = 'Stop'

function Find-UniqueBytePattern {
    param(
        [byte[]]$RomBytes,
        [byte[]]$Pattern
    )

    $patternLength = $Pattern.Length
    if ($patternLength -eq 0 -or $patternLength -gt $RomBytes.Length) {
        return -1
    }

    $skip = [int[]]::new(256)
    for ($i = 0; $i -lt $skip.Length; $i++) {
        $skip[$i] = $patternLength
    }
    for ($i = 0; $i -lt ($patternLength - 1); $i++) {
        $skip[[int]$Pattern[$i]] = $patternLength - 1 - $i
    }

    $matchOffset = -1
    $lastIndex = $patternLength - 1
    $position = $lastIndex
    while ($position -lt $RomBytes.Length) {
        $candidate = $position - $lastIndex
        $patternIndex = $lastIndex
        while ($patternIndex -ge 0 -and $Pattern[$patternIndex] -eq $RomBytes[$candidate + $patternIndex]) {
            $patternIndex--
        }

        if ($patternIndex -lt 0) {
            if ($matchOffset -ge 0) {
                return -2
            }
            $matchOffset = $candidate
            $position++
        }
        else {
            $position += $skip[[int]$RomBytes[$position]]
        }
    }

    return $matchOffset
}

try {
    $resolvedRomPath = [System.IO.Path]::GetFullPath($RomPath)
    if ([System.IO.Path]::GetExtension($resolvedRomPath) -ine '.gba') {
        throw 'Input must have a .gba extension.'
    }
    if (-not [System.IO.File]::Exists($resolvedRomPath)) {
        throw 'Input ROM does not exist.'
    }

    $romStream = [System.IO.File]::Open(
        $resolvedRomPath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        if ($romStream.Length -lt 0xC0) {
            throw 'Input ROM is smaller than 0xC0 bytes.'
        }
        if ($romStream.Length -gt 32MB) {
            throw 'Input ROM exceeds 32 MiB.'
        }

        $romBytes = [byte[]]::new([int]$romStream.Length)
        $bytesRead = 0
        while ($bytesRead -lt $romBytes.Length) {
            $readCount = $romStream.Read($romBytes, $bytesRead, $romBytes.Length - $bytesRead)
            if ($readCount -eq 0) {
                throw 'Input ROM changed while it was being read.'
            }
            $bytesRead += $readCount
        }
    }
    finally {
        $romStream.Dispose()
    }

    if ([System.Text.Encoding]::ASCII.GetString($romBytes, 0xA0, 12) -cne 'POKEMON FIRE') {
        throw 'ROM header title is not POKEMON FIRE.'
    }
    if ([System.Text.Encoding]::ASCII.GetString($romBytes, 0xAC, 4) -cne 'BPRJ') {
        throw 'ROM game code is not BPRJ.'
    }

    if ([string]::IsNullOrWhiteSpace($SignatureManifestPath)) {
        $SignatureManifestPath = Join-Path $PSScriptRoot 'rtc-signatures.json'
    }
    $resolvedManifestPath = [System.IO.Path]::GetFullPath($SignatureManifestPath)
    if (-not [System.IO.File]::Exists($resolvedManifestPath)) {
        throw 'Signature manifest does not exist.'
    }

    $manifest = Get-Content -LiteralPath $resolvedManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $manifest.PSObject.Properties['schemaVersion'] -or $manifest.schemaVersion -ne 1) {
        throw 'Signature manifest schemaVersion must be 1.'
    }

    $expectedNames = @('SiiRtcProbe', 'SiiRtcReset', 'SiiRtcGetStatus', 'SiiRtcGetDateTime')
    $signatureRecords = @($manifest.signatures)
    if ($signatureRecords.Count -ne $expectedNames.Count) {
        throw 'Signature manifest must contain exactly four signatures.'
    }

    $signatures = @()
    for ($i = 0; $i -lt $expectedNames.Count; $i++) {
        $record = $signatureRecords[$i]
        if ($null -eq $record -or [string]$record.name -cne $expectedNames[$i]) {
            throw 'Signature manifest entries are missing or out of order.'
        }
        if ($null -eq $record.PSObject.Properties['size'] -or
            ($record.size -isnot [int] -and $record.size -isnot [long]) -or
            [long]$record.size -le 0) {
            throw "Signature size for $($expectedNames[$i]) must be a positive decimal integer."
        }
        if ($null -eq $record.PSObject.Properties['bytes'] -or [string]$record.bytes -cnotmatch '^(?:[0-9A-Fa-f]{2})+$') {
            throw "Signature bytes for $($expectedNames[$i]) must be non-empty even-length hexadecimal."
        }

        $hex = [string]$record.bytes
        $patternBytes = [byte[]]::new($hex.Length / 2)
        for ($byteIndex = 0; $byteIndex -lt $patternBytes.Length; $byteIndex++) {
            $patternBytes[$byteIndex] = [Convert]::ToByte($hex.Substring($byteIndex * 2, 2), 16)
        }
        if ($patternBytes.Length -ne [long]$record.size) {
            throw "Decoded signature length for $($expectedNames[$i]) does not equal its size."
        }

        $signatures += [pscustomobject]@{
            Name = $expectedNames[$i]
            Size = [long]$record.size
            Bytes = $patternBytes
        }
    }

    $foundOffsets = @()
    foreach ($signature in $signatures) {
        $offset = Find-UniqueBytePattern -RomBytes $romBytes -Pattern $signature.Bytes
        if ($offset -eq -1) {
            throw "Signature $($signature.Name) was not found."
        }
        if ($offset -eq -2) {
            throw "Signature $($signature.Name) matched more than once."
        }
        $foundOffsets += $offset
    }

    $outputPath = [System.IO.Path]::ChangeExtension($resolvedRomPath, '.sym')
    if (([System.IO.File]::Exists($outputPath) -or [System.IO.Directory]::Exists($outputPath)) -and -not $Force) {
        throw "Output already exists; use -Force to replace it: $outputPath"
    }

    $lines = @()
    for ($i = 0; $i -lt $signatures.Count; $i++) {
        $address = 0x08000000L + [long]$foundOffsets[$i]
        $lines += ('{0:x8} g {1:x8} {2}' -f $address, $signatures[$i].Size, $signatures[$i].Name)
    }
    $symBytes = [System.Text.Encoding]::ASCII.GetBytes(($lines -join "`r`n") + "`r`n")

    if ($Force) {
        [System.IO.File]::WriteAllBytes($outputPath, $symBytes)
    }
    else {
        $outputStream = [System.IO.File]::Open(
            $outputPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $outputStream.Write($symBytes, 0, $symBytes.Length)
        }
        finally {
            $outputStream.Dispose()
        }
    }

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $romHash = [System.BitConverter]::ToString($sha256.ComputeHash($romBytes)).Replace('-', '').ToLowerInvariant()
        $symHash = [System.BitConverter]::ToString($sha256.ComputeHash($symBytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }

    [Console]::WriteLine("Output: $outputPath")
    [Console]::WriteLine("ROM SHA-256: $romHash")
    [Console]::WriteLine("SYM SHA-256: $symHash")
    exit 0
}
catch {
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}

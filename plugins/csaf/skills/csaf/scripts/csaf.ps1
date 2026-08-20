#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CsafArgs
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Stop-BootstrapRequired {
    [Console]::Error.WriteLine('{"status":"bootstrap_required","reason":"runtime_missing_or_unhealthy","next_action":"run_platform_bootstrap_after_explicit_consent","requires_consent":true,"installs":["CSAF","OfficeCLI"],"network":"verified tagged stable release assets over HTTPS","api_key_required":false,"hosted_ai":false,"bootstrap":{"url":"https://github.com/karthiknambiar/csm-skills-framework/releases/latest/download/install.ps1","invocation":"powershell -NoProfile -ExecutionPolicy Bypass -File <downloaded-install.ps1>"}}')
    exit 3
}

function Skip-JsonWhitespace {
    while ($script:JsonPosition -lt $script:JsonText.Length -and
        [char]::IsWhiteSpace($script:JsonText[$script:JsonPosition])) {
        $script:JsonPosition += 1
    }
}

function Read-JsonString {
    if ($script:JsonPosition -ge $script:JsonText.Length -or
        $script:JsonText[$script:JsonPosition] -ne '"') {
        throw [FormatException]::new("invalid JSON string")
    }
    $script:JsonPosition += 1
    $builder = [Text.StringBuilder]::new()
    while ($script:JsonPosition -lt $script:JsonText.Length) {
        $character = $script:JsonText[$script:JsonPosition]
        $script:JsonPosition += 1
        if ($character -eq '"') { return $builder.ToString() }
        if ([int]$character -lt 32) { throw [FormatException]::new("invalid JSON control") }
        if ($character -ne '\') {
            [void]$builder.Append($character)
            continue
        }
        if ($script:JsonPosition -ge $script:JsonText.Length) {
            throw [FormatException]::new("invalid JSON escape")
        }
        $escape = $script:JsonText[$script:JsonPosition]
        $script:JsonPosition += 1
        switch ($escape) {
            '"' { [void]$builder.Append('"') }
            '\' { [void]$builder.Append('\') }
            '/' { [void]$builder.Append('/') }
            'b' { [void]$builder.Append([char]8) }
            'f' { [void]$builder.Append([char]12) }
            'n' { [void]$builder.Append([char]10) }
            'r' { [void]$builder.Append([char]13) }
            't' { [void]$builder.Append([char]9) }
            'u' {
                if ($script:JsonPosition + 4 -gt $script:JsonText.Length) {
                    throw [FormatException]::new("invalid JSON unicode escape")
                }
                $hex = $script:JsonText.Substring($script:JsonPosition, 4)
                if ($hex -notmatch '^[0-9A-Fa-f]{4}$') {
                    throw [FormatException]::new("invalid JSON unicode escape")
                }
                $code = [Convert]::ToInt32($hex, 16)
                [void]$builder.Append([char]$code)
                $script:JsonPosition += 4
            }
            default { throw [FormatException]::new("invalid JSON escape") }
        }
    }
    throw [FormatException]::new("unterminated JSON string")
}

function Read-JsonNumber {
    $start = $script:JsonPosition
    while ($script:JsonPosition -lt $script:JsonText.Length -and
        $script:JsonText[$script:JsonPosition] -match '[0-9eE+.-]') {
        $script:JsonPosition += 1
    }
    $number = $script:JsonText.Substring($start, $script:JsonPosition - $start)
    if ($number -notmatch '^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$') {
        throw [FormatException]::new("invalid JSON number")
    }
}

function Read-JsonValue {
    Skip-JsonWhitespace
    if ($script:JsonPosition -ge $script:JsonText.Length) {
        throw [FormatException]::new("missing JSON value")
    }
    $character = $script:JsonText[$script:JsonPosition]
    if ($character -eq '{') {
        $script:JsonPosition += 1
        Skip-JsonWhitespace
        $keys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        if ($script:JsonPosition -lt $script:JsonText.Length -and
            $script:JsonText[$script:JsonPosition] -eq '}') {
            $script:JsonPosition += 1
            return
        }
        while ($true) {
            $key = Read-JsonString
            if (-not $keys.Add($key)) { throw [FormatException]::new("duplicate JSON key") }
            Skip-JsonWhitespace
            if ($script:JsonPosition -ge $script:JsonText.Length -or
                $script:JsonText[$script:JsonPosition] -ne ':') {
                throw [FormatException]::new("missing JSON colon")
            }
            $script:JsonPosition += 1
            Read-JsonValue
            Skip-JsonWhitespace
            if ($script:JsonPosition -ge $script:JsonText.Length) {
                throw [FormatException]::new("unterminated JSON object")
            }
            $separator = $script:JsonText[$script:JsonPosition]
            $script:JsonPosition += 1
            if ($separator -eq '}') { return }
            if ($separator -ne ',') { throw [FormatException]::new("invalid JSON object") }
            Skip-JsonWhitespace
        }
    }
    if ($character -eq '[') {
        $script:JsonPosition += 1
        Skip-JsonWhitespace
        if ($script:JsonPosition -lt $script:JsonText.Length -and
            $script:JsonText[$script:JsonPosition] -eq ']') {
            $script:JsonPosition += 1
            return
        }
        while ($true) {
            Read-JsonValue
            Skip-JsonWhitespace
            if ($script:JsonPosition -ge $script:JsonText.Length) {
                throw [FormatException]::new("unterminated JSON array")
            }
            $separator = $script:JsonText[$script:JsonPosition]
            $script:JsonPosition += 1
            if ($separator -eq ']') { return }
            if ($separator -ne ',') { throw [FormatException]::new("invalid JSON array") }
            Skip-JsonWhitespace
        }
    }
    if ($character -eq '"') { [void](Read-JsonString); return }
    if ($character -eq '-' -or $character -match '[0-9]') { Read-JsonNumber; return }
    foreach ($literal in @("true", "false", "null")) {
        if ($script:JsonText.Substring($script:JsonPosition).StartsWith($literal)) {
            $script:JsonPosition += $literal.Length
            return
        }
    }
    throw [FormatException]::new("invalid JSON value")
}

function Read-StrictJson([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -gt 1048576 -or
        ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)) {
        throw [FormatException]::new("invalid metadata encoding")
    }
    $script:JsonText = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
    $script:JsonPosition = 0
    Read-JsonValue
    Skip-JsonWhitespace
    if ($script:JsonPosition -ne $script:JsonText.Length) {
        throw [FormatException]::new("trailing JSON content")
    }
    return $script:JsonText | ConvertFrom-Json
}

function Test-ExactProperties([object]$Value, [string[]]$Expected) {
    if ($null -eq $Value) { return $false }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    return -not [bool](Compare-Object $actual @($Expected | Sort-Object))
}

function Assert-RealPathChain([string]$Path, [bool]$LeafIsDirectory) {
    $candidate = [IO.Path]::GetFullPath($Path)
    $first = $true
    while ($candidate) {
        if (-not (Test-Path -LiteralPath $candidate)) {
            throw [IO.IOException]::new("controlled path is missing")
        }
        $item = Get-Item -LiteralPath $candidate -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw [IO.IOException]::new("controlled path contains a reparse point")
        }
        if ($first -and $item.PSIsContainer -ne $LeafIsDirectory) {
            throw [IO.IOException]::new("controlled path has the wrong type")
        }
        $first = $false
        $parent = [IO.Path]::GetDirectoryName($candidate)
        if (-not $parent -or $parent -eq $candidate) { break }
        $candidate = $parent
    }
}

function Test-Version([object]$Value) {
    return $Value -is [string] -and $Value -cmatch '^[0-9]+\.[0-9]+\.[0-9]+$'
}

function Assert-AbsoluteNormalizedPath([object]$Value) {
    if ($Value -isnot [string] -or -not [IO.Path]::IsPathRooted($Value)) {
        throw [FormatException]::new("invalid absolute path")
    }
    $normalized = [IO.Path]::GetFullPath($Value)
    $pathRoot = [IO.Path]::GetPathRoot($normalized)
    if ($normalized.Equals($pathRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not $normalized.Equals($Value, [StringComparison]::OrdinalIgnoreCase)) {
        throw [FormatException]::new("path is not normalized")
    }
}

function Test-ChecksumKey([string]$Key, [object]$State) {
    if ($Key -cmatch '^(runtime|runtime-content):([0-9]+\.[0-9]+\.[0-9]+)$') {
        return $State.installed_versions -contains $Matches[2]
    }
    if ($Key -cmatch '^officecli:([0-9]+\.[0-9]+\.[0-9]+)$') {
        return $Matches[1] -eq $State.officecli_version
    }
    if ($Key -cmatch '^adapter:(codex|claude):([0-9]+\.[0-9]+\.[0-9]+)$') {
        return $State.installed_versions -contains $Matches[2]
    }
    return $false
}

try {
    if ($env:CSAF_DATA_ROOT) {
        if (-not [IO.Path]::IsPathRooted($env:CSAF_DATA_ROOT)) { Stop-BootstrapRequired }
        $dataRoot = [IO.Path]::GetFullPath($env:CSAF_DATA_ROOT)
    }
    elseif ($env:LOCALAPPDATA) {
        if (-not [IO.Path]::IsPathRooted($env:LOCALAPPDATA)) { Stop-BootstrapRequired }
        $dataRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "CSAF"))
    }
    else {
        if (-not [IO.Path]::IsPathRooted($HOME)) { Stop-BootstrapRequired }
        $dataRoot = [IO.Path]::GetFullPath((Join-Path $HOME "AppData\Local\CSAF"))
    }
    $root = [IO.Path]::GetPathRoot($dataRoot)
    $dataRoot = $dataRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if (-not $dataRoot -or $dataRoot.Equals($root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        Stop-BootstrapRequired
    }
    Assert-RealPathChain $dataRoot $true

    $currentPath = Join-Path $dataRoot "current.json"
    $statePath = Join-Path $dataRoot "state.json"
    Assert-RealPathChain $currentPath $false
    Assert-RealPathChain $statePath $false
    $current = Read-StrictJson $currentPath
    $state = Read-StrictJson $statePath

    if (-not (Test-ExactProperties $current @("schema_version", "active_version", "runtime_path")) -or
        -not (Test-ExactProperties $state @(
            "schema_version", "active_version", "installed_versions", "runtime_paths",
            "verified_checksums", "adapter_targets", "officecli_version", "officecli_path",
            "officecli_sha256", "officecli_installed_by_csaf", "installed_at", "updated_at"
        ))) { Stop-BootstrapRequired }
    if ($current.schema_version -isnot [int] -or $current.schema_version -ne 1 -or
        $state.schema_version -isnot [int] -or $state.schema_version -ne 1 -or
        $current.active_version -isnot [string] -or
        $current.active_version -notmatch '^\d+\.\d+\.\d+$' -or
        $state.active_version -isnot [string] -or
        $state.active_version -ne $current.active_version -or
        $current.runtime_path -isnot [string] -or
        $state.installed_versions -isnot [array] -or
        $state.runtime_paths -isnot [pscustomobject] -or
        $state.verified_checksums -isnot [pscustomobject] -or
        $state.adapter_targets -isnot [pscustomobject] -or
        $state.officecli_version -isnot [string] -or
        $state.officecli_version -notmatch '^\d+\.\d+\.\d+$' -or
        $state.officecli_path -isnot [string] -or
        $state.officecli_sha256 -isnot [string] -or
        $state.officecli_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $state.officecli_installed_by_csaf -isnot [bool] -or
        $state.installed_at -isnot [string] -or $state.updated_at -isnot [string]) {
        Stop-BootstrapRequired
    }
    $installed = @{}
    foreach ($version in @($state.installed_versions)) {
        if (-not (Test-Version $version) -or $installed.ContainsKey($version)) {
            Stop-BootstrapRequired
        }
        $installed[$version] = $true
    }
    if (-not $installed.ContainsKey($current.active_version)) { Stop-BootstrapRequired }

    $runtimeProperties = @($state.runtime_paths.PSObject.Properties)
    if ($runtimeProperties.Count -ne $installed.Count) { Stop-BootstrapRequired }
    foreach ($property in $runtimeProperties) {
        if (-not (Test-Version $property.Name) -or
            -not $installed.ContainsKey($property.Name)) { Stop-BootstrapRequired }
        Assert-AbsoluteNormalizedPath $property.Value
        $expected = [IO.Path]::GetFullPath((Join-Path (Join-Path $dataRoot "versions") $property.Name))
        if (-not $property.Value.Equals($expected, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-BootstrapRequired
        }
        Assert-RealPathChain $property.Value $true
    }
    foreach ($version in $installed.Keys) {
        if ($null -eq $state.runtime_paths.PSObject.Properties[$version]) {
            Stop-BootstrapRequired
        }
    }

    foreach ($property in $state.verified_checksums.PSObject.Properties) {
        if (-not (Test-ChecksumKey $property.Name $state) -or
            $property.Value -isnot [string] -or
            $property.Value -cnotmatch '^[0-9a-f]{64}$') { Stop-BootstrapRequired }
    }
    foreach ($property in $state.adapter_targets.PSObject.Properties) {
        if ($property.Name -cnotmatch '^(codex|claude)$') { Stop-BootstrapRequired }
        Assert-AbsoluteNormalizedPath $property.Value
    }

    Assert-AbsoluteNormalizedPath $current.runtime_path
    Assert-AbsoluteNormalizedPath $state.officecli_path
    $runtimePath = [IO.Path]::GetFullPath($current.runtime_path)
    $expectedRuntime = [IO.Path]::GetFullPath((Join-Path (Join-Path $dataRoot "versions") $current.active_version))
    $recordedRuntime = $state.runtime_paths.PSObject.Properties[$current.active_version].Value
    if ($runtimePath -ne $expectedRuntime -or $recordedRuntime -ne $runtimePath) {
        Stop-BootstrapRequired
    }
    $runtimeLauncher = Join-Path $runtimePath "csaf.exe"
    Assert-RealPathChain (Join-Path $dataRoot "versions") $true
    Assert-RealPathChain $runtimePath $true
    Assert-RealPathChain $runtimeLauncher $false

    $officecliPath = [IO.Path]::GetFullPath($state.officecli_path)
    $expectedOffice = [IO.Path]::GetFullPath((Join-Path (Join-Path (Join-Path $dataRoot "officecli") $state.officecli_version) "officecli.exe"))
    if ($officecliPath -ne $expectedOffice) { Stop-BootstrapRequired }
    Assert-RealPathChain (Join-Path $dataRoot "officecli") $true
    Assert-RealPathChain (Join-Path (Join-Path $dataRoot "officecli") $state.officecli_version) $true
    Assert-RealPathChain $officecliPath $false

    $env:CSAF_DATA_ROOT = $dataRoot
    $env:CSAF_OFFICECLI = $officecliPath
    $env:OFFICECLI_SKIP_UPDATE = "1"

    $updateOutput = @(& $runtimeLauncher "setup" "check-update" 2>$null) -join "`n"
    if ($LASTEXITCODE -eq 0 -and $updateOutput -match '(?im)^Update available\.') {
        [Console]::Error.WriteLine("CSAF update available. Run csaf setup update after explicit consent.")
    }
    & $runtimeLauncher @CsafArgs
    exit $LASTEXITCODE
}
catch {
    Stop-BootstrapRequired
}
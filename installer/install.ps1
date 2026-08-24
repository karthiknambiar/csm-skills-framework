#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Alias("dry-run")][switch]$DryRun,
    [Alias("yes")][switch]$AssumeYes,
    [string]$Version,
    [Alias("manifest")][string]$ManifestPath,
    [Alias("data-root")][string]$DataRoot,
    [string]$Platform,
    [Alias("codex-only")][switch]$CodexOnly,
    [Alias("claude-only")][switch]$ClaudeOnly
)

if ($WhatIfPreference) { $DryRun = $true }
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Stable private bootstrap contract: uv 0.12.3 and Python 3.12.13.
$UvVersion = "0.12.3"
$PythonVersion = "3.12.13"
$OfficeCLIVersion = "1.0.143"
$OfficeCLIMinimum = "1.0.137"
$ReleaseRoot = "https://github.com/karthiknambiar/csm-skills-framework/releases"
$RequiredPlatforms = @(
    "linux-arm64", "linux-x64", "macos-arm64", "macos-x64",
    "windows-arm64", "windows-x64"
)
$UvAssets = @{
    "windows-arm64" = @{
        Url = "https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-pc-windows-msvc.zip"
        Sha256 = "4343217d668727b8a8eb5cad92389a1d2eeead93c89940d1b955ba1bb15462eb"
        Size = 17905068
    }
    "windows-x64" = @{
        Url = "https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-pc-windows-msvc.zip"
        Sha256 = "b23350c79e8ad0192b8124af13a0f17e8d4e4549524785e1aef389ae5a06990e"
        Size = 19013455
    }
}

function Fail([string]$Message) {
    [Console]::Error.WriteLine("CSAF installer failed: $Message")
    exit 2
}

function Test-Version([object]$Value) {
    return $Value -is [string] -and $Value -match '^[0-9]+\.[0-9]+\.[0-9]+$'
}

function Test-Asset([object]$Asset) {
    if ($null -eq $Asset) { return $false }
    $names = @($Asset.PSObject.Properties.Name | Sort-Object)
    if ((Compare-Object $names @("sha256", "size", "url"))) { return $false }
    return (
        $Asset.url -is [string] -and $Asset.url -match '^https://' -and
        $Asset.url -notmatch '/main/' -and
        $Asset.sha256 -is [string] -and $Asset.sha256 -cmatch '^[0-9a-f]{64}$' -and
        ($Asset.size -is [int] -or $Asset.size -is [long]) -and [int64]$Asset.size -gt 0
    )
}

function Test-PlatformAssets([object]$Assets) {
    if ($null -eq $Assets) { return $false }
    $actual = @($Assets.PSObject.Properties.Name | Sort-Object)
    if (Compare-Object $actual $RequiredPlatforms) { return $false }
    foreach ($name in $RequiredPlatforms) {
        if (-not (Test-Asset $Assets.$name)) { return $false }
    }
    return $true
}

function Test-ReleaseManifest([object]$Manifest) {
    if ($null -eq $Manifest) { return $false }
    $names = @($Manifest.PSObject.Properties.Name | Sort-Object)
    $expected = @("claude_plugin", "codex_skill", "officecli", "runtime", "schema_version", "version")
    if (Compare-Object $names $expected) { return $false }
    if ($Manifest.schema_version -ne 1 -or -not (Test-Version $Manifest.version)) { return $false }
    if (-not (Test-PlatformAssets $Manifest.runtime)) { return $false }
    if (-not (Test-Asset $Manifest.codex_skill) -or -not (Test-Asset $Manifest.claude_plugin)) {
        return $false
    }
    $officeNames = @($Manifest.officecli.PSObject.Properties.Name | Sort-Object)
    if (Compare-Object $officeNames @("assets", "minimum_version", "version")) { return $false }
    return (
        $Manifest.officecli.version -eq $OfficeCLIVersion -and
        $Manifest.officecli.minimum_version -eq $OfficeCLIMinimum -and
        (Test-PlatformAssets $Manifest.officecli.assets)
    )
}

function Get-PlatformName {
    if ($Platform) {
        if ($Platform -notin $RequiredPlatforms) { Fail "unsupported platform override" }
        return $Platform
    }
    $architecture = [Environment]::GetEnvironmentVariable("PROCESSOR_ARCHITECTURE")
    if ($architecture -eq "ARM64") { return "windows-arm64" }
    if ($architecture -in @("AMD64", "x86_64")) { return "windows-x64" }
    Fail "Windows x64 or arm64 is required"
}

function Assert-PrivateDirectory([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw [InvalidOperationException]::new(
            "installer-controlled path must be a real private directory"
        )
    }
}

function Initialize-PrivateDirectory([string]$Path) {
    $candidate = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Path]::GetDirectoryName($candidate)
    if (-not $parent) {
        throw [InvalidOperationException]::new(
            "installer-controlled directory path is invalid"
        )
    }
    Assert-PrivateDirectory $parent
    if (Test-Path -LiteralPath $candidate) {
        Assert-PrivateDirectory $candidate
    }
    else {
        New-Item -ItemType Directory -Path $candidate | Out-Null
        Assert-PrivateDirectory $candidate
    }
}

function Assert-SafeFileTarget([string]$Path, [bool]$AllowExisting) {
    $candidate = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Path]::GetDirectoryName($candidate)
    if (-not $parent) {
        throw [InvalidOperationException]::new(
            "installer-controlled file path is invalid"
        )
    }
    Assert-PrivateDirectory $parent
    $existing = Get-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        if ($existing.PSIsContainer -or
            ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not $AllowExisting) {
            throw [InvalidOperationException]::new(
                "installer-controlled file target is unsafe"
            )
        }
    }
}

function Set-PrivateDataRootAcl([string]$Root) {
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $administratorsSid = [Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $forbiddenSids = @("S-1-1-0", "S-1-5-11", "S-1-5-32-545")
    $allowedSids = @(
        $currentSid.Value, $systemSid.Value, $administratorsSid.Value
    )
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($currentSid)
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    foreach ($sid in @($currentSid, $systemSid, $administratorsSid)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Root -AclObject $acl
    $verified = Get-Acl -LiteralPath $Root
    $ownerSid = (
        [Security.Principal.NTAccount]$verified.Owner
    ).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -ne $currentSid.Value -or -not $verified.AreAccessRulesProtected) {
        throw [InvalidOperationException]::new(
            "CSAF data root private ACL could not be verified"
        )
    }
    foreach ($rule in $verified.Access) {
        $ruleSid = $rule.IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if ($rule.IsInherited -or $rule.AccessControlType -ne "Allow" -or
            $ruleSid -notin $allowedSids -or $ruleSid -in $forbiddenSids) {
            throw [InvalidOperationException]::new(
                "CSAF data root private ACL could not be verified"
            )
        }
    }
}

function Initialize-PrivateDataRoot([string]$Root) {
    $candidate = [IO.Path]::GetFullPath($Root)
    if ($candidate.Equals(
        [IO.Path]::GetPathRoot($candidate), [StringComparison]::OrdinalIgnoreCase
    )) {
        throw [InvalidOperationException]::new(
            "CSAF data root must not be a filesystem root"
        )
    }
    $existing = $candidate
    while (-not (Test-Path -LiteralPath $existing)) {
        $parent = [IO.Path]::GetDirectoryName($existing)
        if (-not $parent -or $parent -eq $existing) { break }
        $existing = $parent
    }
    while ($existing) {
        $item = Get-Item -LiteralPath $existing -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw [InvalidOperationException]::new(
                "CSAF data root contains a link or reparse point"
            )
        }
        $parent = [IO.Path]::GetDirectoryName($existing)
        if (-not $parent -or $parent -eq $existing) { break }
        $existing = $parent
    }
    New-Item -ItemType Directory -Path $candidate -Force | Out-Null
    $rootItem = Get-Item -LiteralPath $candidate -Force
    if (-not $rootItem.PSIsContainer -or
        ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw [InvalidOperationException]::new(
            "CSAF data root must be a private regular directory"
        )
    }
    Set-PrivateDataRootAcl $candidate
    Assert-PrivateDirectory $candidate
}

function Invoke-HttpsBoundedDownload(
    [string]$Uri,
    [int]$TimeoutSec,
    [int64]$MaxBytes,
    [string]$Destination
) {
    if ($TimeoutSec -le 0 -or $MaxBytes -le 0) {
        throw [InvalidOperationException]::new("HTTPS request limits are invalid")
    }
    $current = $null
    if (-not [Uri]::TryCreate($Uri, [UriKind]::Absolute, [ref]$current) -or
        $current.Scheme -cne "https" -or $current.UserInfo) {
        throw [InvalidOperationException]::new(
            "HTTPS request URL is invalid or contains userinfo"
        )
    }
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $visited = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    try {
        for ($redirects = 0; $redirects -le 8; $redirects += 1) {
            if (-not $visited.Add($current.AbsoluteUri)) {
                throw [InvalidOperationException]::new("HTTPS redirect loop detected")
            }
            $request = [Net.Http.HttpRequestMessage]::new(
                [Net.Http.HttpMethod]::Get, $current
            )
            $request.Headers.AcceptEncoding.ParseAdd("identity")
            $response = $null
            try {
                $response = $client.SendAsync(
                    $request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead
                ).GetAwaiter().GetResult()
                $status = [int]$response.StatusCode
                if ($status -in @(301, 302, 303, 307, 308)) {
                    if ($redirects -ge 8) {
                        throw [InvalidOperationException]::new(
                            "HTTPS redirect limit exceeded"
                        )
                    }
                    $next = $response.Headers.Location
                    if ($null -eq $next -or -not $next.IsAbsoluteUri -or
                        $next.Scheme -cne "https" -or $next.UserInfo) {
                        throw [InvalidOperationException]::new(
                            "HTTPS redirect Location is invalid, relative, insecure, or contains userinfo"
                        )
                    }
                    $current = $next
                    continue
                }
                if (-not $response.IsSuccessStatusCode) {
                    throw [InvalidOperationException]::new(
                        "HTTPS request returned an invalid status"
                    )
                }
                $declared = $response.Content.Headers.ContentLength
                if ($null -ne $declared -and [int64]$declared -gt $MaxBytes) {
                    throw [InvalidOperationException]::new(
                        "HTTPS response exceeded its size limit"
                    )
                }
                $incoming = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                $outgoing = [IO.File]::Open(
                    $Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                try {
                    $buffer = [byte[]]::new(65536)
                    [int64]$total = 0
                    while (($count = $incoming.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $total += $count
                        if ($total -gt $MaxBytes) {
                            throw [InvalidOperationException]::new(
                                "HTTPS response exceeded its size limit"
                            )
                        }
                        $outgoing.Write($buffer, 0, $count)
                    }
                    $outgoing.Flush($true)
                }
                finally {
                    $outgoing.Dispose()
                    $incoming.Dispose()
                }
                return
            }
            finally {
                if ($null -ne $response) { $response.Dispose() }
                $request.Dispose()
            }
        }
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }
    throw [InvalidOperationException]::new("HTTPS redirect limit exceeded")
}

function Invoke-HttpsManualRedirect(
    [string]$Uri,
    [int]$TimeoutSec,
    [int64]$MaxBytes
) {
    if ($TimeoutSec -le 0 -or $MaxBytes -le 0) {
        throw [InvalidOperationException]::new("HTTPS request limits are invalid")
    }
    $current = $null
    if (-not [Uri]::TryCreate($Uri, [UriKind]::Absolute, [ref]$current) -or
        $current.Scheme -cne "https" -or $current.UserInfo) {
        throw [InvalidOperationException]::new("HTTPS request URL is invalid or contains userinfo")
    }
    $visited = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    for ($redirects = 0; $redirects -le 8; $redirects += 1) {
        if (-not $visited.Add($current.AbsoluteUri)) {
            throw [InvalidOperationException]::new("HTTPS redirect loop detected")
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $current.AbsoluteUri `
                -TimeoutSec $TimeoutSec -MaximumRedirection 0 `
                -Headers @{"Accept-Encoding" = "identity"}
            $status = [int]$response.StatusCode
        }
        catch {
            $raw = if ($_.Exception.PSObject.Properties.Name -contains "Response") {
                $_.Exception.Response
            } else {
                $null
            }
            if ($null -eq $raw) {
                throw [InvalidOperationException]::new("HTTPS request failed")
            }
            $status = [int]$raw.StatusCode
            $response = [pscustomobject]@{
                StatusCode = $status
                Headers = $raw.Headers
                Content = [byte[]]@()
            }
        }
        if ($status -in @(301, 302, 303, 307, 308)) {
            if ($redirects -ge 8) {
                throw [InvalidOperationException]::new("HTTPS redirect limit exceeded")
            }
            $location = [string]$response.Headers["Location"]
            $next = $null
            if (-not $location -or
                -not [Uri]::TryCreate($location, [UriKind]::Absolute, [ref]$next) -or
                $next.Scheme -cne "https" -or $next.UserInfo) {
                throw [InvalidOperationException]::new(
                    "HTTPS redirect Location is invalid, relative, insecure, or contains userinfo"
                )
            }
            $current = $next
            continue
        }
        if ($status -lt 200 -or $status -ge 300) {
            throw [InvalidOperationException]::new("HTTPS request returned an invalid status")
        }
        if ($response.Content -is [byte[]]) {
            $bytes = [byte[]]$response.Content
        }
        elseif ($response.PSObject.Properties.Name -contains "RawContentStream" -and
            $null -ne $response.RawContentStream) {
            $memory = [IO.MemoryStream]::new()
            $buffer = [byte[]]::new(65536)
            while (($count = $response.RawContentStream.Read(
                $buffer, 0, $buffer.Length
            )) -gt 0) {
                if ($memory.Length + $count -gt $MaxBytes) {
                    $memory.Dispose()
                    throw [InvalidOperationException]::new(
                        "HTTPS response exceeded its size limit"
                    )
                }
                $memory.Write($buffer, 0, $count)
            }
            $bytes = [byte[]]::new($memory.Length)
            $memory.Position = 0
            [void]$memory.Read($bytes, 0, $bytes.Length)
            $memory.Dispose()
        }
        else {
            $bytes = [Text.Encoding]::UTF8.GetBytes([string]$response.Content)
        }
        if ($bytes.LongLength -gt $MaxBytes) {
            throw [InvalidOperationException]::new("HTTPS response exceeded its size limit")
        }
        return $bytes
    }
    throw [InvalidOperationException]::new("HTTPS redirect limit exceeded")
}

function Get-ManifestObject([string]$Source) {
    try {
        if (Test-Path -LiteralPath $Source -PathType Leaf) {
            $item = Get-Item -LiteralPath $Source
            if ($item.Length -gt 1048576) { Fail "release manifest is too large" }
            $text = [IO.File]::ReadAllText($item.FullName, [Text.UTF8Encoding]::new($false, $true))
        }
        else {
            if ($Source -notmatch '^https://') { Fail "manifest must be a local file or HTTPS URL" }
            if ($env:CSAF_INSTALLER_NETWORK_FORBIDDEN -eq "1") { Fail "network access is disabled" }
            $manifestDownload = Join-Path (
                [IO.Path]::GetTempPath()
            ) ("csaf-manifest-" + [guid]::NewGuid().ToString("N") + ".json")
            try {
                Invoke-HttpsBoundedDownload $Source 30 1048576 $manifestDownload
                $bytes = [IO.File]::ReadAllBytes($manifestDownload)
            }
            finally {
                if (Test-Path -LiteralPath $manifestDownload) {
                    Remove-Item -LiteralPath $manifestDownload -Force
                }
            }
            $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
        }
        $parsed = $text | ConvertFrom-Json
    }
    catch {
        Fail "release manifest could not be read"
    }
    if (-not (Test-ReleaseManifest $parsed)) { Fail "release manifest is invalid" }
    if ($Version -and $parsed.version -ne $Version) { Fail "requested version does not match manifest" }
    return $parsed
}

function Download-Verified([object]$Asset, [string]$Destination) {
    if ($env:CSAF_INSTALLER_NETWORK_FORBIDDEN -eq "1") { Fail "network access is disabled" }
    Assert-SafeFileTarget $Destination $false
    $partial = "$Destination.partial-$([guid]::NewGuid().ToString("N"))"
    try {
        Invoke-HttpsBoundedDownload (
            [string]$Asset.url
        ) 120 ([int64]$Asset.size) $partial
        $item = Get-Item -LiteralPath $partial
        if ($item.Length -ne [int64]$Asset.size) { Fail "downloaded asset size did not match" }
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $partial).Hash.ToLowerInvariant()
        if ($digest -cne [string]$Asset.sha256) { Fail "downloaded asset checksum did not match" }
        Move-Item -LiteralPath $partial -Destination $Destination
    }
    catch {
        Fail "verified asset download failed"
    }
    finally {
        if (Test-Path -LiteralPath $partial) {
            Remove-Item -LiteralPath $partial -Force
        }
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "private bootstrap command failed" }
}

if ($MyInvocation.InvocationName -eq ".") {
    return
}
if ($CodexOnly -and $ClaudeOnly) { Fail "choose only one assistant override" }
$SelectedPlatform = Get-PlatformName
if (-not $SelectedPlatform.StartsWith("windows-")) { Fail "use install.sh on macOS or Linux" }
if (-not $DataRoot) {
    if (-not $env:LOCALAPPDATA) { Fail "LOCALAPPDATA is unavailable" }
    $DataRoot = Join-Path $env:LOCALAPPDATA "CSAF"
}
if (-not [IO.Path]::IsPathRooted($DataRoot)) { Fail "data root must be absolute" }
$DataRoot = [IO.Path]::GetFullPath($DataRoot)

if ($ManifestPath) {
    $ManifestSource = $ManifestPath
}
elseif ($Version) {
    if (-not (Test-Version $Version)) { Fail "version must use X.Y.Z" }
    $ManifestSource = "$ReleaseRoot/download/v$Version/csaf-release-manifest.json"
}
else {
    $ManifestSource = "$ReleaseRoot/latest/download/csaf-release-manifest.json"
}
$ReleaseManifest = Get-ManifestObject $ManifestSource

$Targets = [Collections.Generic.List[string]]::new()
if ($CodexOnly) {
    $Targets.Add("codex")
}
elseif ($ClaudeOnly) {
    $Targets.Add("claude")
}
else {
    if ($env:CODEX_HOME -or (Get-Command codex -ErrorAction SilentlyContinue)) {
        $Targets.Add("codex")
    }
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        $Targets.Add("claude")
    }
}
$TargetSummary = if ($Targets.Count) { $Targets -join ", " } else { "runtime only (none detected)" }
$UvPath = Join-Path $DataRoot "bin\uv.exe"
$PythonRoot = Join-Path $DataRoot "python"
$UvCache = Join-Path $DataRoot "cache\uv"
$OfficeCLIPath = Join-Path $DataRoot "officecli\$OfficeCLIVersion\officecli.exe"

Write-Output "CSAF $($ReleaseManifest.version) installation plan"
Write-Output "Platform: $SelectedPlatform"
Write-Output "Targets: $TargetSummary"
Write-Output "Data root: $DataRoot"
Write-Output "Private uv 0.12.3: $UvPath"
Write-Output "Private Python 3.12.13: $PythonRoot"
Write-Output "Mandatory OfficeCLI 1.0.143: $OfficeCLIPath"
Write-Output "OfficeCLI is mandatory because QBR PowerPoint and Word generation cannot work without it."
Write-Output "CSAF and OfficeCLI run locally with no API key or hosted AI service."
Write-Output "Release source: $ManifestSource"
Write-Output "Network: verified HTTPS release assets only; normal installed operation is offline."

if ($DryRun) {
    Write-Output "Dry run complete; no downloads or filesystem changes were made."
    exit 0
}
if (-not $AssumeYes) {
    $answer = Read-Host "Install CSAF and mandatory OfficeCLI into every selected assistant? [y/N]"
    if ($answer -notmatch '^[Yy](?:[Ee][Ss])?$') { Fail "installation was declined" }
}

$StagingDirectory = $null
try {
    Initialize-PrivateDataRoot $DataRoot
    Initialize-PrivateDirectory (Join-Path $DataRoot "staging")
    Initialize-PrivateDirectory (Join-Path $DataRoot "bin")
    Initialize-PrivateDirectory (Join-Path $DataRoot "python")
    Initialize-PrivateDirectory (Join-Path $DataRoot "cache")
    Initialize-PrivateDirectory (Join-Path $DataRoot "cache\uv")
    $StagingDirectory = Join-Path $DataRoot (
        "staging\bootstrap-" + [guid]::NewGuid().ToString("N")
    )
    Initialize-PrivateDirectory $StagingDirectory

    $UvArchive = Join-Path $StagingDirectory "uv.zip"
    Assert-SafeFileTarget $UvArchive $false
    Download-Verified $UvAssets[$SelectedPlatform] $UvArchive
    $UvExtracted = Join-Path $StagingDirectory "uv-extracted"
    Initialize-PrivateDirectory $UvExtracted
    Expand-Archive -LiteralPath $UvArchive -DestinationPath $UvExtracted
    Assert-PrivateDirectory $UvExtracted
    $UvCandidate = Get-ChildItem -LiteralPath $UvExtracted -Filter "uv.exe" -File -Recurse |
        Select-Object -First 1
    if ($null -eq $UvCandidate) { Fail "verified uv archive was incomplete" }
    Assert-SafeFileTarget $UvPath $true
    Copy-Item -LiteralPath $UvCandidate.FullName -Destination $UvPath -Force
    Assert-SafeFileTarget $UvPath $true

    $env:UV_UNMANAGED_INSTALL = Join-Path $DataRoot "bin"
    $env:UV_PYTHON_INSTALL_DIR = $PythonRoot
    $env:UV_CACHE_DIR = $UvCache
    $env:UV_NO_CONFIG = "1"
    Remove-Item Env:UV_OFFLINE -ErrorAction SilentlyContinue
    Invoke-Checked $UvPath @("python", "install", $PythonVersion)
    $PythonExecutable = (& $UvPath "python" "find" "--python-preference" "only-managed" $PythonVersion |
        Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        Fail "private Python installation was not found"
    }
    $resolvedPython = [IO.Path]::GetFullPath($PythonExecutable)
    if (-not $resolvedPython.StartsWith($PythonRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "private Python resolved outside the CSAF data root"
    }

    $ManifestFile = Join-Path $StagingDirectory "csaf-release-manifest.json"
    Assert-SafeFileTarget $ManifestFile $false
    $ManifestJson = $ReleaseManifest | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText(
        $ManifestFile, $ManifestJson, [Text.UTF8Encoding]::new($false)
    )
    $RuntimeArchive = Join-Path $StagingDirectory "runtime-bundle.zip"
    $RuntimeAsset = $ReleaseManifest.runtime.$SelectedPlatform
    Download-Verified $RuntimeAsset $RuntimeArchive

    $RuntimeBundle = Join-Path $StagingDirectory "runtime-bundle"
    if (Test-Path -LiteralPath $RuntimeBundle) {
        throw [InvalidOperationException]::new("runtime bundle staging path is unsafe")
    }
    # Validates runtime-bundle.json; kept inline so bootstrap has no unverified helper dependency.
    $BundleValidator = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("aW1wb3J0IGhhc2hsaWIsIGpzb24sIHBhdGhsaWIsIHJlLCBzdGF0LCBzeXMsIHVuaWNvZGVkYXRhLCB6aXBmaWxlCgpkZWYgcmVxdWlyZShjb25kaXRpb24pOgogICAgaWYgbm90IGNvbmRpdGlvbjoKICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJydW50aW1lIGJ1bmRsZSBpcyBpbnZhbGlkIikKCmFyY2hpdmUgPSBwYXRobGliLlBhdGgoc3lzLmFyZ3ZbMV0pCmRlc3RpbmF0aW9uID0gcGF0aGxpYi5QYXRoKHN5cy5hcmd2WzJdKQpwbGF0Zm9ybSA9IHN5cy5hcmd2WzNdCmV4cGVjdGVkX3ZlcnNpb24gPSBzeXMuYXJndls0XQpwbGF0Zm9ybXMgPSB7IndpbmRvd3MteDY0IiwgIndpbmRvd3MtYXJtNjQiLCAibWFjb3MteDY0IiwgIm1hY29zLWFybTY0IiwgImxpbnV4LXg2NCIsICJsaW51eC1hcm02NCJ9CnJlcXVpcmUocGxhdGZvcm0gaW4gcGxhdGZvcm1zKQpyZXF1aXJlKHJlLmZ1bGxtYXRjaChyIlswLTldK1wuWzAtOV0rXC5bMC05XSsiLCBleHBlY3RlZF92ZXJzaW9uKSBpcyBub3QgTm9uZSkKcmVxdWlyZShub3QgZGVzdGluYXRpb24uZXhpc3RzKCkpCndpdGggYXJjaGl2ZS5vcGVuKCJyYiIpIGFzIHNvdXJjZSwgemlwZmlsZS5aaXBGaWxlKHNvdXJjZSkgYXMgYnVuZGxlOgogICAgaW5mb3MgPSBidW5kbGUuaW5mb2xpc3QoKQogICAgcmVxdWlyZShsZW4oaW5mb3MpIDw9IDI1NikKICAgIG5hbWVzID0gW2l0ZW0uZmlsZW5hbWUgZm9yIGl0ZW0gaW4gaW5mb3NdCiAgICByZXF1aXJlKGxlbihzZXQobmFtZXMpKSA9PSBsZW4oaW5mb3MpKQogICAgZm9sZGVkID0ge3VuaWNvZGVkYXRhLm5vcm1hbGl6ZSgiTkZDIiwgbmFtZSkuY2FzZWZvbGQoKSBmb3IgbmFtZSBpbiBuYW1lc30KICAgIHJlcXVpcmUobGVuKGZvbGRlZCkgPT0gbGVuKGluZm9zKSkKICAgIHJlcXVpcmUoc3VtKGl0ZW0uZmlsZV9zaXplIGZvciBpdGVtIGluIGluZm9zKSA8PSAxMDczNzQxODI0KQogICAgZm9yIGl0ZW0gaW4gaW5mb3M6CiAgICAgICAgbmFtZSA9IHBhdGhsaWIuUHVyZVBvc2l4UGF0aChpdGVtLmZpbGVuYW1lKQogICAgICAgIHJlcXVpcmUobGVuKGl0ZW0uZmlsZW5hbWUuZW5jb2RlKCJ1dGYtOCIpKSA8PSA0MDk2KQogICAgICAgIHJlcXVpcmUobm90IGl0ZW0uaXNfZGlyKCkgYW5kIG5vdCBuYW1lLmlzX2Fic29sdXRlKCkgYW5kICIuLiIgbm90IGluIG5hbWUucGFydHMpCiAgICAgICAgcmVxdWlyZShpdGVtLmZpbGVfc2l6ZSA8PSAyNjg0MzU0NTYpCiAgICAgICAgcmVxdWlyZShzdGF0LlNfSUZNVChpdGVtLmV4dGVybmFsX2F0dHIgPj4gMTYpIGluICgwLCBzdGF0LlNfSUZSRUcpKQogICAgbmFtZV9zZXQgPSBzZXQobmFtZXMpCiAgICByZXF1aXJlKCJydW50aW1lLWJ1bmRsZS5qc29uIiBpbiBuYW1lX3NldCkKICAgIG1hbmlmZXN0X2luZm8gPSBidW5kbGUuZ2V0aW5mbygicnVudGltZS1idW5kbGUuanNvbiIpCiAgICByZXF1aXJlKG1hbmlmZXN0X2luZm8uZmlsZV9zaXplIDw9IDEwNDg1NzYpCiAgICBtYW5pZmVzdCA9IGpzb24ubG9hZHMoYnVuZGxlLnJlYWQobWFuaWZlc3RfaW5mbykuZGVjb2RlKCJ1dGYtOCIpKQogICAgcmVxdWlyZSh0eXBlKG1hbmlmZXN0KSBpcyBkaWN0KQogICAgcmVxdWlyZShzZXQobWFuaWZlc3QpID09IHsic2NoZW1hX3ZlcnNpb24iLCAidmVyc2lvbiIsICJwbGF0Zm9ybSIsICJmaWxlcyJ9KQogICAgcmVxdWlyZSh0eXBlKG1hbmlmZXN0WyJzY2hlbWFfdmVyc2lvbiJdKSBpcyBpbnQgYW5kIG1hbmlmZXN0WyJzY2hlbWFfdmVyc2lvbiJdID09IDEpCiAgICByZXF1aXJlKHR5cGUobWFuaWZlc3RbInZlcnNpb24iXSkgaXMgc3RyIGFuZCBtYW5pZmVzdFsidmVyc2lvbiJdID09IGV4cGVjdGVkX3ZlcnNpb24pCiAgICByZXF1aXJlKG1hbmlmZXN0WyJwbGF0Zm9ybSJdID09IHBsYXRmb3JtKQogICAgZmlsZXMgPSBtYW5pZmVzdFsiZmlsZXMiXQogICAgcmVxdWlyZSh0eXBlKGZpbGVzKSBpcyBkaWN0KQogICAgcmVxdWlyZShzZXQoZmlsZXMpID09IG5hbWVfc2V0IC0geyJydW50aW1lLWJ1bmRsZS5qc29uIn0pCiAgICBydW50aW1lX3doZWVsID0gZiJjc2FmLXtleHBlY3RlZF92ZXJzaW9ufS1weTMtbm9uZS1hbnkud2hsIgogICAgcmVxdWlyZShydW50aW1lX3doZWVsIGluIGZpbGVzIGFuZCAicmVxdWlyZW1lbnRzLmxvY2siIGluIGZpbGVzKQogICAgd2hlZWxfbmFtZXMgPSBzb3J0ZWQobmFtZSBmb3IgbmFtZSBpbiBmaWxlcyBpZiBuYW1lLnN0YXJ0c3dpdGgoIndoZWVsaG91c2UvIikgYW5kIG5hbWUuZW5kc3dpdGgoIi53aGwiKSkKICAgIHJlcXVpcmUoYm9vbCh3aGVlbF9uYW1lcykpCiAgICByZXF1aXJlKHNldChmaWxlcykgPT0ge3J1bnRpbWVfd2hlZWwsICJyZXF1aXJlbWVudHMubG9jayIsICp3aGVlbF9uYW1lc30pCiAgICBkaWdlc3RzID0ge30KICAgIGZvciBuYW1lLCBleHBlY3RlZCBpbiBmaWxlcy5pdGVtcygpOgogICAgICAgIHJlcXVpcmUodHlwZShleHBlY3RlZCkgaXMgZGljdCBhbmQgc2V0KGV4cGVjdGVkKSA9PSB7InNoYTI1NiIsICJzaXplIn0pCiAgICAgICAgcmVxdWlyZSh0eXBlKGV4cGVjdGVkWyJzaGEyNTYiXSkgaXMgc3RyIGFuZCByZS5mdWxsbWF0Y2gociJbMC05YS1mXXs2NH0iLCBleHBlY3RlZFsic2hhMjU2Il0pIGlzIG5vdCBOb25lKQogICAgICAgIHJlcXVpcmUodHlwZShleHBlY3RlZFsic2l6ZSJdKSBpcyBpbnQgYW5kIGV4cGVjdGVkWyJzaXplIl0gPiAwKQogICAgICAgIGluZm8gPSBidW5kbGUuZ2V0aW5mbyhuYW1lKQogICAgICAgIHJlcXVpcmUoaW5mby5maWxlX3NpemUgPT0gZXhwZWN0ZWRbInNpemUiXSkKICAgICAgICBkaWdlc3QgPSBoYXNobGliLnNoYTI1NigpCiAgICAgICAgd2l0aCBidW5kbGUub3BlbihpbmZvKSBhcyBpbmNvbWluZzoKICAgICAgICAgICAgd2hpbGUgY2h1bmsgOj0gaW5jb21pbmcucmVhZCgxMDQ4NTc2KToKICAgICAgICAgICAgICAgIGRpZ2VzdC51cGRhdGUoY2h1bmspCiAgICAgICAgcmVxdWlyZShkaWdlc3QuaGV4ZGlnZXN0KCkgPT0gZXhwZWN0ZWRbInNoYTI1NiJdKQogICAgICAgIGRpZ2VzdHNbbmFtZV0gPSBkaWdlc3QuaGV4ZGlnZXN0KCkKICAgIGxvY2tfaW5mbyA9IGJ1bmRsZS5nZXRpbmZvKCJyZXF1aXJlbWVudHMubG9jayIpCiAgICByZXF1aXJlKGxvY2tfaW5mby5maWxlX3NpemUgPD0gMTA0ODU3NikKICAgIGxvY2sgPSBidW5kbGUucmVhZChsb2NrX2luZm8pLmRlY29kZSgidXRmLTgiKS5zcGxpdGxpbmVzKCkKICAgIHJlcXVpcmUoYm9vbChsb2NrKSBhbmQgYWxsKGxpbmUgYW5kIGxpbmUgPT0gbGluZS5zdHJpcCgpIGZvciBsaW5lIGluIGxvY2spKQogICAgcmVxdWlyZShsb2NrWzBdID09IGYiLi97cnVudGltZV93aGVlbH0gLS1oYXNoPXNoYTI1Njp7ZGlnZXN0c1tydW50aW1lX3doZWVsXX0iKQogICAgZXhwZWN0ZWQgPSB7ZiJ7cGF0aGxpYi5QdXJlUG9zaXhQYXRoKG5hbWUpLm5hbWUuc3BsaXQoJy0nKVswXS5yZXBsYWNlKCdfJywgJy0nKX09PXtwYXRobGliLlB1cmVQb3NpeFBhdGgobmFtZSkubmFtZS5zcGxpdCgnLScpWzFdfSAtLWhhc2g9c2hhMjU2OntkaWdlc3RzW25hbWVdfSIgZm9yIG5hbWUgaW4gd2hlZWxfbmFtZXN9CiAgICByZXF1aXJlKHNldChsb2NrWzE6XSkgPT0gZXhwZWN0ZWQgYW5kIGxlbihsb2NrWzE6XSkgPT0gbGVuKGV4cGVjdGVkKSkKICAgIGRlc3RpbmF0aW9uLm1rZGlyKCkKICAgIGZvciBuYW1lLCBleHBlY3RlZF9maWxlIGluIGZpbGVzLml0ZW1zKCk6CiAgICAgICAgaW5mbyA9IGJ1bmRsZS5nZXRpbmZvKG5hbWUpCiAgICAgICAgdGFyZ2V0ID0gZGVzdGluYXRpb24gLyBwYXRobGliLlB1cmVQb3NpeFBhdGgobmFtZSkKICAgICAgICB0YXJnZXQucGFyZW50Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSkKICAgICAgICBkaWdlc3QgPSBoYXNobGliLnNoYTI1NigpCiAgICAgICAgd2l0aCBidW5kbGUub3BlbihpbmZvKSBhcyBpbmNvbWluZywgdGFyZ2V0Lm9wZW4oInhiIikgYXMgb3V0Z29pbmc6CiAgICAgICAgICAgIHdoaWxlIGNodW5rIDo9IGluY29taW5nLnJlYWQoMTA0ODU3Nik6CiAgICAgICAgICAgICAgICBkaWdlc3QudXBkYXRlKGNodW5rKQogICAgICAgICAgICAgICAgb3V0Z29pbmcud3JpdGUoY2h1bmspCiAgICAgICAgcmVxdWlyZShkaWdlc3QuaGV4ZGlnZXN0KCkgPT0gZXhwZWN0ZWRfZmlsZVsic2hhMjU2Il0p"))
    Invoke-Checked $resolvedPython @(
        "-I", "-S", "-c", $BundleValidator, $RuntimeArchive, $RuntimeBundle,
        $SelectedPlatform, ([string]$ReleaseManifest.version)
    )
    Assert-PrivateDirectory $RuntimeBundle

    $env:UV_OFFLINE = "1"
    Invoke-Checked $UvPath @(
        "pip", "install", "--python", $resolvedPython, "--offline", "--no-config",
        "--no-index", "--require-hashes", "--find-links", (Join-Path $RuntimeBundle "wheelhouse"),
        "--requirement", (Join-Path $RuntimeBundle "requirements.lock")
    )

    $env:CSAF_DATA_ROOT = $DataRoot
    if ($CodexOnly) {
        Invoke-Checked $resolvedPython @("-m", "csaf.setup.cli", "install", "--manifest", $ManifestFile, "--yes", "--codex-only")
    }
    elseif ($ClaudeOnly) {
        Invoke-Checked $resolvedPython @("-m", "csaf.setup.cli", "install", "--manifest", $ManifestFile, "--yes", "--claude-only")
    }
    else {
        Invoke-Checked $resolvedPython @("-m", "csaf.setup.cli", "install", "--manifest", $ManifestFile, "--yes")
    }
    Write-Output "CSAF installation is ready. Diagnose later with: csaf setup doctor"
}
catch {
    Fail "installation did not activate; the previous installation remains available"
}
finally {
    if ($StagingDirectory -and (Test-Path -LiteralPath $StagingDirectory)) {
        $expectedPrefix = Join-Path $DataRoot "staging\bootstrap-"
        if ($StagingDirectory.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $StagingDirectory -Recurse -Force
        }
    }
}
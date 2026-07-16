#requires -Version 7.2

<#
.SYNOPSIS
Builds a sanitized, model-free, complete offline Windows release.

.DESCRIPTION
The release is assembled in an allowlisted staging directory. Inno Setup is
allowed to read only that staging directory, never the live project tree.
Use -StageOnly to create and audit staging without invoking ISCC.exe.
#>

[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$SourceRoot = "",
    [string]$OutputRoot = "",
    [switch]$StageOnly,
    [string]$ISCCPath = "",
    [switch]$KeepStaging,
    [switch]$RequireSignedInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultSourceRoot = Split-Path -Parent $ScriptDir

function Write-Step {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[Release] $Message" -ForegroundColor Cyan
}

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-SafeChildPath {
    param(
        [Parameter(Mandatory)][string]$Parent,
        [Parameter(Mandatory)][string]$Child,
        [Parameter(Mandatory)][string]$ExpectedLeafPrefix
    )

    $parentFull = (Get-FullPath $Parent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $childFull = Get-FullPath $Child
    $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $childFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe path outside output root: $childFull"
    }
    if (-not (Split-Path -Leaf $childFull).StartsWith(
        $ExpectedLeafPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing unexpected staging path: $childFull"
    }
}

function Remove-SafeTree {
    param(
        [Parameter(Mandatory)][string]$OutputBase,
        [Parameter(Mandatory)][string]$Target
    )

    Assert-SafeChildPath -Parent $OutputBase -Child $Target -ExpectedLeafPrefix "LingJingAI-"
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}

function Test-ExcludedRelativePath {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][ValidateSet("Application", "Workflow", "RuntimePython", "ComfyUI", "Venv")]
        [string]$Profile
    )

    $normal = $RelativePath.Replace('/', '\').TrimStart('\')
    $segments = @($normal.Split('\', [System.StringSplitOptions]::RemoveEmptyEntries))
    $leaf = if ($segments.Count) { $segments[-1] } else { "" }

    if ($segments | Where-Object { $_ -in @('.git', '.gitnexus', '__pycache__') }) {
        return $true
    }
    if ($leaf -match '(?i)\.(pyc|pyo)$') {
        return $true
    }
    if ($leaf -match '(?i)^(session|account_session|client_instance|workflow_config|config\.local)\.json$') {
        return $true
    }
    if ($leaf -match '(?i)^\.(session|workflow_config)\.lock$' -or $leaf -match '(?i)^gateway\.lock$') {
        return $true
    }

    $first = if ($segments.Count) { $segments[0].ToLowerInvariant() } else { "" }
    $mutableRoots = @('models', 'inputs', 'outputs', 'logs', 'tasks', 'temp', 'cache')
    if ($Profile -in @('Application', 'Workflow', 'RuntimePython') -and $first -in $mutableRoots) {
        return $true
    }
    if ($Profile -eq 'RuntimePython' -and $first -eq 'scripts') {
        return $true
    }
    if ($Profile -eq 'ComfyUI' -and $first -in @(
        'models', 'input', 'inputs', 'output', 'outputs', 'temp', 'user',
        'logs', 'tasks', 'cache'
    )) {
        return $true
    }

    return $false
}

function Copy-AllowlistedTree {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][ValidateSet("Application", "Workflow", "RuntimePython", "ComfyUI", "Venv")]
        [string]$Profile
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required source directory is missing: $Source"
    }

    $sourceFull = (Get-FullPath $Source).TrimEnd('\')
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $sourceFull -Recurse -Force) {
        $relative = $item.FullName.Substring($sourceFull.Length).TrimStart('\')
        if (Test-ExcludedRelativePath -RelativePath $relative -Profile $Profile) {
            continue
        }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not allowed in release input: $($item.FullName)"
        }

        $target = Join-Path $Destination $relative
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
        }
        else {
            $targetParent = Split-Path -Parent $target
            New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }
}

function Copy-RequiredFile {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required release file is missing: $Source"
    }
    if ((Get-Item -LiteralPath $Source -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Reparse points are not allowed in release input: $Source"
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Get-StagedRelativePath {
    param(
        [Parameter(Mandatory)][string]$StageRoot,
        [Parameter(Mandatory)][string]$FilePath
    )
    return $FilePath.Substring($StageRoot.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
}

function Assert-StagingPolicy {
    param([Parameter(Mandatory)][string]$StageRoot)

    $forbiddenPathPatterns = @(
        '(^|/)(\.git|\.gitnexus|__pycache__)(/|$)',
        '(^|/)\.(env)(\.|$)',
        '\.(pyc|pyo|pfx|p12|key|token)$',
        '^(models|inputs|outputs|logs|tasks|temp|cache)(/|$)',
        '^runtime/(requests|inputs|outputs|logs|tasks|temp|cache|env-backups|ui-review|workflow_import_tmp)(/|$)',
        '^runtime/comfyui/(models|input|inputs|output|outputs|temp|user|logs|tasks|cache|\.git)(/|$)',
        '(^|/)(session|account_session|client_instance|workflow_config|config\.local)\.json$',
        '(^|/)\.(session|workflow_config)\.lock$',
        '(^|/)gateway\.lock$'
    )

    $secretPatterns = [ordered]@{
        'private-key' = '-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
        'github-token' = '\bgh[pousr]_[A-Za-z0-9]{30,}\b'
        'aws-access-key' = '\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'
        'api-secret' = '\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b'
    }
    $textExtensions = @(
        '.py', '.json', '.yaml', '.yml', '.md', '.txt', '.bat', '.cmd',
        '.ps1', '.iss', '.toml', '.ini', '.cfg', '.html', '.js', '.css',
        '.svg', '.xml'
    )

    foreach ($file in Get-ChildItem -LiteralPath $StageRoot -Recurse -File -Force) {
        $relative = (Get-StagedRelativePath -StageRoot $StageRoot -FilePath $file.FullName).ToLowerInvariant()
        foreach ($pattern in $forbiddenPathPatterns) {
            if ($relative -match $pattern) {
                throw "Forbidden release member detected: $relative"
            }
        }

        if ($file.Extension.ToLowerInvariant() -in $textExtensions -and $file.Length -le 16MB) {
            $content = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop
            foreach ($entry in $secretPatterns.GetEnumerator()) {
                if ($content -match $entry.Value) {
                    throw "Possible $($entry.Key) detected in release member: $relative"
                }
            }
        }
    }
}

function Resolve-ISCC {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "ISCC.exe was not found: $RequestedPath"
        }
        return (Get-FullPath $RequestedPath)
    }

    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    foreach ($candidate in @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw "Inno Setup 6 was not found. Install it, pass -ISCCPath, or use -StageOnly."
}

$SourceRoot = if ($SourceRoot) { Get-FullPath $SourceRoot } else { Get-FullPath $DefaultSourceRoot }
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "Source root does not exist: $SourceRoot"
}

$versionFile = Join-Path $SourceRoot 'VERSION'
if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) {
    throw "VERSION file is missing: $versionFile"
}
$sourceVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
if (-not $Version) {
    $Version = $sourceVersion
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Version is not a safe semantic version: $Version"
}
if ($Version -ne $sourceVersion) {
    throw "Requested version $Version does not match VERSION file $sourceVersion"
}

$OutputRoot = if ($OutputRoot) { Get-FullPath $OutputRoot } else { Join-Path $SourceRoot 'dist' }
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$OutputRoot = Get-FullPath $OutputRoot

$ProductFolder = "LingJingAI-$Version-win-x64"
$StagingBase = Join-Path $OutputRoot 'staging'
$StageRoot = Join-Path $StagingBase $ProductFolder
$ManifestPath = Join-Path $OutputRoot "$ProductFolder.members.json"
$ManifestHashPath = "$ManifestPath.sha256"
New-Item -ItemType Directory -Path $StagingBase -Force | Out-Null
Remove-SafeTree -OutputBase $StagingBase -Target $StageRoot
New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
foreach ($oldManifest in @($ManifestPath, $ManifestHashPath)) {
    $oldManifestFull = Get-FullPath $oldManifest
    $outputPrefix = $OutputRoot.TrimEnd('\') + '\'
    if (-not $oldManifestFull.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe manifest output path: $oldManifestFull"
    }
    if (Test-Path -LiteralPath $oldManifestFull -PathType Leaf) {
        Remove-Item -LiteralPath $oldManifestFull -Force
    }
}

$buildSucceeded = $false
try {
    Write-Step "Validating release inputs"
    $requiredInputs = @(
        'app\gui\main_gateway.py',
        'app\gui\assets\app.ico',
        'workflows',
        'runtime\python\python.exe',
        'runtime\python\pythonw.exe',
        'runtime\ComfyUI\main.py',
        '.venv\Lib\site-packages\torch\__init__.py',
        '.venv\share',
        'bin\cloudflared.exe',
        'start.bat',
        'check-env.bat',
        'README.md',
        'requirements.lock',
        'requirements-runtime.lock',
        'VERSION'
    )
    $missing = @($requiredInputs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $SourceRoot $_)) })
    if ($missing.Count) {
        throw "Release input is incomplete. Missing: $($missing -join ', ')"
    }

    Write-Step "Copying application allowlist"
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'app') -Destination (Join-Path $StageRoot 'app') -Profile Application
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'workflows') -Destination (Join-Path $StageRoot 'workflows') -Profile Workflow
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'runtime\python') -Destination (Join-Path $StageRoot 'runtime\python') -Profile RuntimePython
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'runtime\ComfyUI') -Destination (Join-Path $StageRoot 'runtime\ComfyUI') -Profile ComfyUI
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot '.venv\Lib') -Destination (Join-Path $StageRoot '.venv\Lib') -Profile Venv
    Copy-AllowlistedTree -Source (Join-Path $SourceRoot '.venv\share') -Destination (Join-Path $StageRoot '.venv\share') -Profile Venv

    Copy-RequiredFile -Source (Join-Path $SourceRoot 'bin\cloudflared.exe') -Destination (Join-Path $StageRoot 'bin\cloudflared.exe')
    foreach ($rootFile in @(
        'start.bat',
        'check-env.bat',
        'README.md',
        'requirements.txt',
        'requirements.lock',
        'requirements-runtime.lock',
        'VERSION',
        'icon.png'
    )) {
        $source = Join-Path $SourceRoot $rootFile
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-RequiredFile -Source $source -Destination (Join-Path $StageRoot $rootFile)
        }
    }
    foreach ($optionalNotice in @('LICENSE', 'LICENSE.txt', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md')) {
        $source = Join-Path $SourceRoot $optionalNotice
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-RequiredFile -Source $source -Destination (Join-Path $StageRoot $optionalNotice)
        }
    }

    $releaseInfo = [ordered]@{
        product = 'LingJingAI'
        version = $Version
        platform = 'windows-x64'
        runtime_layout = 'offline-portable-python-comfyui'
        models_included = $false
        generated_utc = [DateTime]::UtcNow.ToString('o')
    }
    $releaseInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $StageRoot 'release-info.json') -Encoding utf8NoBOM

    Write-Step "Scanning forbidden paths and possible secrets"
    Assert-StagingPolicy -StageRoot $StageRoot

    $requiredStagedFiles = @(
        'app\gui\main_gateway.py',
        'app\gui\assets\app.ico',
        'runtime\python\python.exe',
        'runtime\python\pythonw.exe',
        'runtime\ComfyUI\main.py',
        '.venv\Lib\site-packages\torch\__init__.py',
        'bin\cloudflared.exe',
        'start.bat',
        'check-env.bat',
        'README.md',
        'requirements.lock',
        'requirements-runtime.lock',
        'VERSION',
        'release-info.json'
    )
    $missingStaged = @($requiredStagedFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $StageRoot $_) -PathType Leaf)
    })
    if ($missingStaged.Count) {
        throw "Staging validation failed. Missing: $($missingStaged -join ', ')"
    }

    Write-Step "Generating member manifest with SHA256"
    $members = @(
        Get-ChildItem -LiteralPath $StageRoot -Recurse -File -Force |
            Sort-Object FullName |
            ForEach-Object {
                [ordered]@{
                    path = Get-StagedRelativePath -StageRoot $StageRoot -FilePath $_.FullName
                    size_bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
    )
    if (-not $members.Count) {
        throw "Staging is empty"
    }

    $manifest = [ordered]@{
        product = 'LingJingAI'
        version = $Version
        platform = 'windows-x64'
        member_count = $members.Count
        members = $members
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ManifestPath -Encoding utf8NoBOM
    $manifestHash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$manifestHash  $(Split-Path -Leaf $ManifestPath)" |
        Set-Content -LiteralPath $ManifestHashPath -Encoding ascii

    if ($StageOnly) {
        $buildSucceeded = $true
        Write-Step "Stage-only build complete: $StageRoot"
        Write-Step "Member manifest: $ManifestPath"
        return
    }

    $compiler = Resolve-ISCC -RequestedPath $ISCCPath
    $installerScript = Join-Path $SourceRoot 'installer\LingJing.iss'
    if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) {
        throw "Installer script is missing: $installerScript"
    }

    $installerPath = Join-Path $OutputRoot "LingJingAI-Setup-$Version-win-x64.exe"
    $installerHashPath = "$installerPath.sha256"
    foreach ($oldOutput in @($installerPath, $installerHashPath)) {
        $oldOutputFull = Get-FullPath $oldOutput
        $outputPrefix = $OutputRoot.TrimEnd('\') + '\'
        if (-not $oldOutputFull.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing unsafe installer output path: $oldOutputFull"
        }
        if (Test-Path -LiteralPath $oldOutputFull -PathType Leaf) {
            Remove-Item -LiteralPath $oldOutputFull -Force
        }
    }

    Write-Step "Compiling offline installer"
    $compilerArgs = @(
        "/DMyAppVersion=$Version",
        "/DStageDir=$StageRoot",
        "/DReleaseOutputDir=$OutputRoot",
        $installerScript
    )
    & $compiler @compilerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "ISCC.exe failed with exit code $LASTEXITCODE"
    }

    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "Expected installer output was not created: $installerPath"
    }
    $installer = Get-Item -LiteralPath $installerPath
    if ($installer.Length -ge 2GB) {
        Remove-Item -LiteralPath $installerPath -Force
        throw "Installer exceeds GitHub's 2 GiB per-asset limit: $($installer.Length) bytes"
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        if ($RequireSignedInstaller) {
            Remove-Item -LiteralPath $installerPath -Force
            throw "Installer signature is not valid: $($signature.Status)"
        }
        Write-Warning "Installer is not Authenticode-signed. Do not publish it as a trusted release until it is signed."
    }

    $installerHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$installerHash  $($installer.Name)" |
        Set-Content -LiteralPath $installerHashPath -Encoding ascii

    $buildSucceeded = $true
    Write-Step "Installer: $installerPath"
    Write-Step "SHA256: $installerHash"
}
finally {
    $removeStage = (-not $buildSucceeded) -or (-not $StageOnly -and -not $KeepStaging)
    if ($removeStage -and (Test-Path -LiteralPath $StageRoot)) {
        Remove-SafeTree -OutputBase $StagingBase -Target $StageRoot
    }
    if (-not $buildSucceeded) {
        foreach ($failedManifest in @($ManifestPath, $ManifestHashPath)) {
            if (Test-Path -LiteralPath $failedManifest -PathType Leaf) {
                Remove-Item -LiteralPath $failedManifest -Force
            }
        }
    }
}

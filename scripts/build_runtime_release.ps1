param(
    [string]$Version = "1.0.0",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$DistDir = Join-Path $ProjectDir "dist"
$PackageName = "runtime-nvidia-rtx30plus-cu130-v$Version.7z"
$ArchivePath = Join-Path $DistDir $PackageName
$HashPath = "$ArchivePath.sha256"

$Required = @(
    "runtime\python\python.exe",
    "runtime\ComfyUI\main.py",
    ".venv\Lib\site-packages\torch\__init__.py",
    "bin\cloudflared.exe"
)

$Missing = @($Required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $ProjectDir $_) -PathType Leaf) })
if ($Missing.Count -gt 0) {
    throw "Environment is incomplete. Missing: $($Missing -join ', ')"
}

Write-Host "[Runtime] Package contract is valid." -ForegroundColor Green
Write-Host "[Runtime] Models and user runtime data are excluded by construction."
if ($ValidateOnly) {
    exit 0
}

$SevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source
if (-not $SevenZip) {
    foreach ($Candidate in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe")) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            $SevenZip = $Candidate
            break
        }
    }
}
$Tar = (Get-Command tar.exe -ErrorAction SilentlyContinue).Source
if (-not $Tar) {
    $Tar = (Get-Command bsdtar -ErrorAction SilentlyContinue).Source
}
if (-not $SevenZip -and -not $Tar) {
    throw "7-Zip or Windows bsdtar/tar.exe is required to create the environment package."
}

New-Item -ItemType Directory -Path $DistDir -Force | Out-Null
Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $HashPath -Force -ErrorAction SilentlyContinue

Write-Host "[Runtime] Creating $PackageName ..." -ForegroundColor Cyan
Push-Location $ProjectDir
try {
    if ($SevenZip) {
        $ExcludeArgs = @(
            "-xr!__pycache__",
            "-xr!*.pyc",
            "-xr!runtime\ComfyUI\.git\*",
            "-xr!runtime\ComfyUI\models\*",
            "-xr!runtime\ComfyUI\input\*",
            "-xr!runtime\ComfyUI\output\*",
            "-xr!runtime\ComfyUI\temp\*",
            "-xr!runtime\ComfyUI\user\*"
        )
        & $SevenZip a -t7z -m0=lzma2 -mx=9 -mfb=64 -md=256m -mmt=on -ms=on $ArchivePath ".venv" "runtime\python" "runtime\ComfyUI" "bin\cloudflared.exe" @ExcludeArgs
    }
    else {
        $ExcludeArgs = @(
            "--exclude=.git",
            "--exclude=.git/**",
            "--exclude=__pycache__",
            "--exclude=**/__pycache__/**",
            "--exclude=*.pyc",
            "--exclude=runtime/ComfyUI/models",
            "--exclude=runtime/ComfyUI/models/**",
            "--exclude=runtime/ComfyUI/input",
            "--exclude=runtime/ComfyUI/input/**",
            "--exclude=runtime/ComfyUI/output",
            "--exclude=runtime/ComfyUI/output/**",
            "--exclude=runtime/ComfyUI/temp",
            "--exclude=runtime/ComfyUI/temp/**",
            "--exclude=runtime/ComfyUI/user",
            "--exclude=runtime/ComfyUI/user/**"
        )
        & $Tar -a -cf $ArchivePath @ExcludeArgs ".venv" "runtime/python" "runtime/ComfyUI" "bin/cloudflared.exe"
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Archive creation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Members = @(& $Tar -tf $ArchivePath)
if ($LASTEXITCODE -ne 0) {
    throw "The generated archive cannot be listed."
}

$RequiredArchive = @(
    "runtime/python/python.exe",
    "runtime/ComfyUI/main.py",
    ".venv/Lib/site-packages/torch/__init__.py",
    "bin/cloudflared.exe"
)
$NormalisedMembers = @($Members | ForEach-Object {
    $Member = $_.Replace('\', '/')
    if ($Member.StartsWith('./')) {
        $Member = $Member.Substring(2)
    }
    $Member.TrimEnd('/')
})
$MissingArchive = @($RequiredArchive | Where-Object { $_ -notin $NormalisedMembers })
if ($MissingArchive.Count -gt 0) {
    throw "Generated archive has an invalid root layout. Missing: $($MissingArchive -join ', ')"
}

$Forbidden = @($NormalisedMembers | Where-Object {
    $_ -match '^models(/|$)' -or
    $_ -match '^runtime/ComfyUI/models/.+' -or
    $_ -match '^runtime/(account_session|client_instance|session|workflow_config)\.json$' -or
    $_ -match '^runtime/(requests|outputs|logs|tasks|temp)(/|$)'
})
if ($Forbidden.Count -gt 0) {
    throw "Generated archive contains forbidden model or user-data paths: $($Forbidden[0..([Math]::Min(9, $Forbidden.Count - 1))] -join ', ')"
}

$Archive = Get-Item -LiteralPath $ArchivePath
if ($Archive.Length -ge 2000MB) {
    throw "Release asset is $([Math]::Round($Archive.Length / 1MB, 0)) MiB; the project keeps a safety margin below GitHub's 2 GiB hard limit."
}

$Hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
"$Hash  $PackageName" | Set-Content -LiteralPath $HashPath -Encoding utf8

Write-Host "[Runtime] Created: $ArchivePath" -ForegroundColor Green
Write-Host "[Runtime] Size: $([Math]::Round($Archive.Length / 1GB, 3)) GiB"
Write-Host "[Runtime] SHA256: $Hash"

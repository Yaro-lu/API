# Build Portable Package
param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$DistDir = Join-Path $ProjectDir "dist"
$PackageName = "AIWorker_Portable_$Version"
$PackageDir = Join-Path $DistDir $PackageName

function Write-Log {
    param($Message)
    Write-Host "[Portable] $Message" -ForegroundColor Green
}

try {
    Write-Log "Starting portable build..."

    if (Test-Path $PackageDir) {
        Remove-Item -Path $PackageDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null

    $ItemsToCopy = @("app", "config", "workflows", "models", "inputs", "outputs", "VERSION")
    foreach ($Item in $ItemsToCopy) {
        $Source = Join-Path $ProjectDir $Item
        $Dest = Join-Path $PackageDir $Item
        if (Test-Path $Source) {
            Write-Log "Copying $Item..."
            Copy-Item -Path $Source -Destination $Dest -Recurse -Force
        }
    }

    $RuntimeSource = Join-Path $ProjectDir "runtime"
    $RuntimeDest = Join-Path $PackageDir "runtime"
    if (Test-Path $RuntimeSource) {
        Write-Log "Copying runtime..."
        Copy-Item -Path $RuntimeSource -Destination $RuntimeDest -Recurse -Force
    }

    Write-Log "Creating 7z archive..."
    $7zPath = "7z.exe"
    if (-not (Get-Command $7zPath -ErrorAction SilentlyContinue)) {
        Write-Log "7z not found, please install 7-Zip"
        exit 1
    }

    $ArchivePath = Join-Path $DistDir "$PackageName.7z"
    Push-Location $DistDir
    & $7zPath a -t7z -m0=lzma2 -mx=9 -mfb=64 -md=32m -ms=on $ArchivePath $PackageName
    Pop-Location

    Write-Log "Calculating SHA256..."
    $Hash = Get-FileHash -Path $ArchivePath -Algorithm SHA256
    "$($Hash.Hash)  $(Split-Path -Leaf $ArchivePath)" | Out-File -FilePath "$ArchivePath.sha256" -Encoding UTF8

    Write-Log "Portable build completed: $ArchivePath"
}
catch {
    Write-Log "Error: $_"
    exit 1
}

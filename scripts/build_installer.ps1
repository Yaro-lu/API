# Build Installer
param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$DistDir = Join-Path $ProjectDir "dist"
$InstallerScript = Join-Path $ProjectDir "installer" "AIWorker.iss"

function Write-Log {
    param($Message)
    Write-Host "[Installer] $Message" -ForegroundColor Magenta
}

try {
    Write-Log "Starting installer build..."

    $ISCCPath = "ISCC.exe"
    if (-not (Get-Command $ISCCPath -ErrorAction SilentlyContinue)) {
        $ISCCPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        if (-not (Test-Path $ISCCPath)) {
            Write-Log "Inno Setup not found, please install Inno Setup 6"
            exit 1
        }
    }

    if (-not (Test-Path $InstallerScript)) {
        Write-Log "Installer script not found: $InstallerScript"
        exit 1
    }

    Write-Log "Compiling installer..."
    & $ISCCPath "/DMyAppVersion=$Version" $InstallerScript

    $InstallerPath = Join-Path $DistDir "AIWorker_Setup_$Version.exe"
    if (Test-Path $InstallerPath) {
        Write-Log "Calculating SHA256..."
        $Hash = Get-FileHash -Path $InstallerPath -Algorithm SHA256
        "$($Hash.Hash)  $(Split-Path -Leaf $InstallerPath)" | Out-File -FilePath "$InstallerPath.sha256" -Encoding UTF8

        Write-Log "Installer build completed: $InstallerPath"
    }
    else {
        Write-Log "Installer output not found"
        exit 1
    }
}
catch {
    Write-Log "Error: $_"
    exit 1
}

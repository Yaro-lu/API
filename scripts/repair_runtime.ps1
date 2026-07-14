# Repair Runtime Script
param(
    [ValidateSet("quick", "full")]
    [string]$Mode = "quick"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$LogsDir = Join-Path $ProjectDir "logs"
$Report = @{
    timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    mode = $Mode
    steps = @()
    success = $false
}
$ExitCode = 1

function Write-Log {
    param([string]$Message)
    Write-Host "[Repair] $Message" -ForegroundColor Cyan
    $Report.steps += $Message
}

try {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    Write-Log "Starting repair in $Mode mode..."

    $Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $LogFiles = @(Get-ChildItem -LiteralPath $LogsDir -File -ErrorAction SilentlyContinue)
    if ($LogFiles.Count -gt 0) {
        $BackupDir = Join-Path $LogsDir "repair_backups\$Timestamp"
        New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
        $LogFiles | Copy-Item -Destination $BackupDir -Force
        Write-Log "Backed up $($LogFiles.Count) log file(s)."
    }

    if ($Mode -eq "full") {
        throw "Full repair is unavailable because no verified runtime archive and SHA256 manifest are configured. Existing runtime was left unchanged."
    }

    $PythonPath = Join-Path $ProjectDir "runtime\python\python.exe"
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Portable Python was not found: $PythonPath"
    }

    $RequirementsPath = Join-Path $ProjectDir "requirements.lock"
    if (-not (Test-Path -LiteralPath $RequirementsPath -PathType Leaf)) {
        throw "Locked requirements file was not found: $RequirementsPath"
    }

    Write-Log "Removing the unused, undeclared Paramiko package when present..."
    & $PythonPath -m pip --disable-pip-version-check uninstall --yes paramiko
    if ($LASTEXITCODE -ne 0) {
        throw "Unused Paramiko removal failed with exit code $LASTEXITCODE."
    }

    Write-Log "Installing the locked Python dependency closure..."
    & $PythonPath -m pip --disable-pip-version-check install --no-input --only-binary=:all: --no-deps -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE."
    }

    Write-Log "Checking Python dependency consistency..."
    & $PythonPath -m pip --disable-pip-version-check check
    if ($LASTEXITCODE -ne 0) {
        throw "pip check failed with exit code $LASTEXITCODE."
    }

    Write-Log "Checking required Python imports..."
    & $PythonPath -c "import fastapi, uvicorn, requests, pydantic, yaml, psutil, customtkinter"
    if ($LASTEXITCODE -ne 0) {
        throw "Required import check failed with exit code $LASTEXITCODE."
    }

    $Report.success = $true
    $ExitCode = 0
    Write-Log "Repair completed."
}
catch {
    $Report.error = "$_"
    Write-Log "Repair failed: $_"
}
finally {
    try {
        New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
        $ReportPath = Join-Path $LogsDir "repair_report.json"
        $Report | ConvertTo-Json -Depth 10 | Out-File -FilePath $ReportPath -Encoding UTF8
        Write-Host "[Repair] Report saved to: $ReportPath" -ForegroundColor Cyan
    }
    catch {
        Write-Error "Failed to write repair report: $_" -ErrorAction Continue
        $ExitCode = 1
    }
}

exit $ExitCode

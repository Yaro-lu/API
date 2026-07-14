# Preflight Check Script
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$LogsDir = Join-Path $ProjectDir "logs"
$Report = @{
    timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    checks = @{}
    success = $true
}
$ExitCode = 1

function Write-Log {
    param($Message)
    Write-Host "[Preflight] $Message" -ForegroundColor Yellow
}

try {
    if (-not (Test-Path $LogsDir)) {
        New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
    }

    Write-Log "Checking nvidia-smi..."
    try {
        $nvidiaOutput = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>&1
        $Report.checks.nvidia_smi = @{
            available = $LASTEXITCODE -eq 0
            output = $nvidiaOutput
        }
    }
    catch {
        $Report.checks.nvidia_smi = @{
            available = $false
            error = "$_"
        }
        $Report.success = $false
    }

    Write-Log "Checking Python..."
    $PythonPath = Join-Path $ProjectDir "runtime\python\python.exe"
    if (Test-Path $PythonPath) {
        $Report.checks.python = @{ available = $true; path = $PythonPath }

        Write-Log "Checking Python dependency consistency..."
        try {
            $PipCheckOutput = @(& $PythonPath -m pip --disable-pip-version-check check 2>&1)
            $PipCheckExitCode = $LASTEXITCODE
            $Report.checks.python_dependencies = @{
                available = $PipCheckExitCode -eq 0
                output = @($PipCheckOutput | ForEach-Object { "$_" })
            }
            if ($PipCheckExitCode -ne 0) {
                $Report.success = $false
            }
        }
        catch {
            $Report.checks.python_dependencies = @{
                available = $false
                error = "$_"
            }
            $Report.success = $false
        }

        Write-Log "Checking required Python imports..."
        try {
            $ImportCheckOutput = @(& $PythonPath -c "import fastapi, uvicorn, requests, pydantic, yaml, psutil, customtkinter" 2>&1)
            $ImportCheckExitCode = $LASTEXITCODE
            $Report.checks.python_imports = @{
                available = $ImportCheckExitCode -eq 0
                output = @($ImportCheckOutput | ForEach-Object { "$_" })
            }
            if ($ImportCheckExitCode -ne 0) {
                $Report.success = $false
            }
        }
        catch {
            $Report.checks.python_imports = @{
                available = $false
                error = "$_"
            }
            $Report.success = $false
        }
    }
    else {
        $Report.checks.python = @{ available = $false }
        $Report.checks.python_dependencies = @{ available = $false; skipped = "Portable Python is missing" }
        $Report.checks.python_imports = @{ available = $false; skipped = "Portable Python is missing" }
        $Report.success = $false
    }

    Write-Log "Checking ComfyUI..."
    $ComfyUIPath = Join-Path $ProjectDir "runtime\ComfyUI\main.py"
    if (Test-Path $ComfyUIPath) {
        $Report.checks.comfyui = @{ available = $true; path = $ComfyUIPath }
    }
    else {
        $Report.checks.comfyui = @{ available = $false }
        $Report.success = $false
    }

    Write-Log "Checking FFmpeg..."
    $FFmpegPath = Join-Path $ProjectDir "runtime\ffmpeg\bin\ffmpeg.exe"
    if (Test-Path $FFmpegPath) {
        $Report.checks.ffmpeg = @{ available = $true; path = $FFmpegPath }
    }
    else {
        $Report.checks.ffmpeg = @{ available = $false }
    }

    Write-Log "Checks completed: $(if ($Report.success) { 'PASSED' } else { 'FAILED' })"
}
catch {
    $Report.error = "$_"
    $Report.success = $false
}
finally {
    try {
        New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
        $ReportPath = Join-Path $LogsDir "preflight_report.json"
        $Report | ConvertTo-Json -Depth 10 | Out-File -FilePath $ReportPath -Encoding UTF8
        Write-Log "Report saved to: $ReportPath"
        if ($Report.success) {
            $ExitCode = 0
        }
    }
    catch {
        $Report.success = $false
        Write-Error "Failed to write preflight report: $_" -ErrorAction Continue
        $ExitCode = 1
    }
}

exit $ExitCode

# Build Runtime Script
param(
    [switch]$SkipPython,
    [switch]$SkipComfyUI,
    [switch]$SkipFFmpeg
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$RuntimeDir = Join-Path $ProjectDir "runtime"
$Report = @{
    timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    steps = @()
    success = $false
}

function Write-Log {
    param($Message)
    Write-Host "[Build] $Message" -ForegroundColor Cyan
    $Report.steps += $Message
}

try {
    Write-Log "Starting runtime build..."

    if (-not $SkipPython) {
        Write-Log "Setting up Python..."
        $PythonDir = Join-Path $RuntimeDir "python"
        if (-not (Test-Path $PythonDir)) {
            New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null
            Write-Log "Please download Python embeddable package to $PythonDir"
        }
    }

    if (-not $SkipComfyUI) {
        Write-Log "Setting up ComfyUI..."
        $ComfyUIDir = Join-Path $RuntimeDir "ComfyUI"
        if (-not (Test-Path $ComfyUIDir)) {
            Write-Log "Please clone ComfyUI to $ComfyUIDir"
        }
    }

    if (-not $SkipFFmpeg) {
        Write-Log "Setting up FFmpeg..."
        $FFmpegDir = Join-Path $RuntimeDir "ffmpeg"
        if (-not (Test-Path $FFmpegDir)) {
            New-Item -ItemType Directory -Path $FFmpegDir -Force | Out-Null
            Write-Log "Please download FFmpeg to $FFmpegDir"
        }
    }

    $Report.success = $true
    Write-Log "Runtime build completed successfully!"
}
catch {
    Write-Log "Error: $_"
    $Report.error = "$_"
}
finally {
    $ReportPath = Join-Path $ProjectDir "logs" "runtime_build_report.json"
    $Report | ConvertTo-Json -Depth 10 | Out-File -FilePath $ReportPath -Encoding UTF8
}

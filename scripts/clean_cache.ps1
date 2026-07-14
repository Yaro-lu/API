# Clean Cache Script
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

function Write-Log {
    param($Message)
    Write-Host "[Clean] $Message" -ForegroundColor Gray
}

try {
    $CacheDir = Join-Path $ProjectDir "cache"
    if (Test-Path $CacheDir) {
        Write-Log "Cleaning cache directory..."
        Remove-Item -Path "$CacheDir\*" -Recurse -Force
        Write-Log "Cache cleaned"
    }
    else {
        Write-Log "Cache directory not found"
    }

    $ComfyUITemp = Join-Path $ProjectDir "runtime\ComfyUI\temp"
    if (Test-Path $ComfyUITemp) {
        Write-Log "Cleaning ComfyUI temp..."
        Remove-Item -Path "$ComfyUITemp\*" -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Log "Clean completed!"
}
catch {
    Write-Log "Error: $_"
}

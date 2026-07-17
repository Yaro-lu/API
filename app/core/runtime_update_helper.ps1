#requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(1, 2147483647)][int]$ParentPid,
    [Parameter(Mandatory)][string]$BaseDir,
    [Parameter(Mandatory)][string]$StagingDir,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
    [ValidateRange(30, 600)][int]$WaitTimeoutSeconds = 180,
    [switch]$NoRestart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ManagedRoots = @(
    '.venv\Lib',
    '.venv\share',
    'runtime\python',
    'runtime\ComfyUI',
    'bin\cloudflared.exe'
)
$RequiredRoots = @(
    '.venv\Lib',
    'runtime\python',
    'runtime\ComfyUI',
    'bin\cloudflared.exe'
)
$LegacyRoots = @(
    '.venv\Scripts',
    '.venv\Include',
    '.venv\pyvenv.cfg'
)
$RequiredFiles = @(
    'runtime\python\python.exe',
    'runtime\ComfyUI\main.py',
    '.venv\Lib\site-packages\torch\__init__.py',
    'bin\cloudflared.exe'
)

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-PathEquals {
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )
    return (Get-FullPath $Left).Equals(
        (Get-FullPath $Right),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-DirectSafeChild {
    param(
        [Parameter(Mandatory)][string]$Parent,
        [Parameter(Mandatory)][string]$Child,
        [Parameter(Mandatory)][string]$LeafPattern
    )
    $parentFull = (Get-FullPath $Parent).TrimEnd('\')
    $childFull = (Get-FullPath $Child).TrimEnd('\')
    if (-not (Test-PathEquals (Split-Path -Parent $childFull) $parentFull)) {
        throw "Unsafe child path outside client directory: $childFull"
    }
    if ((Split-Path -Leaf $childFull) -notmatch $LeafPattern) {
        throw "Unsafe child path name: $childFull"
    }
    return $childFull
}

function Assert-ManagedDestination {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Base
    )
    foreach ($relative in @($ManagedRoots + $LegacyRoots)) {
        if (Test-PathEquals $Path (Join-Path $Base $relative)) {
            return
        }
    }
    throw "Refusing unmanaged runtime path: $Path"
}

function Move-WithRetry {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination -Force
            return
        }
        catch {
            if ($attempt -eq 20) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Remove-ManagedPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Base
    )
    Assert-ManagedDestination -Path $Path -Base $Base
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Write-UpdateResult {
    param(
        [Parameter(Mandatory)][bool]$Success,
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][bool]$RolledBack
    )
    $runtimeDir = Join-Path $script:BaseFull 'runtime'
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $resultPath = Join-Path $runtimeDir 'runtime-update-result.json'
    $temporary = Join-Path $runtimeDir "runtime-update-result.$OperationId.tmp"
    [ordered]@{
        schema_version = 1
        operation_id = $OperationId
        success = $Success
        result_code = $Code
        rolled_back = $RolledBack
        message = $Message
        completed_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $resultPath -Force
}

function Start-Client {
    if ($NoRestart) { return }
    $python = Join-Path $script:BaseFull 'runtime\python\pythonw.exe'
    $entrypoint = Join-Path $script:BaseFull 'app\gui\main_gateway.py'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Client Python is missing after runtime update: $python"
    }
    if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
        throw "Client entrypoint is missing after runtime update: $entrypoint"
    }
    $arguments = "-s -B `"$entrypoint`""
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:BaseFull
}

$BaseFull = Get-FullPath $BaseDir
if (-not (Test-Path -LiteralPath $BaseFull -PathType Container)) {
    throw "Client directory does not exist: $BaseFull"
}
$root = [System.IO.Path]::GetPathRoot($BaseFull).TrimEnd('\')
if ($BaseFull.TrimEnd('\').Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to update a drive root'
}

$StagingFull = Assert-DirectSafeChild `
    -Parent $BaseFull `
    -Child $StagingDir `
    -LeafPattern "^\.runtime-install-staging-$OperationId$"
if (-not (Test-Path -LiteralPath $StagingFull -PathType Container)) {
    throw "Runtime staging directory does not exist: $StagingFull"
}
$BackupFull = Assert-DirectSafeChild `
    -Parent $BaseFull `
    -Child (Join-Path $BaseFull ".runtime-install-backup-$OperationId") `
    -LeafPattern "^\.runtime-install-backup-$OperationId$"
if (Test-Path -LiteralPath $BackupFull) {
    throw "Runtime backup directory already exists: $BackupFull"
}
foreach ($relative in $RequiredFiles) {
    $required = Join-Path $StagingFull $relative
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Runtime staging is incomplete: $relative"
    }
    if ((Get-Item -LiteralPath $required).Length -le 0) {
        throw "Runtime staging contains an empty core file: $relative"
    }
}

$parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
if ($null -ne $parent) {
    Wait-Process -InputObject $parent -Timeout $WaitTimeoutSeconds -ErrorAction SilentlyContinue
    if (-not $parent.HasExited) {
        throw "Timed out waiting for client process $ParentPid to exit"
    }
}

$movedExisting = [System.Collections.Generic.List[object]]::new()
$installed = [System.Collections.Generic.List[string]]::new()
$transactionSucceeded = $false
$rollbackSucceeded = $false
$failureMessage = ''

try {
    New-Item -ItemType Directory -Path $BackupFull | Out-Null
    foreach ($relative in $ManagedRoots) {
        $source = Join-Path $StagingFull $relative
        $destination = Join-Path $BaseFull $relative
        Assert-ManagedDestination -Path $destination -Base $BaseFull
        if (-not (Test-Path -LiteralPath $source)) {
            if ($relative -in $RequiredRoots) {
                throw "Runtime staging is missing a managed root: $relative"
            }
            continue
        }
        if (Test-Path -LiteralPath $destination) {
            $backup = Join-Path $BackupFull $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Move-WithRetry -Source $destination -Destination $backup
            $movedExisting.Add([pscustomobject]@{ Backup = $backup; Destination = $destination })
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Move-WithRetry -Source $source -Destination $destination
        $installed.Add($destination)
    }

    foreach ($relative in $LegacyRoots) {
        $destination = Join-Path $BaseFull $relative
        Assert-ManagedDestination -Path $destination -Base $BaseFull
        if (Test-Path -LiteralPath $destination) {
            $backup = Join-Path $BackupFull $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Move-WithRetry -Source $destination -Destination $backup
            $movedExisting.Add([pscustomobject]@{ Backup = $backup; Destination = $destination })
        }
    }

    foreach ($relative in $RequiredFiles) {
        $required = Join-Path $BaseFull $relative
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Runtime verification failed after install: $relative"
        }
        if ((Get-Item -LiteralPath $required).Length -le 0) {
            throw "Runtime verification found an empty file: $relative"
        }
    }
    $transactionSucceeded = $true
}
catch {
    $failureMessage = $_.Exception.Message
    try {
        for ($index = $installed.Count - 1; $index -ge 0; $index--) {
            Remove-ManagedPath -Path $installed[$index] -Base $BaseFull
        }
        for ($index = $movedExisting.Count - 1; $index -ge 0; $index--) {
            $item = $movedExisting[$index]
            if (Test-Path -LiteralPath $item.Destination) {
                Remove-ManagedPath -Path $item.Destination -Base $BaseFull
            }
            New-Item -ItemType Directory -Path (Split-Path -Parent $item.Destination) -Force | Out-Null
            Move-WithRetry -Source $item.Backup -Destination $item.Destination
        }
        $rollbackSucceeded = $true
    }
    catch {
        $failureMessage = "$failureMessage; rollback failed: $($_.Exception.Message)"
        $rollbackSucceeded = $false
    }
}
finally {
    if (Test-Path -LiteralPath $StagingFull) {
        $safeStage = Assert-DirectSafeChild `
            -Parent $BaseFull `
            -Child $StagingFull `
            -LeafPattern "^\.runtime-install-staging-$OperationId$"
        Remove-Item -LiteralPath $safeStage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($transactionSucceeded -or $rollbackSucceeded) {
        if (Test-Path -LiteralPath $BackupFull) {
            $safeBackup = Assert-DirectSafeChild `
                -Parent $BaseFull `
                -Child $BackupFull `
                -LeafPattern "^\.runtime-install-backup-$OperationId$"
            Remove-Item -LiteralPath $safeBackup -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($transactionSucceeded) {
    Write-UpdateResult -Success $true -RolledBack $false -Code 'installed' -Message 'Runtime installation completed.'
    try { Start-Client } catch {
        Write-UpdateResult -Success $true -RolledBack $false -Code 'installed_restart_failed' -Message $_.Exception.Message
        exit 2
    }
    exit 0
}

$message = if ($rollbackSucceeded) {
    $failureMessage
}
else {
    $failureMessage
}
if ($rollbackSucceeded) {
    Write-UpdateResult -Success $false -RolledBack $true -Code 'install_failed_rolled_back' -Message $message
}
else {
    Write-UpdateResult -Success $false -RolledBack $false -Code 'install_failed_rollback_incomplete' -Message $message
}
try { Start-Client } catch { exit 3 }
exit 1

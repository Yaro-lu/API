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
$script:BaseFull = $null
$script:LogPath = $null
$script:UpdateMutex = $null
$script:UpdateMutexName = $null
$script:UpdateMutexOwned = $false
$script:UpdateMutexAbandoned = $false

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Get-ProcessIfRunningStrict {
    param([Parameter(Mandatory)][int]$ProcessId)
    try {
        return [System.Diagnostics.Process]::GetProcessById($ProcessId)
    }
    catch [System.ArgumentException] {
        # GetProcessById uses ArgumentException only when the PID does not
        # exist.  Permission and system-query failures must remain fatal.
        return $null
    }
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

function Initialize-UpdateLog {
    $runtimeDir = Join-Path $script:BaseFull 'runtime'
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $script:LogPath = Join-Path $runtimeDir 'runtime-update-helper.log'
}

function Write-UpdateLog {
    param([Parameter(Mandatory)][string]$Message)
    if ([string]::IsNullOrWhiteSpace($script:LogPath)) { return }
    try {
        $singleLine = $Message -replace "(`r`n|`n|`r)", ' '
        $line = "{0} operation={1} {2}" -f `
            [DateTime]::UtcNow.ToString('o'), $OperationId, $singleLine
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    }
    catch {
        # Logging must never hide the update result or prevent client recovery.
    }
}

function Get-UpdateMutexName {
    param([Parameter(Mandatory)][string]$Base)
    $canonical = (Get-FullPath $Base).TrimEnd('\').ToUpperInvariant()
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($encoding.GetBytes($canonical))
    }
    finally {
        $sha256.Dispose()
    }
    $hex = [System.BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
    return "Local\LingJingAI.RuntimeUpdate.Transaction.$hex"
}

function Enter-UpdateMutex {
    param([Parameter(Mandatory)][string]$Base)
    $script:UpdateMutexName = Get-UpdateMutexName -Base $Base
    $mutex = $null
    try {
        $mutex = [System.Threading.Mutex]::new($false, $script:UpdateMutexName)
        try {
            $script:UpdateMutexOwned = $mutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $script:UpdateMutexOwned = $true
            $script:UpdateMutexAbandoned = $true
        }
        if (-not $script:UpdateMutexOwned) {
            $mutex.Dispose()
            return $false
        }
        $script:UpdateMutex = $mutex
        return $true
    }
    catch {
        if ($null -ne $mutex -and -not $script:UpdateMutexOwned) {
            $mutex.Dispose()
        }
        throw
    }
}

function Exit-UpdateMutex {
    if ($null -eq $script:UpdateMutex) { return }
    if ($script:UpdateMutexOwned) {
        try {
            $script:UpdateMutex.ReleaseMutex()
            Write-UpdateLog "MUTEX_RELEASED name=$script:UpdateMutexName"
        }
        catch {
            Write-UpdateLog "MUTEX_RELEASE_FAILED message=$($_.Exception.Message)"
        }
        finally {
            $script:UpdateMutexOwned = $false
        }
    }
    try {
        $script:UpdateMutex.Dispose()
    }
    catch {
        Write-UpdateLog "MUTEX_DISPOSE_FAILED message=$($_.Exception.Message)"
    }
    $script:UpdateMutex = $null
}

function Write-StartedMarker {
    $runtimeDir = Join-Path $script:BaseFull 'runtime'
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $startedPath = Join-Path $runtimeDir "runtime-update-started-$OperationId.json"
    $temporary = Join-Path $runtimeDir "runtime-update-started-$OperationId.tmp"
    [ordered]@{
        schema_version = 1
        operation_id = $OperationId
        process_id = $PID
        started_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $startedPath -Force
    Write-UpdateLog "STARTED_ACK pid=$PID"
}

function Confirm-CommitMarker {
    $runtimeDir = Join-Path $script:BaseFull 'runtime'
    $commitPath = Join-Path $runtimeDir "runtime-update-commit-$OperationId.json"
    $temporary = Join-Path $runtimeDir "runtime-update-commit-$OperationId.tmp"
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    $lastError = 'commit marker was not created'
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $commitPath -PathType Leaf) {
            try {
                if ((Get-Item -LiteralPath $commitPath).Length -gt 16384) {
                    throw 'commit marker is too large'
                }
                $commit = Get-Content -LiteralPath $commitPath -Raw -Encoding UTF8 |
                    ConvertFrom-Json
                if ([int]$commit.schema_version -ne 1 -or
                    [string]$commit.operation_id -ne $OperationId -or
                    [int]$commit.process_id -ne $PID) {
                    throw 'commit marker identity does not match this helper'
                }
                Remove-Item -LiteralPath $commitPath -Force
                Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
                Write-UpdateLog "COMMIT_CONFIRMED pid=$PID"
                return
            }
            catch {
                $lastError = $_.Exception.Message
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Runtime update commit was not confirmed: $lastError"
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
    Write-UpdateLog "RESULT code=$Code success=$Success rolled_back=$RolledBack message=$Message"
}

function Start-Client {
    if ($NoRestart) {
        Write-UpdateLog 'RESTART_SKIPPED no-restart requested'
        return $null
    }
    $python = Join-Path $script:BaseFull 'runtime\python\pythonw.exe'
    $entrypoint = Join-Path $script:BaseFull 'app\gui\main_gateway.py'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Client Python is missing after runtime update: $python"
    }
    if (-not (Test-Path -LiteralPath $entrypoint -PathType Leaf)) {
        throw "Client entrypoint is missing after runtime update: $entrypoint"
    }
    $runtimeDir = Join-Path $script:BaseFull 'runtime'
    $readyPath = Join-Path $runtimeDir "runtime-update-ready-$OperationId.json"
    $readyTemporary = Join-Path $runtimeDir "runtime-update-ready-$OperationId.tmp"
    Remove-Item -LiteralPath $readyPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $readyTemporary -Force -ErrorAction SilentlyContinue

    $arguments = "-s -B `"$entrypoint`""
    $environmentName = 'LINGJING_RUNTIME_UPDATE_OPERATION_ID'
    $previousOperation = [Environment]::GetEnvironmentVariable(
        $environmentName,
        [EnvironmentVariableTarget]::Process
    )
    try {
        [Environment]::SetEnvironmentVariable(
            $environmentName,
            $OperationId,
            [EnvironmentVariableTarget]::Process
        )
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList $arguments `
            -WorkingDirectory $script:BaseFull `
            -PassThru
    }
    finally {
        [Environment]::SetEnvironmentVariable(
            $environmentName,
            $previousOperation,
            [EnvironmentVariableTarget]::Process
        )
    }
    Write-UpdateLog "RESTART_STARTED executable=$python pid=$($process.Id)"
    return $process
}

function Confirm-ClientReady {
    param(
        [System.Diagnostics.Process]$ClientProcess,
        [ValidateRange(5, 120)][int]$TimeoutSeconds = 45
    )
    if ($NoRestart) { return }
    if ($null -eq $ClientProcess) {
        throw 'Client process was not returned by Start-Process'
    }

    $runtimeDir = Join-Path $script:BaseFull 'runtime'
    $readyPath = Join-Path $runtimeDir "runtime-update-ready-$OperationId.json"
    $readyTemporary = Join-Path $runtimeDir "runtime-update-ready-$OperationId.tmp"
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    try {
        while ([DateTime]::UtcNow -lt $deadline) {
            $ClientProcess.Refresh()
            if ($ClientProcess.HasExited) {
                throw "Client exited before readiness acknowledgement (pid $($ClientProcess.Id))"
            }
            if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
                try {
                    $ready = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 |
                        ConvertFrom-Json
                    $readyOperation = [string]$ready.operation_id
                    $readyPid = [int]$ready.pid
                    if ($readyOperation -eq $OperationId -and
                        $readyPid -eq $ClientProcess.Id) {
                        Write-UpdateLog "RESTART_READY pid=$readyPid"
                        return
                    }
                }
                catch {
                    # The atomic writer may still be replacing the ready file.
                }
            }
            Start-Sleep -Milliseconds 250
        }
        throw "Timed out waiting for client readiness acknowledgement (pid $($ClientProcess.Id))"
    }
    finally {
        Remove-Item -LiteralPath $readyPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $readyTemporary -Force -ErrorAction SilentlyContinue
    }
}

$StagingFull = $null
$BackupFull = $null
$baseValidated = $false
$parentMayStillBeRunning = $true
try {
    $script:BaseFull = Get-FullPath $BaseDir
    if (-not (Test-Path -LiteralPath $script:BaseFull -PathType Container)) {
        throw "Client directory does not exist: $script:BaseFull"
    }
    $root = [System.IO.Path]::GetPathRoot($script:BaseFull).TrimEnd('\')
    if ($script:BaseFull.TrimEnd('\').Equals(
        $root,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Refusing to update a drive root'
    }
    $baseValidated = $true
    Initialize-UpdateLog
    Write-UpdateLog "START parent_pid=$ParentPid base=$script:BaseFull staging=$StagingDir"
    try {
        $mutexAcquired = Enter-UpdateMutex -Base $script:BaseFull
    }
    catch {
        Write-UpdateLog "MUTEX_FAILED message=$($_.Exception.Message)"
        exit 4
    }
    if (-not $mutexAcquired) {
        Write-UpdateLog "MUTEX_BUSY name=$script:UpdateMutexName"
        exit 4
    }
    Write-UpdateLog "MUTEX_ACQUIRED name=$script:UpdateMutexName abandoned=$script:UpdateMutexAbandoned"
    Write-StartedMarker

    $parent = Get-ProcessIfRunningStrict -ProcessId $ParentPid
    if ($null -ne $parent) {
        Write-UpdateLog "WAIT_PARENT pid=$ParentPid timeout_seconds=$WaitTimeoutSeconds"
        Wait-Process `
            -InputObject $parent `
            -Timeout $WaitTimeoutSeconds `
            -ErrorAction SilentlyContinue
        # Do not read HasExited from the stale Process object.  Windows
        # PowerShell 5.1 can throw after its underlying handle has closed.
        $parentAfterWait = Get-ProcessIfRunningStrict -ProcessId $ParentPid
        if ($null -ne $parentAfterWait) {
            throw "Timed out waiting for client process $ParentPid to exit"
        }
    }
    $parentMayStillBeRunning = $false
    Write-UpdateLog "PARENT_EXITED pid=$ParentPid"
    Confirm-CommitMarker

    $StagingFull = Assert-DirectSafeChild `
        -Parent $script:BaseFull `
        -Child $StagingDir `
        -LeafPattern "^\.runtime-install-staging-$OperationId$"
    if (-not (Test-Path -LiteralPath $StagingFull -PathType Container)) {
        throw "Runtime staging directory does not exist: $StagingFull"
    }
    $BackupFull = Assert-DirectSafeChild `
        -Parent $script:BaseFull `
        -Child (Join-Path $script:BaseFull ".runtime-install-backup-$OperationId") `
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
    Write-UpdateLog "PREFLIGHT_SUCCEEDED staging=$StagingFull"
}
catch {
    $preflightMessage = $_.Exception.Message
    Write-UpdateLog "PREFLIGHT_FAILED message=$preflightMessage"
    try {
        if ($baseValidated -and $null -ne $script:BaseFull -and
            (Test-Path -LiteralPath $script:BaseFull -PathType Container)) {
            Write-UpdateResult `
                -Success $false `
                -RolledBack $false `
                -Code 'preflight_failed' `
                -Message $preflightMessage
        }
    }
    catch {
        Write-UpdateLog "RESULT_WRITE_FAILED message=$($_.Exception.Message)"
    }

    try {
        if (-not $baseValidated) {
            Exit-UpdateMutex
            exit 1
        }
        if ($parentMayStillBeRunning) {
            $runningParent = Get-ProcessIfRunningStrict -ProcessId $ParentPid
            if ($null -ne $runningParent) {
                Write-UpdateLog "RESTART_SKIPPED original_client_running pid=$ParentPid"
                Exit-UpdateMutex
                exit 1
            }
        }
        $clientProcess = Start-Client
        Confirm-ClientReady -ClientProcess $clientProcess
    }
    catch {
        $restartMessage = $_.Exception.Message
        Write-UpdateLog "RESTART_FAILED message=$restartMessage"
        try {
            Write-UpdateResult `
                -Success $false `
                -RolledBack $false `
                -Code 'preflight_failed_restart_failed' `
                -Message "$preflightMessage; restart failed: $restartMessage"
        }
        catch {
            Write-UpdateLog "RESULT_WRITE_FAILED message=$($_.Exception.Message)"
        }
        Exit-UpdateMutex
        exit 3
    }
    Exit-UpdateMutex
    exit 1
}

$movedExisting = [System.Collections.Generic.List[object]]::new()
$installed = [System.Collections.Generic.List[string]]::new()
$transactionSucceeded = $false
$rollbackSucceeded = $false
$failureMessage = ''

try {
    Write-UpdateLog "TRANSACTION_START backup=$BackupFull"
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
    Write-UpdateLog 'TRANSACTION_SUCCEEDED'
}
catch {
    $failureMessage = $_.Exception.Message
    Write-UpdateLog "TRANSACTION_FAILED message=$failureMessage"
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
        Write-UpdateLog 'ROLLBACK_SUCCEEDED'
    }
    catch {
        $failureMessage = "$failureMessage; rollback failed: $($_.Exception.Message)"
        $rollbackSucceeded = $false
        Write-UpdateLog "ROLLBACK_FAILED message=$failureMessage"
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
    try {
        $clientProcess = Start-Client
        Confirm-ClientReady -ClientProcess $clientProcess
    }
    catch {
        Write-UpdateLog "RESTART_FAILED message=$($_.Exception.Message)"
        Write-UpdateResult -Success $true -RolledBack $false -Code 'installed_restart_failed' -Message $_.Exception.Message
        Exit-UpdateMutex
        exit 2
    }
    Exit-UpdateMutex
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
try {
    $clientProcess = Start-Client
    Confirm-ClientReady -ClientProcess $clientProcess
}
catch {
    Write-UpdateLog "RESTART_FAILED message=$($_.Exception.Message)"
    Exit-UpdateMutex
    exit 3
}
Exit-UpdateMutex
exit 1

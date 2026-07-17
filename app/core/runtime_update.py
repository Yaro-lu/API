"""Launch a detached Windows helper for runtime replacement after GUI exit."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from app.core.runtime_package import missing_runtime_paths


RUNTIME_UPDATE_HELPER = Path("app/core/runtime_update_helper.ps1")
RUNTIME_UPDATE_RESULT = Path("runtime/runtime-update-result.json")
_STAGING_NAME = re.compile(r"^\.runtime-install-staging-([0-9a-f]{32})$")
_MAX_RESULT_BYTES = 64 * 1024


def _resolved_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label}不存在: {path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"{label}不是目录: {resolved}")
    return resolved


def validate_runtime_update_handoff(
    base_dir: Path,
    staging_dir: Path,
) -> tuple[Path, Path, Path, str]:
    """Validate the only paths that the detached updater may mutate."""
    base = _resolved_directory(base_dir, label="客户端目录")
    staging = _resolved_directory(staging_dir, label="环境暂存目录")
    if base == Path(base.anchor):
        raise ValueError("拒绝在磁盘根目录执行运行环境更新")
    if staging.parent != base:
        raise ValueError("环境暂存目录必须是客户端目录的直接子目录")
    match = _STAGING_NAME.fullmatch(staging.name)
    if not match:
        raise ValueError("环境暂存目录名称无效")

    helper = (base / RUNTIME_UPDATE_HELPER).resolve(strict=False)
    expected_helper = base / "app" / "core" / "runtime_update_helper.ps1"
    if helper != expected_helper or not helper.is_file():
        raise ValueError(f"运行环境更新助手缺失: {expected_helper}")

    missing = missing_runtime_paths(staging)
    if missing:
        raise ValueError(f"环境暂存目录不完整，缺少: {', '.join(missing)}")
    return base, staging, helper, match.group(1)


def find_windows_powershell(
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path:
    """Find the system Windows PowerShell executable used outside runtime/python."""
    environ = os.environ if environ is None else environ
    candidates: list[Path] = []
    windows_root = str(environ.get("SystemRoot") or environ.get("WINDIR") or "").strip()
    if windows_root:
        candidates.append(
            Path(windows_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
    resolved = which("powershell.exe")
    if resolved:
        candidates.append(Path(resolved))
    for candidate in candidates:
        try:
            full = candidate.resolve(strict=True)
        except OSError:
            continue
        if full.is_file() and full.name.casefold() == "powershell.exe":
            return full
    raise RuntimeError("未找到系统 Windows PowerShell，无法安全更新运行环境")


def build_runtime_update_command(
    base_dir: Path,
    staging_dir: Path,
    *,
    parent_pid: int,
    powershell_path: Path | None = None,
    wait_timeout_seconds: int = 180,
) -> list[str]:
    """Build a shell-free argument vector for the detached update helper."""
    base, staging, helper, operation_id = validate_runtime_update_handoff(
        base_dir,
        staging_dir,
    )
    if int(parent_pid) <= 0:
        raise ValueError("父进程 PID 无效")
    if not 30 <= int(wait_timeout_seconds) <= 600:
        raise ValueError("等待 GUI 退出的超时时间无效")
    powershell = Path(powershell_path or find_windows_powershell()).resolve(strict=True)
    if not powershell.is_file() or powershell.name.casefold() != "powershell.exe":
        raise ValueError("运行环境更新只能使用 Windows PowerShell")
    return [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper),
        "-ParentPid",
        str(int(parent_pid)),
        "-BaseDir",
        str(base),
        "-StagingDir",
        str(staging),
        "-OperationId",
        operation_id,
        "-WaitTimeoutSeconds",
        str(int(wait_timeout_seconds)),
    ]


def launch_runtime_update(
    base_dir: Path,
    staging_dir: Path,
    *,
    parent_pid: int,
    powershell_path: Path | None = None,
    popen_factory=None,
):
    """Start the updater outside the GUI Job/process tree and return its process."""
    if os.name != "nt":
        raise RuntimeError("运行环境自动更新仅支持 Windows")
    base, _, _, _ = validate_runtime_update_handoff(base_dir, staging_dir)
    command = build_runtime_update_command(
        base,
        staging_dir,
        parent_pid=parent_pid,
        powershell_path=powershell_path,
    )
    popen_factory = popen_factory or subprocess.Popen
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )
    return popen_factory(
        command,
        cwd=str(base),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
    )


def consume_runtime_update_result(base_dir: Path) -> dict[str, object] | None:
    """Read and remove the fixed updater result file shown after restart."""
    base = Path(base_dir).resolve(strict=False)
    result_path = base / RUNTIME_UPDATE_RESULT
    try:
        if not result_path.is_file() or result_path.stat().st_size > _MAX_RESULT_BYTES:
            return None
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass

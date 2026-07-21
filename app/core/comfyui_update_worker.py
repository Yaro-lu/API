"""Apply a prepared ComfyUI update as a crash-auditable filesystem transaction."""

from __future__ import annotations

import argparse
import csv
import datetime as _datetime
import errno
import json
import os
import re
import shutil
import stat as _stat
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence


COMFYUI_UPDATE_RESULT = Path("runtime/comfyui-update-result.json")
COMFYUI_VERSION_RECORD = Path(".lingjing-comfyui-version.json")

_STAGING_NAME = re.compile(r"^\.comfyui-update-staging-([0-9a-f]{32})$")
_OVERLAY_NAME = re.compile(r"^\.comfyui-update-overlay-([0-9a-f]{32})$")
_BACKUP_NAME = re.compile(r"^\.comfyui-update-backup-([0-9a-f]{32})$")
_JOURNAL_NAME = re.compile(r"^\.comfyui-update-journal-([0-9a-f]{32})\.json$")
_MANIFEST_NAME = re.compile(r"^\.comfyui-update-manifest-([0-9a-f]{32})\.json$")
_SAFE_DEPENDENCY_ENTRY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")
_MAX_JSON_BYTES = 256 * 1024
_MAX_RESULT_BYTES = 64 * 1024
_MAX_CLEANUP_WARNINGS = 32
_MAX_CLEANUP_WARNING_CHARS = 1_000
_MAX_CLEANUP_WARNINGS_TOTAL_CHARS = 16_000
_REPARSE_POINT = 0x400
_UPDATER_LOCK_NAME = ".comfyui-updater.lock"

PROTECTED_COMFYUI_PATHS = (
    Path("models"),
    Path("custom_nodes"),
    Path("user"),
    Path("input"),
    Path("inputs"),
    Path("output"),
    Path("outputs"),
    Path("temp"),
    Path("logs"),
    Path("tasks"),
    Path("cache"),
    Path("extra_model_paths.yaml"),
)

_ALLOWED_OVERLAY_MODULES = {
    "comfyui_frontend_package",
    "comfyui_embedded_docs",
    "comfy_kitchen",
    "comfy_aimdo",
}
_WORKFLOW_TEMPLATE_PREFIX = "comfyui_workflow_templates"


def _path_exists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _plain_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label}不存在: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"{label}不是目录: {resolved}")
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise ValueError(f"{label}不能是链接或重解析点: {candidate}")
    return resolved


class _UpdaterFileLock:
    """One OS-backed updater lock; closing/crashing releases the held range."""

    def __init__(self, base: Path):
        runtime = _plain_directory(base / "runtime", label="runtime 目录")
        if runtime.parent != base:
            raise ValueError("runtime 目录位置无效")
        self.path = runtime / _UPDATER_LOCK_NAME
        self._fd: int | None = None
        self._windows_overlapped = None

    def _open(self) -> int:
        if _path_exists(self.path) and (
            self.path.is_symlink()
            or _is_reparse_point(self.path)
            or not self.path.is_file()
        ):
            raise ValueError("ComfyUI 更新锁文件类型无效")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            opened = os.fstat(fd)
            current = self.path.stat()
            if not _stat.S_ISREG(opened.st_mode):
                raise ValueError("ComfyUI 更新锁不是普通文件")
            if int(getattr(opened, "st_nlink", 1) or 1) > 1:
                raise ValueError("ComfyUI 更新锁不能是硬链接")
            if self.path.is_symlink() or _is_reparse_point(self.path):
                raise ValueError("ComfyUI 更新锁不能是链接或重解析点")
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("ComfyUI 更新锁在打开时被替换")
        except Exception:
            os.close(fd)
            raise
        return fd

    def acquire(self, *, blocking: bool) -> bool:
        if self._fd is not None:
            raise RuntimeError("ComfyUI 更新锁已被当前对象持有")
        fd = self._open()
        try:
            acquired = self._lock_fd(fd, blocking=blocking)
        except Exception:
            os.close(fd)
            raise
        if not acquired:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def _lock_fd(self, fd: int, *, blocking: bool) -> bool:
        if os.name != "nt":
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, flags)
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
            return True

        import ctypes
        import msvcrt
        from ctypes import wintypes

        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else wintypes.DWORD

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ulong_ptr),
                ("InternalHigh", ulong_ptr),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        overlapped = Overlapped()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel32.LockFileEx.restype = wintypes.BOOL
        flags = 0x00000002
        if not blocking:
            flags |= 0x00000001
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if not blocking and error == 33:
                return False
            raise OSError(error, "无法获取 ComfyUI 更新锁")
        self._windows_overlapped = (kernel32, handle, overlapped)
        return True

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            if os.name != "nt":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            elif self._windows_overlapped is not None:
                import ctypes

                kernel32, handle, overlapped = self._windows_overlapped
                kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        finally:
            self._windows_overlapped = None
            self._fd = None
            os.close(fd)


def _assert_direct_child(
    base: Path,
    path: Path,
    *,
    pattern: re.Pattern[str],
    label: str,
) -> tuple[Path, str]:
    resolved = _plain_directory(path, label=label)
    if resolved.parent != base:
        raise ValueError(f"{label}必须是客户端目录的直接子目录")
    match = pattern.fullmatch(resolved.name)
    if not match:
        raise ValueError(f"{label}名称无效")
    return resolved, match.group(1)


def _assert_constructed_child(
    base: Path,
    path: Path,
    *,
    pattern: re.Pattern[str],
    operation_id: str,
    label: str,
) -> None:
    if path.parent != base:
        raise ValueError(f"{label}必须是客户端目录的直接子目录")
    match = pattern.fullmatch(path.name)
    if not match or match.group(1) != operation_id:
        raise ValueError(f"{label}名称无效")


def _assert_no_links_in_tree(
    root: Path,
    *,
    label: str,
    skip_top_level: set[str] | None = None,
) -> None:
    skip_top_level = {name.casefold() for name in (skip_top_level or set())}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(root)
        selected_files = files
        if relative == Path("."):
            retained: list[str] = []
            for name in directories:
                if name.casefold() in skip_top_level:
                    continue
                retained.append(name)
            directories[:] = retained
            selected_files = [
                name for name in files if name.casefold() not in skip_top_level
            ]
        for name in [*directories, *selected_files]:
            child = current_path / name
            if child.is_symlink() or _is_reparse_point(child):
                display = child.relative_to(root).as_posix()
                raise ValueError(f"{label}包含链接或重解析点: {display}")
            try:
                stat = child.lstat()
            except OSError as exc:
                raise ValueError(f"无法检查{label}: {child}") from exc
            if child.is_file() and int(getattr(stat, "st_nlink", 1) or 1) > 1:
                display = child.relative_to(root).as_posix()
                raise ValueError(f"{label}包含硬链接: {display}")


def _validate_release_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("发布元数据必须是对象")
    metadata = dict(value)
    try:
        encoded = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("发布元数据无法序列化") from exc
    if len(encoded) > _MAX_JSON_BYTES:
        raise ValueError("发布元数据过大")
    return metadata


def _validate_dependency_overlay(
    base: Path,
    overlay: Path | None,
    *,
    operation_id: str,
) -> tuple[Path | None, list[Path]]:
    if overlay is None or not str(overlay).strip():
        return None, []
    resolved, overlay_id = _assert_direct_child(
        base,
        Path(overlay),
        pattern=_OVERLAY_NAME,
        label="依赖暂存目录",
    )
    if overlay_id != operation_id:
        raise ValueError("依赖暂存目录与核心暂存目录事务 ID 不匹配")

    entries: list[Path] = []
    seen: set[str] = set()
    distributions: set[str] = set()
    for entry in sorted(resolved.iterdir(), key=lambda item: item.name.casefold()):
        if not _SAFE_DEPENDENCY_ENTRY.fullmatch(entry.name):
            raise ValueError(f"依赖暂存目录包含不安全的顶层条目: {entry.name}")
        folded = entry.name.casefold()
        if not _allowed_overlay_entry(entry):
            raise ValueError(f"依赖暂存目录包含未授权的顶层条目: {entry.name}")
        if folded in seen:
            raise ValueError(f"依赖暂存目录包含大小写重复条目: {entry.name}")
        seen.add(folded)
        distribution = _overlay_distribution_name(folded)
        if distribution is not None:
            if distribution in distributions:
                raise ValueError(f"依赖暂存目录包含重复分发元数据: {distribution}")
            distributions.add(distribution)
        if entry.parent != resolved or entry.is_symlink() or _is_reparse_point(entry):
            raise ValueError(f"依赖暂存目录包含链接或越界条目: {entry.name}")
        if not entry.is_file() and not entry.is_dir():
            raise ValueError(f"依赖暂存目录包含不支持的条目: {entry.name}")
        if entry.is_dir():
            _assert_no_links_in_tree(entry, label=f"依赖条目 {entry.name}")
        elif int(getattr(entry.lstat(), "st_nlink", 1) or 1) > 1:
            raise ValueError(f"依赖暂存目录包含硬链接: {entry.name}")
        entries.append(entry)
    return resolved, entries


def _allowed_overlay_entry(entry: Path) -> bool:
    """Accept only audited ComfyUI distributions and their import roots."""
    folded_name = entry.name.casefold()
    if folded_name == "scripts" or folded_name.endswith(".pth"):
        return False
    if entry.is_file():
        module_name = folded_name.removesuffix(".py")
        if not folded_name.endswith(".py"):
            return False
        return module_name in _ALLOWED_OVERLAY_MODULES or bool(
            re.fullmatch(r"comfyui_workflow_templates(?:_[a-z0-9_]+)*", module_name)
        )
    if not entry.is_dir():
        return False
    if folded_name in _ALLOWED_OVERLAY_MODULES:
        return True
    if re.fullmatch(
        r"comfyui_workflow_templates(?:_[a-z0-9_]+)*",
        folded_name,
    ):
        return True
    return _overlay_distribution_name(folded_name) is not None


def _overlay_distribution_name(entry_name: str) -> str | None:
    """Return the audited normalized distribution represented by a dist-info name."""
    folded = str(entry_name or "").casefold()
    match = re.fullmatch(
        r"(?P<distribution>[a-z0-9_]+)-"
        r"(?P<version>[0-9a-z][0-9a-z.+_-]*)\.dist-info",
        folded,
    )
    if not match:
        return None
    distribution = match.group("distribution")
    if distribution in _ALLOWED_OVERLAY_MODULES:
        return distribution
    if re.fullmatch(
        r"comfyui_workflow_templates(?:_[a-z0-9_]+)*",
        distribution,
    ):
        return distribution
    return None


def _workflow_import_root_name(entry_name: str) -> bool:
    folded = str(entry_name or "").casefold()
    if folded.endswith(".py"):
        folded = folded[:-3]
    return bool(
        re.fullmatch(r"comfyui_workflow_templates(?:_[a-z0-9_]+)*", folded)
    )


def _controlled_import_root(entry_name: str, distribution: str) -> bool:
    folded = str(entry_name or "").casefold()
    module = folded[:-3] if folded.endswith(".py") else folded
    if distribution in _ALLOWED_OVERLAY_MODULES:
        return module == distribution
    if distribution.startswith(_WORKFLOW_TEMPLATE_PREFIX):
        return _workflow_import_root_name(folded)
    return False


def _recorded_import_roots(dist_info: Path, distribution: str) -> set[str]:
    record = dist_info / "RECORD"
    if not record.is_file() or record.is_symlink() or _is_reparse_point(record):
        return set()
    if record.stat().st_size > 4 * 1024 * 1024:
        raise ValueError(f"旧版分发 RECORD 过大: {dist_info.name}")
    roots: set[str] = set()
    try:
        with record.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if not row or not row[0] or "\\" in row[0] or ":" in row[0]:
                    continue
                relative = PurePosixPath(row[0])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    continue
                root_name = relative.parts[0]
                if _controlled_import_root(root_name, distribution):
                    roots.add(root_name)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"无法读取旧版分发 RECORD: {dist_info.name}") from exc
    return roots


def _validate_dependency_entry(path: Path, *, label: str) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise ValueError(f"{label}是链接或重解析点: {path.name}")
    if not path.is_file() and not path.is_dir():
        raise ValueError(f"{label}类型无效: {path.name}")
    if path.is_dir():
        _assert_no_links_in_tree(path, label=f"{label} {path.name}")
    elif int(getattr(path.lstat(), "st_nlink", 1) or 1) > 1:
        raise ValueError(f"{label}是硬链接: {path.name}")


def _existing_distribution_entries(
    site_packages: Path,
    overlay_entries: list[Path],
) -> list[Path]:
    overlay_distributions = {
        distribution
        for entry in overlay_entries
        if (distribution := _overlay_distribution_name(entry.name)) is not None
    }
    if not overlay_distributions:
        return []
    update_workflow_family = any(
        distribution == _WORKFLOW_TEMPLATE_PREFIX
        or distribution.startswith(f"{_WORKFLOW_TEMPLATE_PREFIX}_")
        for distribution in overlay_distributions
    )
    selected: dict[str, Path] = {}
    recorded_roots: set[tuple[str, str]] = set()
    for installed in site_packages.iterdir():
        distribution = _overlay_distribution_name(installed.name)
        affected = distribution in overlay_distributions or bool(
            update_workflow_family
            and distribution
            and distribution.startswith(_WORKFLOW_TEMPLATE_PREFIX)
        )
        if not affected or distribution is None:
            continue
        _validate_dependency_entry(installed, label="旧版分发元数据")
        selected[installed.name.casefold()] = installed
        for root_name in _recorded_import_roots(installed, distribution):
            recorded_roots.add((root_name, distribution))

    for distribution in overlay_distributions:
        root_names = {distribution, f"{distribution}.py"}
        for root_name in root_names:
            if _controlled_import_root(root_name, distribution):
                recorded_roots.add((root_name, distribution))
    if update_workflow_family:
        for installed in site_packages.iterdir():
            if (
                _overlay_distribution_name(installed.name) is None
                and _workflow_import_root_name(installed.name)
            ):
                recorded_roots.add((installed.name, _WORKFLOW_TEMPLATE_PREFIX))

    for root_name, distribution in recorded_roots:
        if not _controlled_import_root(root_name, distribution):
            continue
        installed = site_packages / root_name
        if not _path_exists(installed):
            continue
        _validate_dependency_entry(installed, label="旧版受控导入根")
        selected[installed.name.casefold()] = installed
    return sorted(selected.values(), key=lambda path: path.name.casefold())


def _validate_transaction_paths(
    base_dir: Path,
    staging_core: Path,
    dependency_overlay: Path | None,
    release_metadata: object,
) -> dict[str, object]:
    base = _plain_directory(base_dir, label="客户端目录")
    if base == Path(base.anchor):
        raise ValueError("拒绝在磁盘根目录执行 ComfyUI 更新")

    staging, operation_id = _assert_direct_child(
        base,
        staging_core,
        pattern=_STAGING_NAME,
        label="ComfyUI 核心暂存目录",
    )
    main_file = staging / "main.py"
    if (
        not main_file.is_file()
        or main_file.is_symlink()
        or _is_reparse_point(main_file)
        or main_file.stat().st_size <= 0
    ):
        raise ValueError("ComfyUI 核心暂存目录缺少有效的 main.py")
    _assert_no_links_in_tree(staging, label="ComfyUI 核心暂存目录")

    runtime = _plain_directory(base / "runtime", label="runtime 目录")
    if runtime.parent != base:
        raise ValueError("runtime 目录位置无效")
    live = _plain_directory(runtime / "ComfyUI", label="本地 ComfyUI")
    if live.parent != runtime:
        raise ValueError("本地 ComfyUI 目录位置无效")
    if _path_exists(live / ".git"):
        raise ValueError("检测到带 .git 的用户 ComfyUI，拒绝自动覆盖")
    _assert_no_links_in_tree(
        live,
        label="本地 ComfyUI",
        skip_top_level={path.name for path in PROTECTED_COMFYUI_PATHS},
    )

    backup = base / f".comfyui-update-backup-{operation_id}"
    journal = base / f".comfyui-update-journal-{operation_id}.json"
    _assert_constructed_child(
        base,
        backup,
        pattern=_BACKUP_NAME,
        operation_id=operation_id,
        label="ComfyUI 备份目录",
    )
    _assert_constructed_child(
        base,
        journal,
        pattern=_JOURNAL_NAME,
        operation_id=operation_id,
        label="ComfyUI 更新日志",
    )
    if _path_exists(backup):
        raise ValueError("同一事务的 ComfyUI 备份目录已存在，拒绝覆盖")
    if _path_exists(journal):
        raise ValueError("同一事务的 ComfyUI 更新日志已存在，拒绝覆盖")

    overlay, overlay_entries = _validate_dependency_overlay(
        base,
        dependency_overlay,
        operation_id=operation_id,
    )
    site_packages: Path | None = None
    existing_distribution_entries: list[Path] = []
    if overlay is not None:
        venv = _plain_directory(base / ".venv", label="便携依赖目录")
        lib = _plain_directory(venv / "Lib", label="便携依赖 Lib 目录")
        site_packages = _plain_directory(
            lib / "site-packages",
            label="便携依赖 site-packages 目录",
        )
        if venv.parent != base or lib.parent != venv or site_packages.parent != lib:
            raise ValueError("便携依赖目录位置无效")
        for entry in overlay_entries:
            destination = site_packages / entry.name
            if _path_exists(destination):
                if destination.is_symlink() or _is_reparse_point(destination):
                    raise ValueError(f"已安装依赖是链接或重解析点: {entry.name}")
                if destination.is_dir():
                    _assert_no_links_in_tree(
                        destination,
                        label=f"已安装依赖 {entry.name}",
                    )
                elif int(getattr(destination.lstat(), "st_nlink", 1) or 1) > 1:
                    raise ValueError(f"已安装依赖是硬链接: {entry.name}")
        existing_distribution_entries = _existing_distribution_entries(
            site_packages,
            overlay_entries,
        )

    return {
        "base": base,
        "live": live,
        "staging": staging,
        "overlay": overlay,
        "overlay_entries": overlay_entries,
        "site_packages": site_packages,
        "existing_distribution_entries": existing_distribution_entries,
        "backup": backup,
        "journal": journal,
        "operation_id": operation_id,
        "release_metadata": _validate_release_metadata(release_metadata),
    }


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _Journal:
    def __init__(self, base: Path, path: Path, operation_id: str):
        self.base = base
        self.path = path
        self.payload: dict[str, object] = {
            "schema_version": 1,
            "operation_id": operation_id,
            "phase": "starting",
            "actions": [],
        }
        self.write()

    @property
    def actions(self) -> list[dict[str, object]]:
        return self.payload["actions"]  # type: ignore[return-value]

    def write(self) -> None:
        _atomic_write_json(self.path, self.payload)

    def phase(self, value: str) -> None:
        self.payload["phase"] = value
        self.write()

    def plan(self, kind: str, source: Path | None, destination: Path) -> int:
        action: dict[str, object] = {
            "kind": kind,
            "state": "planned",
            "destination": destination.relative_to(self.base).as_posix(),
        }
        if source is not None:
            action["source"] = source.relative_to(self.base).as_posix()
        self.actions.append(action)
        self.write()
        return len(self.actions) - 1

    def complete(self, index: int) -> None:
        self.actions[index]["state"] = "done"
        self.write()


def _rename_path(source: Path, destination: Path) -> None:
    if not _path_exists(source):
        raise FileNotFoundError(f"更新源路径不存在: {source}")
    if _path_exists(destination):
        raise FileExistsError(f"更新目标路径已存在: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"更新目标父目录不存在: {destination.parent}")
    os.rename(source, destination)


def _journaled_move(
    journal: _Journal,
    source: Path,
    destination: Path,
    *,
    kind: str,
) -> None:
    index = journal.plan(kind, source, destination)
    _rename_path(source, destination)
    journal.complete(index)


def _journaled_mkdir(journal: _Journal, destination: Path, *, kind: str) -> None:
    if _path_exists(destination):
        raise FileExistsError(f"事务目录已存在: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"事务目录父路径不存在: {destination.parent}")
    index = journal.plan(kind, None, destination)
    destination.mkdir()
    journal.complete(index)


def _safe_rmtree(base: Path, target: Path, *, expected_name: re.Pattern[str]) -> None:
    if target.parent != base or not expected_name.fullmatch(target.name):
        raise ValueError(f"拒绝清理未验证的事务目录: {target}")
    if target.is_symlink() or _is_reparse_point(target):
        raise ValueError(f"拒绝清理链接或重解析点: {target}")
    if target.is_dir():
        shutil.rmtree(target)


def _reverse_action(base: Path, action: Mapping[str, object]) -> None:
    destination = base / str(action["destination"])
    kind = str(action.get("kind") or "")
    if kind.startswith("mkdir_"):
        if not _path_exists(destination):
            return
        if destination.is_symlink() or _is_reparse_point(destination):
            raise RuntimeError(f"回滚目录变成了链接: {destination}")
        destination.rmdir()
        return

    source = base / str(action["source"])
    source_exists = _path_exists(source)
    destination_exists = _path_exists(destination)
    if source_exists and not destination_exists:
        return
    if destination_exists and not source_exists:
        _rename_path(destination, source)
        return
    if source_exists and destination_exists:
        raise RuntimeError(f"回滚路径同时存在，拒绝覆盖: {source} / {destination}")
    raise RuntimeError(f"回滚路径同时缺失: {source} / {destination}")


def _rollback(journal: _Journal) -> list[str]:
    errors: list[str] = []
    try:
        journal.phase("rolling_back")
    except Exception as exc:
        errors.append(f"无法记录回滚阶段: {exc}")
    for action in reversed(journal.actions):
        try:
            _reverse_action(journal.base, action)
            action["rollback_state"] = "done"
        except Exception as exc:
            action["rollback_state"] = "failed"
            action["rollback_error"] = str(exc)
            errors.append(str(exc))
        try:
            journal.write()
        except Exception as exc:
            errors.append(f"无法记录回滚动作: {exc}")
    try:
        journal.phase("rollback_incomplete" if errors else "rolled_back")
    except Exception as exc:
        errors.append(f"无法记录回滚结果: {exc}")
    return errors


def _default_probe_runner(live_core: Path) -> bool:
    base = live_core.parents[1]
    python = base / "runtime" / "python" / "python.exe"
    if not python.is_file() or python.is_symlink() or _is_reparse_point(python):
        raise RuntimeError("缺少安全的便携 Python，无法验证更新后的 ComfyUI")
    command = [
        str(python),
        "-s",
        "-B",
        str(live_core / "main.py"),
        "--quick-test-for-ci",
        "--disable-all-custom-nodes",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        }
    )
    completed = subprocess.run(
        command,
        cwd=str(live_core),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"ComfyUI 快速验证失败（{completed.returncode}）: {detail}")
    return True


def _run_probe(probe_runner: Callable[[Path], object], live: Path) -> None:
    result = probe_runner(live)
    if isinstance(result, bool):
        if not result:
            raise RuntimeError("ComfyUI 快速验证未通过")
        return
    if isinstance(result, Mapping):
        if not bool(result.get("ok", result.get("success", False))):
            raise RuntimeError(str(result.get("error") or "ComfyUI 快速验证未通过"))
        return
    return_code = getattr(result, "returncode", None)
    if return_code is not None and int(return_code) != 0:
        raise RuntimeError(f"ComfyUI 快速验证失败（{return_code}）")


def _version_record(metadata: Mapping[str, object]) -> dict[str, object]:
    record = dict(metadata)
    record["schema_version"] = 1
    record["installed_at"] = _datetime.datetime.now(
        _datetime.timezone.utc
    ).isoformat()
    return record


def apply_prepared_update(
    base_dir: Path,
    staging_core: Path,
    dependency_overlay: Path | None,
    release_metadata: dict,
    probe_runner: Callable[[Path], object] = _default_probe_runner,
) -> dict[str, object]:
    """Swap a prepared Core/overlay into place and roll back every completed move."""
    paths = _validate_transaction_paths(
        base_dir,
        staging_core,
        dependency_overlay,
        release_metadata,
    )
    base = paths["base"]
    live = paths["live"]
    staging = paths["staging"]
    overlay = paths["overlay"]
    overlay_entries = paths["overlay_entries"]
    existing_distribution_entries = paths["existing_distribution_entries"]
    site_packages = paths["site_packages"]
    backup = paths["backup"]
    journal_path = paths["journal"]
    operation_id = str(paths["operation_id"])
    metadata = paths["release_metadata"]
    assert isinstance(base, Path)
    assert isinstance(live, Path)
    assert isinstance(staging, Path)
    assert isinstance(backup, Path)
    assert isinstance(journal_path, Path)
    assert isinstance(metadata, dict)

    journal = _Journal(base, journal_path, operation_id)
    cleanup_warnings: list[str] = []
    try:
        journal.phase("backing_up_core")
        _journaled_move(journal, live, backup, kind="move_live_to_backup")

        incoming_protected = backup / f".lingjing-incoming-protected-{operation_id}"
        staged_protected = [
            relative for relative in PROTECTED_COMFYUI_PATHS if _path_exists(staging / relative)
        ]
        if staged_protected:
            journal.phase("quarantining_staged_user_paths")
            _journaled_mkdir(
                journal,
                incoming_protected,
                kind="mkdir_incoming_protected",
            )
            for relative in staged_protected:
                _journaled_move(
                    journal,
                    staging / relative,
                    incoming_protected / relative,
                    kind="move_staged_protected_to_quarantine",
                )

        journal.phase("activating_core")
        _journaled_move(journal, staging, live, kind="move_staging_to_live")

        journal.phase("restoring_user_paths")
        for relative in PROTECTED_COMFYUI_PATHS:
            source = backup / relative
            if not _path_exists(source):
                continue
            destination = live / relative
            if _path_exists(destination):
                raise RuntimeError(f"新核心仍包含受保护路径: {relative.as_posix()}")
            _journaled_move(
                journal,
                source,
                destination,
                kind="move_protected_to_live",
            )

        if overlay is not None:
            assert isinstance(overlay, Path)
            assert isinstance(site_packages, Path)
            dependency_backup = backup / f".lingjing-dependencies-{operation_id}"
            journal.phase("installing_dependencies")
            _journaled_mkdir(
                journal,
                dependency_backup,
                kind="mkdir_dependency_backup",
            )
            for installed in existing_distribution_entries:
                _journaled_move(
                    journal,
                    installed,
                    dependency_backup / installed.name,
                    kind="move_dependency_to_backup",
                )
            for overlay_entry in overlay_entries:
                destination = site_packages / overlay_entry.name
                saved = dependency_backup / overlay_entry.name
                if _path_exists(destination):
                    _journaled_move(
                        journal,
                        destination,
                        saved,
                        kind="move_dependency_to_backup",
                    )
                _journaled_move(
                    journal,
                    overlay_entry,
                    destination,
                    kind="move_overlay_to_site_packages",
                )

        journal.phase("probing")
        _run_probe(probe_runner, live)

        journal.phase("writing_version")
        _atomic_write_json(live / COMFYUI_VERSION_RECORD, _version_record(metadata))
        journal.phase("committed")
    except Exception as exc:
        rollback_errors = _rollback(journal)
        status = "rollback_incomplete" if rollback_errors else "failed_rolled_back"
        if not rollback_errors:
            try:
                journal_path.unlink(missing_ok=True)
            except OSError:
                pass
        result: dict[str, object] = {
            "schema_version": 1,
            "status": status,
            "success": False,
            "rolled_back": not rollback_errors,
            "operation_id": operation_id,
            "message": (
                "ComfyUI 更新失败，已恢复原版本"
                if not rollback_errors
                else "ComfyUI 更新失败，回滚未完整完成"
            ),
            "error": str(exc),
        }
        if rollback_errors:
            result["rollback_errors"] = rollback_errors
            result["journal_path"] = str(journal_path)
        return result

    try:
        _safe_rmtree(base, backup, expected_name=_BACKUP_NAME)
    except Exception as exc:
        cleanup_warnings.append(f"旧核心备份暂未清理: {exc}")
    if isinstance(overlay, Path):
        try:
            overlay.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_warnings.append(f"依赖暂存目录暂未清理: {exc}")
    try:
        journal_path.unlink(missing_ok=True)
    except OSError as exc:
        cleanup_warnings.append(f"事务日志暂未清理: {exc}")

    result = {
        "schema_version": 1,
        "status": "installed",
        "success": True,
        "rolled_back": False,
        "operation_id": operation_id,
        "message": "ComfyUI 已更新并通过快速验证",
        "release_metadata": metadata,
    }
    if cleanup_warnings:
        result["cleanup_warnings"] = cleanup_warnings
    return result


def _load_prepared_manifest(path: Path) -> dict[str, object]:
    manifest = Path(path)
    if manifest.is_symlink() or _is_reparse_point(manifest):
        raise ValueError("更新准备清单不能是链接或重解析点")
    try:
        if not manifest.is_file() or manifest.stat().st_size > _MAX_JSON_BYTES:
            raise ValueError("更新准备清单不存在或过大")
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取更新准备清单: {manifest}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("更新准备清单版本无效")
    status = str(payload.get("status") or "ready")
    if status != "ready":
        raise ValueError(f"更新准备清单尚未就绪: {status}")
    for required in ("base_dir", "staging_core", "release_metadata"):
        if required not in payload:
            raise ValueError(f"更新准备清单缺少字段: {required}")
    return payload


def _validated_preparation_warnings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("更新准备清单的 cleanup_warnings 必须是列表")
    if len(value) > _MAX_CLEANUP_WARNINGS:
        raise ValueError("更新准备清单的 cleanup_warnings 条目过多")
    warnings: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for item in value:
        if not isinstance(item, str):
            raise ValueError("更新准备清单的 cleanup_warnings 条目必须是字符串")
        warning = item.strip()
        if not warning:
            raise ValueError("更新准备清单的 cleanup_warnings 包含空条目")
        if "\x00" in warning:
            raise ValueError("更新准备清单的 cleanup_warnings 包含 NUL 字符")
        if len(warning) > _MAX_CLEANUP_WARNING_CHARS:
            raise ValueError("更新准备清单的 cleanup_warnings 条目过长")
        total_chars += len(warning)
        if total_chars > _MAX_CLEANUP_WARNINGS_TOTAL_CHARS:
            raise ValueError("更新准备清单的 cleanup_warnings 总长度过长")
        if warning not in seen:
            seen.add(warning)
            warnings.append(warning)
    return warnings


def _merge_cleanup_warnings(
    result: dict[str, object], warnings: Sequence[str]
) -> None:
    if not warnings:
        return
    existing = result.get("cleanup_warnings")
    merged = list(existing) if isinstance(existing, list) else []
    seen = {warning for warning in merged if isinstance(warning, str)}
    for warning in warnings:
        if warning not in seen:
            merged.append(warning)
            seen.add(warning)
    if merged:
        result["cleanup_warnings"] = merged


def _manifest_paths(
    payload: Mapping[str, object],
) -> tuple[Path, Path, Path | None, dict, list[str]]:
    overlay_value = str(payload.get("dependency_overlay") or "").strip()
    metadata = payload.get("release_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("更新准备清单的 release_metadata 无效")
    preparation_warnings = _validated_preparation_warnings(
        payload.get("cleanup_warnings", [])
    )
    return (
        Path(str(payload["base_dir"])),
        Path(str(payload["staging_core"])),
        Path(overlay_value) if overlay_value else None,
        metadata,
        preparation_warnings,
    )


def _fixed_result_path(base: Path, result_path: Path | None) -> Path:
    expected = base / COMFYUI_UPDATE_RESULT
    candidate = Path(result_path) if result_path is not None else expected
    resolved = candidate.resolve(strict=False)
    if resolved != expected:
        raise ValueError(f"更新结果只能写入固定路径: {expected}")
    runtime = _plain_directory(base / "runtime", label="runtime 目录")
    if runtime != expected.parent:
        raise ValueError("更新结果目录位置无效")
    if _path_exists(expected) and (expected.is_symlink() or _is_reparse_point(expected)):
        raise ValueError("更新结果文件不能是链接或重解析点")
    return expected


def _validated_manifest_path(base: Path, manifest: Path, operation_id: str) -> Path:
    candidate = Path(manifest)
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise ValueError("更新准备清单不能是链接或重解析点")
    resolved = candidate.resolve(strict=True)
    match = _MANIFEST_NAME.fullmatch(resolved.name)
    if resolved.parent != base or not match or match.group(1) != operation_id:
        raise ValueError("更新准备清单必须是客户端目录内与事务匹配的固定名称")
    return resolved


def _validate_restart_client(base: Path, restart_client: Path | None) -> Path | None:
    if restart_client is None or not str(restart_client).strip():
        return None
    candidate = Path(restart_client)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"客户端重启程序不存在: {candidate}") from exc
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("客户端重启程序必须位于客户端目录内") from exc
    if (
        not resolved.is_file()
        or resolved.suffix.casefold() != ".exe"
        or candidate.is_symlink()
        or _is_reparse_point(candidate)
    ):
        raise ValueError("客户端重启程序无效")
    return resolved


def _validated_parent_wait(parent_pid: int, wait_timeout_seconds: int) -> tuple[int, int]:
    pid = int(parent_pid)
    timeout = int(wait_timeout_seconds)
    if pid <= 0:
        raise ValueError("父进程 PID 无效")
    if not 1 <= timeout <= 600:
        raise ValueError("等待客户端退出的超时时间无效")
    return pid, timeout


def _wait_for_parent_exit(parent_pid: int, timeout_seconds: int) -> bool:
    """Wait for one Windows process handle without shelling out or polling WMI."""
    if os.name != "nt":
        raise RuntimeError("等待客户端退出仅支持 Windows")
    pid, timeout = _validated_parent_wait(parent_pid, timeout_seconds)
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return True
        raise OSError(error, "无法等待客户端进程退出")
    try:
        outcome = int(kernel32.WaitForSingleObject(handle, timeout * 1000))
    finally:
        kernel32.CloseHandle(handle)
    if outcome == wait_object_0:
        return True
    if outcome == wait_timeout:
        return False
    if outcome == wait_failed:
        error = ctypes.get_last_error()
        raise OSError(error, "等待客户端进程退出失败")
    raise RuntimeError(f"等待客户端进程退出返回未知状态: {outcome}")


def build_worker_command(
    manifest_path: Path,
    result_path: Path | None = None,
    restart_client: Path | None = None,
    *,
    parent_pid: int,
    wait_timeout_seconds: int = 60,
) -> list[str]:
    """Build a shell-free command for the bundled detached Python worker."""
    manifest_input = Path(manifest_path)
    payload = _load_prepared_manifest(manifest_input)
    manifest = manifest_input.resolve(strict=True)
    base_dir, staging_core, dependency_overlay, metadata, _warnings = _manifest_paths(
        payload
    )
    paths = _validate_transaction_paths(
        base_dir,
        staging_core,
        dependency_overlay,
        metadata,
    )
    base = paths["base"]
    assert isinstance(base, Path)
    operation_id = str(paths["operation_id"])
    manifest = _validated_manifest_path(base, manifest, operation_id)
    result = _fixed_result_path(base, result_path)
    restart = _validate_restart_client(base, restart_client)
    parent, wait_timeout = _validated_parent_wait(parent_pid, wait_timeout_seconds)
    python = base / "runtime" / "python" / "pythonw.exe"
    if not python.is_file():
        python = base / "runtime" / "python" / "python.exe"
    if not python.is_file() or python.is_symlink() or _is_reparse_point(python):
        raise ValueError("缺少安全的便携 Python，无法启动 ComfyUI 更新 worker")
    module = base / "app" / "core" / "comfyui_update_worker.py"
    if not module.is_file() or module.is_symlink() or _is_reparse_point(module):
        raise ValueError("安装包缺少 ComfyUI 更新 worker")
    command = [
        str(python.resolve(strict=True)),
        "-s",
        "-B",
        str(module.resolve(strict=True)),
        "--manifest",
        str(manifest),
        "--result",
        str(result),
        "--parent-pid",
        str(parent),
        "--wait-timeout-seconds",
        str(wait_timeout),
    ]
    if restart is not None:
        command.extend(["--restart-client", str(restart)])
    return command


def launch_worker(
    manifest_path: Path,
    result_path: Path | None = None,
    restart_client: Path | None = None,
    *,
    parent_pid: int,
    wait_timeout_seconds: int = 60,
    popen_factory=None,
):
    """Launch the worker outside the GUI process group on Windows."""
    if os.name != "nt":
        raise RuntimeError("ComfyUI 自动更新 worker 仅支持 Windows")
    payload = _load_prepared_manifest(Path(manifest_path))
    base_dir, _, _, _, _ = _manifest_paths(payload)
    base = _plain_directory(base_dir, label="客户端目录")
    command = build_worker_command(
        manifest_path,
        result_path,
        restart_client,
        parent_pid=parent_pid,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
        }
    )
    popen_factory = popen_factory or subprocess.Popen
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )
    return popen_factory(
        command,
        cwd=str(base),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _restart_command(base: Path, executable: Path) -> list[str]:
    if executable.name.casefold() in {"python.exe", "pythonw.exe"}:
        gateway = base / "app" / "gui" / "main_gateway.py"
        if not gateway.is_file() or gateway.is_symlink() or _is_reparse_point(gateway):
            raise ValueError("客户端主程序缺失，无法自动重启")
        return [str(executable), "-s", "-B", str(gateway)]
    return [str(executable)]


def _launch_restart(base: Path, executable: Path, *, popen_factory=None):
    command = _restart_command(base, executable)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(base)
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )
    popen_factory = popen_factory or subprocess.Popen
    return popen_factory(
        command,
        cwd=str(base),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _cleanup_unapplied_update(
    base: Path,
    staging_core: Path,
    dependency_overlay: Path | None,
    operation_id: str,
) -> list[str]:
    """Remove only validated, unactivated inputs for one timed-out transaction."""
    targets: list[tuple[Path, re.Pattern[str], str]] = [
        (staging_core, _STAGING_NAME, "ComfyUI 核心暂存目录")
    ]
    if dependency_overlay is not None:
        targets.append((dependency_overlay, _OVERLAY_NAME, "依赖暂存目录"))
    warnings: list[str] = []
    for candidate, pattern, label in targets:
        if not _path_exists(candidate):
            continue
        try:
            resolved, candidate_id = _assert_direct_child(
                base,
                candidate,
                pattern=pattern,
                label=label,
            )
            if candidate_id != operation_id:
                raise ValueError(f"{label}与更新事务 ID 不匹配")
            _assert_no_links_in_tree(resolved, label=label)
            _safe_rmtree(base, resolved, expected_name=pattern)
        except Exception as exc:
            warnings.append(f"{label}未清理: {exc}")
    return warnings


def _journal_relative_path(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"事务日志 {label} 路径无效")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or value != relative.as_posix()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"事务日志 {label} 路径越界")
    return relative


def _journal_dependency_name(name: str) -> bool:
    folded = str(name or "").casefold()
    if not _SAFE_DEPENDENCY_ENTRY.fullmatch(name) or folded.endswith(".pth"):
        return False
    if folded == "scripts":
        return False
    module = folded[:-3] if folded.endswith(".py") else folded
    return (
        module in _ALLOWED_OVERLAY_MODULES
        or _workflow_import_root_name(folded)
        or _overlay_distribution_name(folded) is not None
    )


def _validated_recovery_action(
    action: object,
    *,
    operation_id: str,
) -> dict[str, object]:
    if not isinstance(action, dict):
        raise ValueError("事务日志动作必须是对象")
    kind = str(action.get("kind") or "")
    state = str(action.get("state") or "")
    if state not in {"planned", "done"}:
        raise ValueError(f"事务日志动作状态无效: {state}")
    destination = _journal_relative_path(
        action.get("destination"),
        label="destination",
    )
    source_value = action.get("source")
    source = (
        _journal_relative_path(source_value, label="source")
        if source_value is not None
        else None
    )
    staging = PurePosixPath(f".comfyui-update-staging-{operation_id}")
    overlay = PurePosixPath(f".comfyui-update-overlay-{operation_id}")
    backup = PurePosixPath(f".comfyui-update-backup-{operation_id}")
    live = PurePosixPath("runtime/ComfyUI")
    site_packages = PurePosixPath(".venv/Lib/site-packages")
    incoming = backup / f".lingjing-incoming-protected-{operation_id}"
    dependency_backup = backup / f".lingjing-dependencies-{operation_id}"
    protected = {PurePosixPath(path.as_posix()) for path in PROTECTED_COMFYUI_PATHS}

    valid = False
    if kind == "move_live_to_backup":
        valid = source == live and destination == backup
    elif kind == "mkdir_incoming_protected":
        valid = source is None and destination == incoming
    elif kind == "move_staged_protected_to_quarantine" and source is not None:
        try:
            relative = source.relative_to(staging)
        except ValueError:
            relative = PurePosixPath(".")
        valid = relative in protected and destination == incoming / relative
    elif kind == "move_staging_to_live":
        valid = source == staging and destination == live
    elif kind == "move_protected_to_live" and source is not None:
        try:
            relative = source.relative_to(backup)
        except ValueError:
            relative = PurePosixPath(".")
        valid = relative in protected and destination == live / relative
    elif kind == "mkdir_dependency_backup":
        valid = source is None and destination == dependency_backup
    elif kind == "move_dependency_to_backup" and source is not None:
        try:
            source_name = source.relative_to(site_packages)
            destination_name = destination.relative_to(dependency_backup)
        except ValueError:
            source_name = destination_name = PurePosixPath(".")
        valid = (
            len(source_name.parts) == 1
            and source_name == destination_name
            and _journal_dependency_name(source_name.name)
        )
    elif kind == "move_overlay_to_site_packages" and source is not None:
        try:
            source_name = source.relative_to(overlay)
            destination_name = destination.relative_to(site_packages)
        except ValueError:
            source_name = destination_name = PurePosixPath(".")
        valid = (
            len(source_name.parts) == 1
            and source_name == destination_name
            and _journal_dependency_name(source_name.name)
        )
    if not valid:
        raise ValueError(f"事务日志包含未授权动作: {kind}")
    return dict(action)


def _validate_recovery_artifact(
    base: Path,
    path: Path,
    *,
    pattern: re.Pattern[str],
    operation_id: str,
    label: str,
) -> Path | None:
    if not _path_exists(path):
        return None
    resolved, observed_id = _assert_direct_child(
        base,
        path,
        pattern=pattern,
        label=label,
    )
    if observed_id != operation_id:
        raise ValueError(f"{label}与事务日志 ID 不匹配")
    return resolved


def _load_recovery_journal(
    base: Path,
    journal_path: Path,
    operation_id: str,
) -> dict[str, object]:
    if (
        journal_path.parent != base
        or journal_path.is_symlink()
        or _is_reparse_point(journal_path)
        or not journal_path.is_file()
        or journal_path.stat().st_size > _MAX_JSON_BYTES
    ):
        raise ValueError("ComfyUI 恢复日志路径或大小无效")
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("ComfyUI 恢复日志无法读取") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("operation_id") != operation_id
    ):
        raise ValueError("ComfyUI 恢复日志版本或事务 ID 无效")
    phase = str(payload.get("phase") or "")
    if phase not in {
        "starting",
        "backing_up_core",
        "quarantining_staged_user_paths",
        "activating_core",
        "restoring_user_paths",
        "installing_dependencies",
        "probing",
        "writing_version",
        "committed",
        "rolling_back",
        "rollback_incomplete",
        "rolled_back",
        "recovering",
        "recovery_incomplete",
    }:
        raise ValueError(f"ComfyUI 恢复日志阶段无效: {phase}")
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list) or len(raw_actions) > 10_000:
        raise ValueError("ComfyUI 恢复日志动作列表无效")
    actions = [
        _validated_recovery_action(action, operation_id=operation_id)
        for action in raw_actions
    ]
    if len(
        {
            (
                action.get("kind"),
                action.get("source"),
                action.get("destination"),
            )
            for action in actions
        }
    ) != len(actions):
        raise ValueError("ComfyUI 恢复日志包含重复动作")
    payload["actions"] = actions
    return payload


def _recovery_failure(
    message: str,
    *,
    operation_id: str = "",
    errors: list[str] | None = None,
    residual_paths: list[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "recovery_incomplete",
        "success": False,
        "recovered": False,
        "message": message,
    }
    if operation_id:
        result["operation_id"] = operation_id
    if errors:
        result["errors"] = errors
    if residual_paths:
        result["residual_paths"] = residual_paths
    return result


def _cleanup_recovery_manifest(
    base: Path,
    operation_id: str,
    warnings: list[str],
) -> None:
    manifest = base / f".comfyui-update-manifest-{operation_id}.json"
    if not _path_exists(manifest):
        return
    try:
        validated = _validated_manifest_path(base, manifest, operation_id)
        validated.unlink()
    except Exception as exc:
        warnings.append(f"更新准备清单未清理: {exc}")


def _validate_recovery_roots(
    base: Path,
    operation_id: str,
    actions: list[dict[str, object]],
) -> dict[str, Path | None]:
    runtime = _plain_directory(base / "runtime", label="runtime 目录")
    if runtime.parent != base:
        raise ValueError("runtime 目录位置无效")
    live = runtime / "ComfyUI"
    if _path_exists(live):
        if live.is_symlink() or _is_reparse_point(live) or not live.is_dir():
            raise ValueError("恢复目标 ComfyUI 不是安全目录")
    staging = base / f".comfyui-update-staging-{operation_id}"
    overlay = base / f".comfyui-update-overlay-{operation_id}"
    backup = base / f".comfyui-update-backup-{operation_id}"
    validated_staging = _validate_recovery_artifact(
        base,
        staging,
        pattern=_STAGING_NAME,
        operation_id=operation_id,
        label="ComfyUI 核心暂存目录",
    )
    validated_overlay = _validate_recovery_artifact(
        base,
        overlay,
        pattern=_OVERLAY_NAME,
        operation_id=operation_id,
        label="依赖暂存目录",
    )
    validated_backup = _validate_recovery_artifact(
        base,
        backup,
        pattern=_BACKUP_NAME,
        operation_id=operation_id,
        label="ComfyUI 备份目录",
    )
    if validated_staging is not None:
        _assert_no_links_in_tree(
            validated_staging,
            label="待恢复的 ComfyUI 核心暂存目录",
        )
    if validated_overlay is not None:
        _assert_no_links_in_tree(
            validated_overlay,
            label="待恢复的依赖暂存目录",
        )
    if validated_backup is not None:
        _assert_no_links_in_tree(
            validated_backup,
            label="待恢复的 ComfyUI 备份目录",
            skip_top_level={path.name for path in PROTECTED_COMFYUI_PATHS},
        )
    has_dependency_actions = any(
        str(action.get("kind") or "")
        in {"move_dependency_to_backup", "move_overlay_to_site_packages"}
        for action in actions
    )
    if has_dependency_actions:
        site_packages = _plain_directory(
            base / ".venv" / "Lib" / "site-packages",
            label="便携依赖 site-packages 目录",
        )
        if site_packages != base / ".venv" / "Lib" / "site-packages":
            raise ValueError("便携依赖目录位置无效")
    for action in actions:
        kind = str(action.get("kind") or "")
        for key in ("source", "destination"):
            raw = action.get(key)
            if raw is None:
                continue
            target = base / Path(*PurePosixPath(str(raw)).parts)
            if kind != "move_protected_to_live" and _path_exists(target) and (
                target.is_symlink() or _is_reparse_point(target)
            ):
                raise ValueError(f"恢复动作路径是链接或重解析点: {raw}")
    return {
        "live": live,
        "staging": validated_staging,
        "overlay": validated_overlay,
        "backup": validated_backup,
    }


def _recover_interrupted_update_locked(base: Path) -> dict[str, object]:
    journals: list[tuple[Path, str]] = []
    residuals: list[str] = []
    orphan_backup = False
    for child in base.iterdir():
        journal_match = _JOURNAL_NAME.fullmatch(child.name)
        if journal_match:
            journals.append((child, journal_match.group(1)))
            continue
        if (
            _STAGING_NAME.fullmatch(child.name)
            or _OVERLAY_NAME.fullmatch(child.name)
            or _MANIFEST_NAME.fullmatch(child.name)
        ):
            residuals.append(child.name)
        if _BACKUP_NAME.fullmatch(child.name):
            residuals.append(child.name)
            orphan_backup = True
    if not journals:
        if orphan_backup:
            return _recovery_failure(
                "发现没有事务日志的 ComfyUI 备份，已保守保留等待人工处理",
                residual_paths=sorted(residuals),
            )
        result: dict[str, object] = {
            "schema_version": 1,
            "status": "no_recovery_needed",
            "success": True,
            "recovered": False,
            "message": "没有需要恢复的 ComfyUI 更新事务",
        }
        if residuals:
            result["residual_paths"] = sorted(residuals)
        return result
    if len(journals) != 1:
        return _recovery_failure(
            "发现多个 ComfyUI 更新事务日志，拒绝自动处理",
            residual_paths=sorted(path.name for path, _ in journals),
        )

    journal_path, operation_id = journals[0]
    try:
        payload = _load_recovery_journal(base, journal_path, operation_id)
        actions = payload["actions"]
        assert isinstance(actions, list)
        roots = _validate_recovery_roots(base, operation_id, actions)
    except Exception as exc:
        return _recovery_failure(
            "ComfyUI 更新事务日志未通过安全校验，未修改任何文件",
            operation_id=operation_id,
            errors=[str(exc)],
            residual_paths=[journal_path.name],
        )

    live = roots["live"]
    staging = base / f".comfyui-update-staging-{operation_id}"
    overlay = base / f".comfyui-update-overlay-{operation_id}"
    backup = base / f".comfyui-update-backup-{operation_id}"
    assert isinstance(live, Path)
    warnings: list[str] = []

    if payload.get("phase") == "committed":
        try:
            if any(action.get("state") != "done" for action in actions):
                raise RuntimeError("已提交事务仍包含未完成动作")
            if (
                not (live / "main.py").is_file()
                or not (live / COMFYUI_VERSION_RECORD).is_file()
            ):
                raise RuntimeError("已提交的新 ComfyUI 缺少核心文件或版本记录")
            if _path_exists(backup):
                _assert_no_links_in_tree(backup, label="已提交的旧 ComfyUI 备份")
                _safe_rmtree(base, backup, expected_name=_BACKUP_NAME)
            warnings.extend(
                _cleanup_unapplied_update(base, staging, overlay, operation_id)
            )
            _cleanup_recovery_manifest(base, operation_id, warnings)
            if warnings:
                return _recovery_failure(
                    "新 ComfyUI 已提交，但仍有事务残留未安全清理",
                    operation_id=operation_id,
                    errors=warnings,
                    residual_paths=[
                        str(path)
                        for path in (backup, staging, overlay, journal_path)
                        if _path_exists(path)
                    ],
                )
            journal_path.unlink()
            return {
                "schema_version": 1,
                "status": "completed",
                "success": True,
                "recovered": True,
                "operation_id": operation_id,
                "message": "已完成上次中断的 ComfyUI 更新收尾",
            }
        except Exception as exc:
            return _recovery_failure(
                "已提交的 ComfyUI 更新无法安全完成收尾",
                operation_id=operation_id,
                errors=[str(exc)],
                residual_paths=[journal_path.name],
            )

    payload["phase"] = "recovering"
    try:
        _atomic_write_json(journal_path, payload)
    except Exception as exc:
        return _recovery_failure(
            "无法记录 ComfyUI 恢复阶段，未执行恢复",
            operation_id=operation_id,
            errors=[str(exc)],
            residual_paths=[journal_path.name],
        )
    errors: list[str] = []
    for action in reversed(actions):
        try:
            _reverse_action(base, action)
            action["recovery_state"] = "done"
        except Exception as exc:
            action["recovery_state"] = "failed"
            action["recovery_error"] = str(exc)
            errors.append(str(exc))
        try:
            _atomic_write_json(journal_path, payload)
        except Exception as exc:
            errors.append(f"无法记录恢复动作: {exc}")
            break
    if not errors:
        if not (live / "main.py").is_file() or (live / "main.py").stat().st_size <= 0:
            errors.append("旧 ComfyUI 核心未能恢复到可用状态")
        if _path_exists(backup):
            errors.append("旧 ComfyUI 备份目录仍未归位")
    if errors:
        payload["phase"] = "recovery_incomplete"
        try:
            _atomic_write_json(journal_path, payload)
        except Exception:
            pass
        return _recovery_failure(
            "上次中断的 ComfyUI 更新未能完整恢复，已保留日志和残留",
            operation_id=operation_id,
            errors=errors,
            residual_paths=[
                str(path)
                for path in (backup, staging, overlay, journal_path)
                if _path_exists(path)
            ],
        )

    warnings.extend(_cleanup_unapplied_update(base, staging, overlay, operation_id))
    _cleanup_recovery_manifest(base, operation_id, warnings)
    if warnings:
        payload["phase"] = "recovery_incomplete"
        try:
            _atomic_write_json(journal_path, payload)
        except Exception:
            pass
        return _recovery_failure(
            "旧 ComfyUI 已恢复，但仍有事务残留未安全清理",
            operation_id=operation_id,
            errors=warnings,
            residual_paths=[
                str(path)
                for path in (staging, overlay, journal_path)
                if _path_exists(path)
            ],
        )
    journal_path.unlink()
    return {
        "schema_version": 1,
        "status": "recovered",
        "success": True,
        "recovered": True,
        "operation_id": operation_id,
        "message": "已恢复上次更新前的 ComfyUI 核心和依赖",
    }


def recover_interrupted_update(base_dir: Path) -> dict[str, object]:
    """Recover one interrupted updater journal without racing an active worker."""
    base = _plain_directory(base_dir, label="客户端目录")
    if base == Path(base.anchor):
        raise ValueError("拒绝在磁盘根目录恢复 ComfyUI 更新")
    update_lock = _UpdaterFileLock(base)
    if not update_lock.acquire(blocking=False):
        return {
            "schema_version": 1,
            "status": "recovery_in_progress",
            "success": False,
            "recovered": False,
            "in_progress": True,
            "message": "ComfyUI 更新仍在进行，暂不执行恢复",
        }
    try:
        return _recover_interrupted_update_locked(base)
    finally:
        update_lock.release()


def _run_prepared_manifest_locked(
    manifest_path: Path,
    result_path: Path | None = None,
    restart_client: Path | None = None,
    *,
    parent_pid: int | None = None,
    wait_timeout_seconds: int = 60,
    parent_waiter: Callable[[int, int], bool] = _wait_for_parent_exit,
    probe_runner: Callable[[Path], object] = _default_probe_runner,
) -> tuple[dict[str, object], Path | None]:
    manifest_input = Path(manifest_path)
    payload = _load_prepared_manifest(manifest_input)
    manifest = manifest_input.resolve(strict=True)
    (
        base_dir,
        staging_core,
        dependency_overlay,
        metadata,
        preparation_warnings,
    ) = _manifest_paths(payload)
    base = _plain_directory(base_dir, label="客户端目录")
    validated_staging, operation_id = _assert_direct_child(
        base,
        staging_core,
        pattern=_STAGING_NAME,
        label="ComfyUI 核心暂存目录",
    )
    validated_overlay: Path | None = None
    if dependency_overlay is not None:
        validated_overlay, overlay_id = _assert_direct_child(
            base,
            dependency_overlay,
            pattern=_OVERLAY_NAME,
            label="依赖暂存目录",
        )
        if overlay_id != operation_id:
            raise ValueError("依赖暂存目录与核心暂存目录事务 ID 不匹配")
    manifest = _validated_manifest_path(base, manifest, operation_id)
    result_file = _fixed_result_path(base, result_path)
    restart = _validate_restart_client(base, restart_client)
    wait_error: Exception | None = None
    if parent_pid is not None:
        parent, timeout = _validated_parent_wait(parent_pid, wait_timeout_seconds)
        try:
            if not parent_waiter(parent, timeout):
                wait_error = TimeoutError(f"等待客户端退出超时（{timeout} 秒）")
        except Exception as exc:
            wait_error = exc
    if wait_error is not None:
        result = {
            "schema_version": 1,
            "status": "failed_rolled_back",
            "success": False,
            "rolled_back": True,
            "message": "客户端未能安全退出，ComfyUI 环境未作修改",
            "error": str(wait_error),
        }
    else:
        try:
            result = apply_prepared_update(
                base,
                staging_core,
                dependency_overlay,
                metadata,
                probe_runner=probe_runner,
            )
        except Exception as exc:
            result = {
                "schema_version": 1,
                "status": "failed_rolled_back",
                "success": False,
                "rolled_back": True,
                "message": "ComfyUI 更新准备无效，未修改本地环境",
                "error": str(exc),
            }
    _merge_cleanup_warnings(result, preparation_warnings)
    _atomic_write_json(result_file, result)
    if wait_error is not None:
        cleanup_warnings = _cleanup_unapplied_update(
            base,
            validated_staging,
            validated_overlay,
            operation_id,
        )
        if cleanup_warnings:
            _merge_cleanup_warnings(result, cleanup_warnings)
            _atomic_write_json(result_file, result)
    try:
        manifest.unlink()
    except OSError as exc:
        result["manifest_cleanup_error"] = str(exc)
        _atomic_write_json(result_file, result)
    return result, restart if wait_error is None else None


def run_prepared_manifest(
    manifest_path: Path,
    result_path: Path | None = None,
    restart_client: Path | None = None,
    *,
    parent_pid: int | None = None,
    wait_timeout_seconds: int = 60,
    parent_waiter: Callable[[int, int], bool] = _wait_for_parent_exit,
    probe_runner: Callable[[Path], object] = _default_probe_runner,
    popen_factory=None,
) -> dict[str, object]:
    """Apply a prepared handoff while exclusively owning the updater lock."""
    payload = _load_prepared_manifest(Path(manifest_path))
    base_dir, _, _, _, _ = _manifest_paths(payload)
    base = _plain_directory(base_dir, label="客户端目录")
    if base == Path(base.anchor):
        raise ValueError("拒绝在磁盘根目录执行 ComfyUI 更新")
    update_lock = _UpdaterFileLock(base)
    if not update_lock.acquire(blocking=True):
        raise RuntimeError("无法获取 ComfyUI 更新锁")
    result: dict[str, object]
    restart: Path | None
    try:
        # Re-read and revalidate the handoff after a possibly blocking lock wait.
        result, restart = _run_prepared_manifest_locked(
            manifest_path,
            result_path,
            restart_client,
            parent_pid=parent_pid,
            wait_timeout_seconds=wait_timeout_seconds,
            parent_waiter=parent_waiter,
            probe_runner=probe_runner,
        )
    finally:
        update_lock.release()
    if restart is not None:
        try:
            _launch_restart(base, restart, popen_factory=popen_factory)
        except Exception as exc:
            result["restart_error"] = str(exc)
            _atomic_write_json(_fixed_result_path(base, result_path), result)
    return result


def consume_update_result(base_dir: Path) -> dict[str, object] | None:
    """Read and remove the fixed updater result exactly once."""
    try:
        base = _plain_directory(base_dir, label="客户端目录")
        result_path = _fixed_result_path(base, None)
    except ValueError:
        return None
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply one prepared ComfyUI update")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--restart-client", type=Path)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--wait-timeout-seconds", type=int, default=60)
    arguments = parser.parse_args(argv)
    try:
        result = run_prepared_manifest(
            arguments.manifest,
            arguments.result,
            arguments.restart_client,
            parent_pid=arguments.parent_pid,
            wait_timeout_seconds=arguments.wait_timeout_seconds,
        )
    except Exception:
        return 2
    return 0 if result.get("status") == "installed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

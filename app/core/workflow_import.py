"""Safe filesystem helpers for importing portable workflow packages."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from app.core.model_maintenance import MODEL_EXTENSIONS


MAX_WORKFLOW_FILES = 4096
MAX_WORKFLOW_BYTES = 256 * 1024 * 1024
_IGNORED_ROOT_NAMES = {"__macosx", ".ds_store"}
_MODEL_SUFFIXES = {suffix.lower() for suffix in MODEL_EXTENSIONS} | {".onnx", ".engine"}
_ALLOWED_ASSET_SUFFIXES = {
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".md",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
}
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "clock$",
    "conin$",
    "conout$",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
_WINDOWS_INVALID_CHARACTERS = set('<>"|?*')
_MAX_RELATIVE_PATH_LENGTH = 220
_MAX_PATH_COMPONENT_LENGTH = 120


def _remove_tree_entry(path: Path) -> None:
    """Remove one known temporary entry without following filesystem links."""
    path = Path(path)
    if path.is_symlink() or _is_reparse_point(path):
        try:
            path.unlink()
        except OSError:
            os.rmdir(path)
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def ensure_safe_workflows_root(path: Path, *, create: bool = False) -> Path:
    """Return a real workflow directory, never a symlink or Windows junction."""
    path = Path(path)
    if not path.exists() and create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path
    if not path.is_dir() or path.is_symlink() or _is_reparse_point(path):
        raise ValueError("工作流目录不能是符号链接或目录联接")
    return path


def _zip_member_path(name: str) -> PurePosixPath:
    normalized = str(name or "").replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized) > _MAX_RELATIVE_PATH_LENGTH
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise ValueError(f"ZIP 包含不安全路径：{name}")
    for part in path.parts:
        base = part.split(".", 1)[0].lower()
        if (
            len(part) > _MAX_PATH_COMPONENT_LENGTH
            or part.startswith(".")
            or part.rstrip(" .") != part
            or base in _WINDOWS_RESERVED_NAMES
            or any(ord(character) < 32 for character in part)
            or any(character in _WINDOWS_INVALID_CHARACTERS for character in part)
        ):
            raise ValueError(f"ZIP 包含 Windows 不支持的路径：{name}")
    return path


def _zip_member_is_link(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(unix_mode)


def create_import_workspace(runtime_dir: Path) -> Path:
    root = Path(runtime_dir) / "workflow_import_tmp"
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or _is_reparse_point(root):
        raise ValueError("工作流临时目录不能是符号链接或目录联接")
    return Path(tempfile.mkdtemp(prefix="import_", dir=root))


def cleanup_stale_workflow_imports(workflows_dir: Path, runtime_dir: Path) -> int:
    """Remove only app-owned staging entries left by an interrupted import."""
    removed = 0
    locations = (
        (Path(workflows_dir), lambda name: name.startswith(".importing_")),
        (
            Path(runtime_dir) / "workflow_import_tmp",
            lambda name: name.startswith("import_") or name == "converted",
        ),
    )
    for parent, matches in locations:
        if not parent.is_dir() or parent.is_symlink() or _is_reparse_point(parent):
            continue
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not matches(entry.name.lower()):
                continue
            try:
                _remove_tree_entry(entry)
                removed += 1
            except OSError:
                continue
    return removed


def extract_zip_safely(
    archive: Path,
    destination: Path,
    *,
    max_files: int = MAX_WORKFLOW_FILES,
    max_bytes: int = MAX_WORKFLOW_BYTES,
) -> Path:
    """Extract a small workflow ZIP without traversal, links or zip bombs."""
    archive = Path(archive)
    destination = Path(destination)
    if destination.exists():
        if destination.is_symlink() or _is_reparse_point(destination):
            raise ValueError("工作流解压目录不能是符号链接或目录联接")
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("工作流解压目录必须是空目录")
    else:
        destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve(strict=True)
    file_count = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            infos = bundle.infolist()
            if len(infos) > max_files:
                raise ValueError(f"工作流包条目过多（最多 {max_files} 个）")
            normalized_targets: set[str] = set()
            for info in infos:
                raw_parts = str(info.filename or "").replace("\\", "/").split("/")
                if raw_parts and (
                    raw_parts[0].casefold() == "__macosx"
                    or raw_parts[-1].casefold() == ".ds_store"
                ):
                    continue
                relative = _zip_member_path(info.filename)
                normalized_target = "/".join(relative.parts).rstrip("/").casefold()
                if normalized_target in normalized_targets:
                    raise ValueError(f"ZIP 包含重复路径：{info.filename}")
                normalized_targets.add(normalized_target)
                if _zip_member_is_link(info):
                    raise ValueError(f"ZIP 不允许包含符号链接：{info.filename}")
                if info.flag_bits & 0x1:
                    raise ValueError("暂不支持加密 ZIP 工作流包")
                if info.is_dir():
                    continue
                if Path(relative.name).suffix.lower() in _MODEL_SUFFIXES:
                    raise ValueError("工作流包不能包含模型文件；请通过“模型与环境”单独导入")
                if relative.name.startswith(".") or Path(relative.name).suffix.lower() not in _ALLOWED_ASSET_SUFFIXES:
                    raise ValueError(f"工作流包包含不支持的文件：{relative.name}")
                file_count += 1
                total_bytes += max(0, int(info.file_size))
                if file_count > max_files:
                    raise ValueError(f"工作流包文件过多（最多 {max_files} 个）")
                if total_bytes > max_bytes:
                    raise ValueError("工作流包过大；模型文件请放到“模型与环境”，不要打进工作流包")

                target = destination.joinpath(*relative.parts)
                if not target.resolve(strict=False).is_relative_to(destination_root):
                    raise ValueError(f"ZIP 路径越出暂存目录：{info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info, "r") as source, open(target, "xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        return destination
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def effective_workflow_root(folder: Path) -> Path:
    """Descend through a single wrapper directory commonly found in ZIPs."""
    current = Path(folder)
    for _ in range(8):
        if current.is_symlink() or _is_reparse_point(current):
            raise ValueError("工作流包不能包含符号链接或目录联接")
        entries = [
            item
            for item in current.iterdir()
            if item.name.lower() not in _IGNORED_ROOT_NAMES
        ]
        files = [item for item in entries if item.is_file()]
        directories = [item for item in entries if item.is_dir()]
        if files or len(directories) != 1:
            return current
        current = directories[0]
    return current


def copy_workflow_assets(
    source_root: Path,
    destination: Path,
    *,
    exclude_names: set[str] | None = None,
    max_files: int = MAX_WORKFLOW_FILES,
    max_bytes: int = MAX_WORKFLOW_BYTES,
) -> None:
    """Copy a small workflow asset tree into a new transaction directory."""
    source_root = Path(source_root)
    destination = Path(destination)
    excluded = {str(name).lower() for name in (exclude_names or set())}
    file_count = 0
    entry_count = 0
    total_bytes = 0

    if source_root.is_symlink() or _is_reparse_point(source_root):
        raise ValueError("工作流目录不能是符号链接或目录联接")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or _is_reparse_point(destination):
        raise ValueError("工作流暂存目录不安全")
    if any(destination.iterdir()):
        raise ValueError("工作流暂存目录必须为空")

    planned: list[tuple[Path, Path, int]] = []
    planned_targets: set[str] = set()

    for root, dirnames, filenames in os.walk(source_root, followlinks=False):
        root_path = Path(root)
        safe_dirs = []
        for name in dirnames:
            child = root_path / name
            entry_count += 1
            if entry_count > max_files:
                raise ValueError(f"工作流目录条目过多（最多 {max_files} 个）")
            if name.startswith("."):
                raise ValueError(f"工作流目录不能包含隐藏目录：{name}")
            if child.is_symlink() or _is_reparse_point(child):
                raise ValueError(f"工作流目录包含不安全链接：{child.name}")
            safe_dirs.append(name)
        dirnames[:] = safe_dirs

        relative_root = root_path.relative_to(source_root)
        for filename in filenames:
            entry_count += 1
            if entry_count > max_files:
                raise ValueError(f"工作流目录条目过多（最多 {max_files} 个）")
            if filename.lower() in excluded:
                continue
            source = root_path / filename
            if source.is_symlink() or _is_reparse_point(source):
                raise ValueError(f"工作流目录包含不安全链接：{filename}")
            if not stat.S_ISREG(source.lstat().st_mode):
                raise ValueError(f"工作流目录包含特殊文件：{filename}")
            if source.suffix.lower() in _MODEL_SUFFIXES:
                raise ValueError("工作流目录不能包含模型文件；请通过“模型与环境”单独导入")
            if filename.startswith(".") or source.suffix.lower() not in _ALLOWED_ASSET_SUFFIXES:
                raise ValueError(f"工作流目录包含不支持的文件：{filename}")
            try:
                size = source.stat().st_size
            except OSError as exc:
                raise ValueError(f"无法读取工作流文件：{filename}") from exc
            file_count += 1
            total_bytes += max(0, size)
            if file_count > max_files:
                raise ValueError(f"工作流目录文件过多（最多 {max_files} 个）")
            if total_bytes > max_bytes:
                raise ValueError("工作流目录过大；模型文件请单独放到“模型与环境”")
            relative_target = (relative_root / filename).as_posix()
            _zip_member_path(relative_target)
            target = destination / relative_root / filename
            if not target.resolve(strict=False).is_relative_to(destination.resolve()):
                raise ValueError(f"工作流文件越出暂存目录：{filename}")
            normalized_target = relative_target.casefold()
            if normalized_target in planned_targets:
                raise ValueError(f"工作流目录包含重复路径：{relative_target}")
            planned_targets.add(normalized_target)
            planned.append((source, target, size))

    for source, target, expected_size in planned:
        if source.is_symlink() or _is_reparse_point(source):
            raise ValueError(f"复制前检测到不安全链接：{source.name}")
        if not stat.S_ISREG(source.lstat().st_mode) or source.stat().st_size != expected_size:
            raise ValueError(f"复制前工作流文件发生变化：{source.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with open(source, "rb") as input_file, open(target, "xb") as output_file:
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size:
                    raise ValueError(f"复制时工作流文件发生变化：{source.name}")
                output_file.write(chunk)
        if copied != expected_size:
            raise ValueError(f"复制时工作流文件发生变化：{source.name}")
        shutil.copystat(source, target, follow_symlinks=False)

"""Portable runtime package contract and verification helpers."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Iterable


RUNTIME_PACKAGE_VERSION = "1.0.0"
RUNTIME_RELEASE_TAG = f"runtime-v{RUNTIME_PACKAGE_VERSION}"
RUNTIME_PACKAGE_NAME = f"runtime-nvidia-rtx30plus-cu130-v{RUNTIME_PACKAGE_VERSION}.7z"
RUNTIME_PACKAGE_SHA256 = "f23fff8ae20e458eddbca1ac30ebbf805d85129aaba9116a1537d9aacd9360d6"
RUNTIME_RELEASE_URL = (
    "https://github.com/Yaro-lu/API/releases/download/"
    f"{RUNTIME_RELEASE_TAG}/{RUNTIME_PACKAGE_NAME}"
)

REQUIRED_RUNTIME_PATHS = (
    Path("runtime/python/python.exe"),
    Path("runtime/ComfyUI/main.py"),
    Path(".venv/Lib/site-packages/torch/__init__.py"),
    Path("bin/cloudflared.exe"),
)
RUNTIME_INSTALL_ROOTS = (
    Path(".venv/Lib"),
    Path(".venv/share"),
    Path("runtime/python"),
    Path("runtime/ComfyUI"),
    Path("bin/cloudflared.exe"),
)
OPTIONAL_RUNTIME_INSTALL_ROOTS = {Path(".venv/share")}
LEGACY_RUNTIME_PATHS = (
    Path(".venv/Scripts"),
    Path(".venv/Include"),
    Path(".venv/pyvenv.cfg"),
)
MAX_RUNTIME_FILES = 150_000
MAX_RUNTIME_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024


def runtime_path_ready(path: Path) -> bool:
    """Reject missing and empty core files when reporting runtime readiness."""
    try:
        return Path(path).is_file() and Path(path).stat().st_size > 0
    except OSError:
        return False


def missing_runtime_paths(base_dir: Path) -> list[str]:
    """Return required package paths missing below ``base_dir``."""
    base_dir = Path(base_dir)
    return [
        path.as_posix()
        for path in REQUIRED_RUNTIME_PATHS
        if not runtime_path_ready(base_dir / path)
    ]


def _normalise_member(name: str) -> str:
    return name.replace("\\", "/").removeprefix("./").rstrip("/").lower()


def missing_archive_entries(members: Iterable[str]) -> list[str]:
    """Validate that an archive uses the package's required root layout."""
    normalised = {_normalise_member(member) for member in members}
    return [
        path.as_posix()
        for path in REQUIRED_RUNTIME_PATHS
        if _normalise_member(path.as_posix()) not in normalised
    ]


def invalid_archive_entries(members: Iterable[str]) -> list[str]:
    """Reject traversal and files outside the environment package roots."""
    invalid: list[str] = []
    for original in members:
        member = _normalise_member(original)
        if not member:
            continue
        parts = member.split("/")
        is_unsafe = (
            original.startswith(("/", "\\"))
            or ".." in parts
            or any(":" in part for part in parts)
        )
        is_allowed = (
            member == ".venv"
            or member.startswith(".venv/")
            or member == "runtime"
            or member in {"runtime/python", "runtime/comfyui"}
            or member.startswith("runtime/python/")
            or member.startswith("runtime/comfyui/")
            or member == "bin"
            or member == "bin/cloudflared.exe"
        )
        comfy_parts = parts[2:] if parts[:2] == ["runtime", "comfyui"] else []
        contains_comfy_user_data = bool(
            comfy_parts
            and comfy_parts[0]
            in {"models", "input", "inputs", "output", "outputs", "temp", "user", "logs", "tasks", "cache", ".git"}
        )
        if is_unsafe or not is_allowed or contains_comfy_user_data:
            invalid.append(original)
    return invalid


def find_extractor(base_dir: Path) -> tuple[str, str] | None:
    """Find a bundled/system 7-Zip, falling back to Windows bsdtar."""
    base_dir = Path(base_dir)
    for name in ("7z.exe", "7zz.exe", "7za.exe"):
        bundled = base_dir / "bin" / name
        if bundled.is_file():
            return "7z", str(bundled)

    for name in ("7z", "7zz", "7za"):
        resolved = shutil.which(name)
        if resolved:
            return "7z", resolved

    for name in ("tar.exe", "bsdtar", "tar"):
        resolved = shutil.which(name)
        if resolved:
            return "tar", resolved
    return None


def archive_list_command(extractor: tuple[str, str], package: Path) -> list[str]:
    kind, executable = extractor
    if kind == "7z":
        return [executable, "l", "-ba", str(package)]
    return [executable, "-tf", str(package)]


def archive_extract_command(
    extractor: tuple[str, str], package: Path, destination: Path
) -> list[str]:
    kind, executable = extractor
    if kind == "7z":
        return [executable, "x", str(package), f"-o{destination}", "-y"]
    return [executable, "-xf", str(package), "-C", str(destination)]


def parse_archive_members(extractor: tuple[str, str], output: str) -> list[str]:
    """Parse member names from 7-Zip's compact listing or bsdtar output."""
    if extractor[0] == "tar":
        return [line.strip() for line in output.splitlines() if line.strip()]

    members: list[str] = []
    for line in output.splitlines():
        # ``7z l -ba`` emits six columns: date, time, attributes, size,
        # compressed size and path.  Split only the first five separators so
        # member names containing spaces remain intact.
        columns = line.strip().split(maxsplit=5)
        if len(columns) == 6:
            members.append(columns[5].strip())
    return members


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_sha256_sidecar(path: Path) -> str:
    text = Path(path).read_text(encoding="utf-8-sig")
    match = re.search(r"(?i)\b([0-9a-f]{64})\b", text)
    if not match:
        raise ValueError(f"SHA256 文件格式无效: {path}")
    return match.group(1).lower()


def verify_sha256(package: Path, sidecar: Path) -> tuple[bool, str, str]:
    expected = read_sha256_sidecar(sidecar)
    actual = sha256_file(package)
    return actual == expected, expected, actual


def verify_runtime_package(package: Path, sidecar: Path | None = None) -> tuple[bool, str, str]:
    """Verify the exact runtime artifact pinned into this client release."""
    package = Path(package)
    if package.name != RUNTIME_PACKAGE_NAME:
        raise ValueError(f"环境包名称不匹配，应为: {RUNTIME_PACKAGE_NAME}")
    expected = RUNTIME_PACKAGE_SHA256.lower()
    if sidecar is not None and Path(sidecar).is_file():
        sidecar_hash = read_sha256_sidecar(Path(sidecar))
        if sidecar_hash != expected:
            return False, expected, sidecar_hash
    actual = sha256_file(package)
    return actual == expected, expected, actual


def _path_exists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _remove_path(path: Path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def validate_staged_runtime(
    staging_dir: Path,
    *,
    max_files: int = MAX_RUNTIME_FILES,
    max_bytes: int = MAX_RUNTIME_EXTRACTED_BYTES,
) -> dict:
    """Validate an extracted environment before it can replace live files."""
    staging_dir = Path(staging_dir)
    if not staging_dir.is_dir():
        raise ValueError("环境包暂存目录不存在")
    missing = missing_runtime_paths(staging_dir)
    if missing:
        raise ValueError(f"环境包目录结构不完整，缺少: {', '.join(missing)}")

    seen = set()
    file_count = 0
    total_bytes = 0
    for root, directories, files in os.walk(staging_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *files]:
            path = root_path / name
            relative = path.relative_to(staging_dir).as_posix()
            invalid = invalid_archive_entries([relative])
            if invalid:
                raise ValueError(f"环境包包含不允许的路径: {invalid[0]}")
            folded = relative.casefold()
            if folded in seen:
                raise ValueError(f"环境包包含大小写重复路径: {relative}")
            seen.add(folded)
            stat = path.lstat()
            attributes = int(getattr(stat, "st_file_attributes", 0) or 0)
            if path.is_symlink() or attributes & 0x400:
                raise ValueError(f"环境包包含链接或重解析点: {relative}")
            if path.is_file():
                if int(getattr(stat, "st_nlink", 1) or 1) > 1:
                    raise ValueError(f"环境包包含硬链接: {relative}")
                file_count += 1
                total_bytes += stat.st_size
                if file_count > max_files:
                    raise ValueError(f"环境包文件数量超过上限: {max_files}")
                if total_bytes > max_bytes:
                    raise ValueError(f"环境包解压大小超过上限: {max_bytes} bytes")
    return {"files": file_count, "bytes": total_bytes}


def install_staged_runtime(staging_dir: Path, base_dir: Path):
    """Transactionally swap only runtime roots and roll back on any failure."""
    staging_dir = Path(staging_dir)
    base_dir = Path(base_dir)
    validate_staged_runtime(staging_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = base_dir / f".runtime-install-backup-{uuid.uuid4().hex}"
    moved_existing: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for relative in (*RUNTIME_INSTALL_ROOTS, *LEGACY_RUNTIME_PATHS):
            source = staging_dir / relative
            destination = base_dir / relative
            backup = backup_dir / relative
            if relative in LEGACY_RUNTIME_PATHS:
                if _path_exists(destination):
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination), str(backup))
                    moved_existing.append((backup, destination))
                continue
            if not _path_exists(source):
                if relative in OPTIONAL_RUNTIME_INSTALL_ROOTS:
                    continue
                raise RuntimeError(f"环境包缺少安装目录: {relative.as_posix()}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if _path_exists(destination):
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(backup))
                moved_existing.append((backup, destination))
            shutil.move(str(source), str(destination))
            installed.append(destination)
        missing = missing_runtime_paths(base_dir)
        if missing:
            raise RuntimeError(f"安装后环境仍不完整，缺少: {', '.join(missing)}")
    except Exception:
        for destination in reversed(installed):
            if _path_exists(destination):
                _remove_path(destination)
        for backup, destination in reversed(moved_existing):
            if _path_exists(backup):
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(destination))
        raise
    finally:
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

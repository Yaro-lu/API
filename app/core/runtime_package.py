"""Portable runtime package contract and verification helpers."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Iterable


RUNTIME_PACKAGE_VERSION = "1.0.0"
RUNTIME_RELEASE_TAG = f"runtime-v{RUNTIME_PACKAGE_VERSION}"
RUNTIME_PACKAGE_NAME = f"runtime-nvidia-rtx30plus-cu130-v{RUNTIME_PACKAGE_VERSION}.7z"
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
        if is_unsafe or not is_allowed:
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
        # ``7z l -ba`` columns end with the archived path.
        match = re.match(r"^\S+\s+\S+\s+\S+\s+\S+\s+(.*)$", line.strip())
        if match:
            members.append(match.group(1).strip())
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

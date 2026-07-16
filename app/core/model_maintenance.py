"""Model package metadata and safe local import helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path
from typing import Callable, Iterable


SAFE_MODEL_EXTENSIONS = (".safetensors", ".gguf")
UNSAFE_MODEL_EXTENSIONS = (".pt", ".pth", ".bin", ".ckpt")
MODEL_EXTENSIONS = SAFE_MODEL_EXTENSIONS + UNSAFE_MODEL_EXTENSIONS
COMFY_MODEL_FOLDERS = {
    "audio_encoders",
    "checkpoints",
    "clip",
    "clip_vision",
    "configs",
    "controlnet",
    "diffusers",
    "diffusion_models",
    "embeddings",
    "gligen",
    "hypernetworks",
    "loras",
    "model_patches",
    "photomaker",
    "style_models",
    "text_encoders",
    "unet",
    "upscale_models",
    "vae",
    "vae_approx",
}

# Exact byte sizes are part of the bundled workflow contract.  They let the
# client reject HTML error pages and interrupted downloads without hashing
# tens of gigabytes on every startup.
MODEL_REQUIREMENTS = {
    "Qwen3.5": {
        "title": "Qwen3.5 文字模型",
        "items": [
            {
                "path": "text_encoders/qwen3.5_4b_bf16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Qwen3.5/resolve/main/text_encoders/qwen3.5_4b_bf16.safetensors",
                "size_bytes": 9_319_828_320,
                "sha256": "9fb3ae42003750fe2d16350259a3ec07761d6d13a8e2b244a6e22fa9d8050841",
            },
        ],
    },
    "Flux2": {
        "title": "Flux2 图片模型",
        "items": [
            {
                "path": "diffusion_models/flux-2-klein-9b-fp8.safetensors",
                "url": "https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8/resolve/main/flux-2-klein-9b-fp8.safetensors",
                "size_bytes": 9_433_061_528,
                "sha256": "865ba09f5b4c3cbd3468a4bd3acb9fcb2f8740c54317482f0bcd4ed1d3655cee",
            },
            {
                "path": "text_encoders/qwen_3_8b_fp8mixed.safetensors",
                "url": "https://huggingface.co/Comfy-Org/flux2-klein-9B/resolve/main/split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors",
                "size_bytes": 8_664_848_742,
                "sha256": "abad16806e0cbabc54e0325d6565847443fe396d5f0be38bb3cd3fe75a1201d6",
            },
            {
                "path": "vae/full_encoder_small_decoder.safetensors",
                "url": "https://huggingface.co/black-forest-labs/FLUX.2-small-decoder/resolve/main/full_encoder_small_decoder.safetensors",
                "size_bytes": 249_519_092,
                "sha256": "ea4273f02d1fafbf8e1d1c2cf6018ed8748652eb0bf34f2dd91171f16f15ab62",
            },
        ],
    },
    "Wan2.1": {
        "title": "Wan2.1 视频模型",
        "items": [
            {
                "path": "diffusion_models/wan2.1_flf2v_720p_14B_fp16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_flf2v_720p_14B_fp16.safetensors",
                "size_bytes": 32_792_693_440,
                "sha256": "bf4ac25667d00f53f49df02c5771f5aa7801c1dcb9b3ccade1407687c426d030",
            },
            {
                "path": "vae/wan_2.1_vae.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors",
                "size_bytes": 253_815_318,
                "sha256": "2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
            },
            {
                "path": "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "size_bytes": 6_735_906_897,
                "sha256": "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
            },
            {
                "path": "clip_vision/clip_vision_h.safetensors",
                "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors",
                "size_bytes": 1_264_219_396,
                "sha256": "64a7ef761bfccbadbaa3da77366aac4185a6c58fa5de5f589b42a65bcc21f161",
            },
        ],
    },
}


def model_file_ready(path: Path, expected_size: int | None = None) -> bool:
    """Return whether a model file exists and matches its known byte size."""
    try:
        path = Path(path)
        if not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False
    return not expected_size or size == int(expected_size)


def model_file_sha256_matches(
    path: Path,
    expected_sha256: str,
    chunk_size: int = 8 * 1024 * 1024,
) -> bool:
    """Stream a model once and compare it with a trusted manifest digest."""
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        return False
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError:
        return False
    return hmac.compare_digest(digest.hexdigest(), expected)


def cleanup_incomplete_imports(models_dir: Path) -> int:
    """Remove temporary imports left by an interrupted previous client run."""
    removed = 0
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        return 0
    for path in models_dir.rglob(".*.importing"):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def check_model_groups(
    models_dir: Path,
    requirements: dict = MODEL_REQUIREMENTS,
) -> dict:
    """Check every model group used by the bundled workflows."""
    models_dir = Path(models_dir)
    result = {"all_ok": False, "missing": {}}
    all_ok = True
    for group, spec in requirements.items():
        missing = []
        for item in spec.get("items", []):
            relative = str(item.get("path") or "").strip()
            if not relative:
                continue
            if not model_file_ready(models_dir / relative, item.get("size_bytes")):
                missing.append(Path(relative).name)
        ready = not missing
        result[group] = "完整" if ready else "缺失"
        result["missing"][group] = missing
        all_ok = all_ok and ready
    result["all_ok"] = all_ok
    return result


def _known_destinations(
    requirements: dict,
) -> dict[str, tuple[Path, int | None, str]]:
    destinations: dict[str, tuple[Path, int | None, str]] = {}
    for spec in requirements.values():
        for item in spec.get("items", []):
            relative = Path(str(item.get("path") or ""))
            if relative.name:
                destinations[relative.name.lower()] = (
                    relative,
                    item.get("size_bytes"),
                    str(item.get("sha256") or ""),
                )
    return destinations


def _candidate_files(source: Path) -> Iterable[Path]:
    for path in source.rglob("*"):
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            yield path


def unsafe_model_files(source: Path) -> list[Path]:
    """List pickle-compatible model files that require an explicit warning."""
    source = Path(source)
    if not source.is_dir():
        return []
    return [
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in UNSAFE_MODEL_EXTENSIONS
    ]


def _unknown_model_destination(source: Path, relative: Path) -> Path:
    parts = relative.parts
    if source.name.lower() == "models":
        return relative
    if parts and parts[0].lower() == "models":
        remaining = parts[1:]
        if remaining:
            return Path(*remaining)
        raise ValueError("models 目录下没有可导入的文件路径")
    for index, part in enumerate(parts):
        if part.lower() in COMFY_MODEL_FOLDERS:
            return Path(*parts[index:])
    if source.name.lower() in COMFY_MODEL_FOLDERS:
        return Path(source.name) / relative
    raise ValueError(
        "无法判断模型类型；请选择 ComfyUI 的 models 目录，或 checkpoints、loras、vae 等具体分类目录"
    )


def import_model_directory(
    source: Path,
    models_dir: Path,
    requirements: dict = MODEL_REQUIREMENTS,
    on_progress: Callable[[int, int, str], None] | None = None,
    allow_unsafe: bool = False,
) -> dict:
    """Copy model files safely and return imported/skipped/failed counters.

    Known bundled filenames are routed to their required ComfyUI subfolders.
    Other model files keep their relative layout.  Every copy lands in a
    temporary file first and is atomically promoted only after size checks.
    """
    source = Path(source)
    models_dir = Path(models_dir)
    if not source.is_dir():
        raise ValueError("选择的模型目录不存在")

    candidates = list(_candidate_files(source))
    known = _known_destinations(requirements)
    result = {
        "found": len(candidates),
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    for index, source_file in enumerate(candidates, 1):
        if on_progress:
            on_progress(index, len(candidates), source_file.name)

        try:
            if source_file.suffix.lower() in UNSAFE_MODEL_EXTENSIONS and not allow_unsafe:
                raise ValueError(
                    "该格式可能包含可执行反序列化内容；仅在确认来源可信后显式允许导入"
                )
            relative = source_file.relative_to(source)
            expected = known.get(source_file.name.lower())
            if expected:
                relative, expected_size, expected_sha256 = expected
            else:
                expected_size = None
                expected_sha256 = ""
                relative = _unknown_model_destination(source, relative)
            destination = models_dir / relative
            if source_file.resolve() == destination.resolve():
                if model_file_ready(source_file, expected_size) and (
                    not expected_sha256
                    or model_file_sha256_matches(source_file, expected_sha256)
                ):
                    result["skipped"] += 1
                    continue
                raise ValueError("文件已在目标目录，但完整性校验未通过")
            source_size = source_file.stat().st_size
            if source_size <= 0:
                raise ValueError("文件为空")
            if expected_size and source_size != int(expected_size):
                raise ValueError(
                    f"文件大小不匹配（应为 {int(expected_size)} 字节，实际 {source_size} 字节）"
                )
            if expected_sha256 and not model_file_sha256_matches(
                source_file, expected_sha256
            ):
                raise ValueError("SHA256 与可信模型清单不一致")
            if model_file_ready(destination, expected_size) and (
                not expected_sha256
                or model_file_sha256_matches(destination, expected_sha256)
            ):
                result["skipped"] += 1
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.importing")
            try:
                temporary.unlink(missing_ok=True)
                shutil.copy2(source_file, temporary)
                if temporary.stat().st_size != source_size:
                    raise IOError("复制后的文件大小与源文件不一致")
                if expected_sha256 and not model_file_sha256_matches(
                    temporary, expected_sha256
                ):
                    raise IOError("复制后的文件 SHA256 校验失败")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            result["imported"] += 1
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append(f"{source_file.name}: {exc}")

    return result

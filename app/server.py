"""
Local AI API Gateway — API 服务器

集成 FastAPI + Bearer 鉴权 + Cloudflare Quick Tunnel
端点：
  POST /                             → 默认工作流（短 URL）
  POST /{workflow_alias}            → 指定工作流（短 URL）
  POST /v1/workflows/run             → 默认工作流
  POST /v1/workflows/run/{wf_id}     → 指定工作流
  GET  /v1/workflows/list            → 工作流列表
  GET  /health                       → 健康检查（免鉴权）
"""
import asyncio
import base64
import binascii
import sys
import json
import random
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote
from typing import Optional
import mimetypes
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import Config  # noqa: E402
from app.core.model_maintenance import MODEL_REQUIREMENTS, check_model_groups  # noqa: E402
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS, missing_runtime_paths  # noqa: E402
from app.core.runtime_state import RuntimeState  # noqa: E402
from app.core.workflow_dependencies import workflow_dependency_report  # noqa: E402
from app.workflow_registry import WorkflowRegistry  # noqa: E402
from app.tunnel.cloudflared_manager import CloudflaredManager  # noqa: E402
from app.engines.comfyui_client import ComfyUIClient  # noqa: E402

# ── 全局 ──────────────────────────────────────────────
config: Optional[Config] = None
state: Optional[RuntimeState] = None
registry: Optional[WorkflowRegistry] = None
tunnel: Optional[CloudflaredManager] = None

# 当前任务状态（由后台线程写入，/health 读取）
current_task: dict = {}
task_records: dict = {}

PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}
PUBLIC_PATHS.add("/healthz")
MANAGEMENT_PATHS = {
    "/v1/workflows/reload",
    "/v1/workflows/rescan",
    "/v1/tunnel/restart",
}
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
MAX_WORKFLOW_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PROMPT_CHARS = 100_000
MAX_TASK_RECORDS = 200
MAX_REQUEST_RECORDS = 200
REQUEST_RECORD_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# mutex for current_task
_task_lock = threading.Lock()
_comfy_nodes_lock = threading.Lock()
_comfy_nodes_cache = {"url": "", "checked_at": 0.0, "nodes": None}


def _clamped_nonterminal_progress_percent(value) -> int:
    try:
        percent = int(float(value))
    except (TypeError, ValueError, OverflowError):
        percent = 0
    return max(0, min(94, percent))


def _next_task_progress_percent(high_water, progress: Optional[dict]) -> int:
    """Keep task progress monotonic while reserving 100 for completion."""
    status = str(progress.get("status") or "").lower() if isinstance(progress, dict) else "running"
    if status == "completed":
        return 100
    candidate = 94 if progress is None else progress.get("percent", 0)
    return max(
        _clamped_nonterminal_progress_percent(high_water),
        _clamped_nonterminal_progress_percent(candidate),
    )


def _workflow_type(workflow_id: str, output_type: str = "") -> str:
    output = (output_type or "").lower()
    if output == "video":
        return "video_flf2v"
    if output == "text":
        return "text_chat"
    if output == "image":
        return "image_t2i"
    text = (workflow_id or "").lower()
    if "wan" in text or "flf2v" in text or "video" in text:
        return "video_flf2v"
    if "text" in text or "chat" in text or "llm" in text:
        return "text_chat"
    return "image_t2i"


def _workflow_json_data(w) -> dict:
    try:
        folder = getattr(w, "folder", None) or BASE_DIR / "workflows" / getattr(w, "id", "")
        path = Path(folder) / "workflow.json"
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _workflow_has_image_input(w) -> bool:
    data = _workflow_json_data(w)
    for node in data.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        if class_type == "LoadImage" or any(key in inputs for key in ("image", "start_image", "end_image")):
            return True
    return False


def _default_input_schema(workflow_type: str) -> dict:
    return {
        "image_t2i": {
            "summary": "输入文字提示词，生成分镜图片。",
            "required": ["prompt"],
            "optional": ["negative_prompt", "size", "width", "height", "steps", "seed"],
            "response": {"type": "image", "format": "url"},
            "inputs": [
                {"name": "prompt", "type": "text", "label": "提示词", "required": True},
                {"name": "negative_prompt", "type": "text", "label": "反向提示词", "required": False},
                {"name": "size", "type": "string", "label": "尺寸", "required": False},
                {"name": "seed", "type": "integer", "label": "随机种子", "required": False},
            ],
        },
        "video_flf2v": {
            "summary": "输入文字动作提示词、首帧图片、尾帧图片，生成分镜视频。",
            "required": ["prompt", "start_image", "end_image"],
            "optional": ["duration", "fps", "seed"],
            "response": {"type": "video", "format": "url"},
            "inputs": [
                {"name": "prompt", "type": "text", "label": "动作/镜头提示词", "required": True},
                {"name": "start_image", "type": "image", "label": "首帧图片", "required": True},
                {"name": "end_image", "type": "image", "label": "尾帧图片", "required": True},
                {"name": "duration", "type": "number", "label": "时长秒", "required": False},
                {"name": "seed", "type": "integer", "label": "随机种子", "required": False},
            ],
        },
        "text_chat": {
            "summary": "输入文字需求，生成脚本、角色或分镜结构化 JSON。",
            "required": ["prompt"],
            "optional": ["messages", "response_format"],
            "response": {"type": "json", "format": "json_object"},
            "inputs": [
                {"name": "prompt", "type": "text", "label": "文字需求", "required": True},
                {"name": "messages", "type": "messages", "label": "聊天消息", "required": False},
                {"name": "response_format", "type": "object", "label": "响应格式", "required": False, "default": {"type": "json_object"}},
            ],
        },
        "text_vision": {
            "summary": "输入文字需求和图片，生成图像描述或提示词文本。此类工作流不用于脚本/分镜结构化生成。",
            "required": ["prompt", "image"],
            "optional": ["response_format"],
            "response": {"type": "text", "format": "plain_text"},
            "inputs": [
                {"name": "prompt", "type": "text", "label": "文字需求", "required": True},
                {"name": "image", "type": "image", "label": "输入图片", "required": True},
                {"name": "response_format", "type": "object", "label": "响应格式", "required": False},
            ],
        },
    }.get(workflow_type, {
        "summary": "自定义工作流，请在工作流配置中补充输入说明。",
        "required": ["prompt"],
        "optional": [],
        "response": {"type": "unknown"},
        "inputs": [{"name": "prompt", "type": "text", "label": "提示词", "required": True}],
    })


def _normalize_input_schema(schema: dict, workflow_type: str) -> dict:
    base = _default_input_schema(workflow_type)
    if not isinstance(schema, dict) or not schema:
        return base
    merged = {**base, **schema}
    if isinstance(schema.get("inputs"), list) and schema.get("inputs"):
        merged["inputs"] = schema["inputs"]
    if not isinstance(merged.get("required"), list):
        merged["required"] = base.get("required", [])
    if not isinstance(merged.get("optional"), list):
        merged["optional"] = base.get("optional", [])
    return merged


def _installed_comfy_node_types(cache_seconds: float = 30.0) -> set[str] | None:
    """Read ComfyUI node types with a short cache; unavailable means unverified."""
    if config is None:
        return None
    url = str(config.comfyui_url or "").rstrip("/")
    if not url:
        return None
    now = time.monotonic()
    with _comfy_nodes_lock:
        cached_nodes = _comfy_nodes_cache["nodes"]
        cache_ttl = max(0.0, cache_seconds) if cached_nodes is not None else min(1.0, max(0.0, cache_seconds))
        if (
            _comfy_nodes_cache["url"] == url
            and now - float(_comfy_nodes_cache["checked_at"]) < cache_ttl
        ):
            return set(cached_nodes) if cached_nodes is not None else None
    try:
        import requests

        response = requests.get(f"{url}/object_info", timeout=1.0)
        response.raise_for_status()
        data = response.json()
        nodes = set(data) if isinstance(data, dict) else None
    except Exception:
        nodes = None
    with _comfy_nodes_lock:
        _comfy_nodes_cache.update(
            {"url": url, "checked_at": now, "nodes": set(nodes) if nodes is not None else None}
        )
    return nodes


def _workflow_payload(w) -> dict:
    workflow_type = _workflow_type(w.id, getattr(w, "output_type", ""))
    if workflow_type == "text_chat" and _workflow_has_image_input(w):
        workflow_type = "text_vision"
    input_schema = _normalize_input_schema(getattr(w, "input_schema", {}) or {}, workflow_type)
    dependency = workflow_dependency_report(
        getattr(w, "dependencies", {}) or {},
        BASE_DIR / "models",
        installed_nodes=_installed_comfy_node_types(),
    )
    workflow_data = _workflow_json_data(w)
    workflow_file_ready = bool(
        workflow_data
        and any(
            isinstance(node, dict) and str(node.get("class_type") or "").strip()
            for node in workflow_data.values()
        )
    )
    dependency_status = dependency["dependency_status"]
    if not workflow_file_ready:
        validation_status = "file_error"
    elif not w.enabled:
        validation_status = "disabled"
    else:
        validation_status = dependency_status
    available = bool(
        w.enabled
        and workflow_file_ready
        and dependency_status == "ready"
        and dependency["available"]
    )
    return {
        "id": w.id,
        "name": w.name,
        "enabled": w.enabled,
        "description": w.description,
        "type": workflow_type,
        "output_type": getattr(w, "output_type", "") or ("video" if workflow_type == "video_flf2v" else "text" if workflow_type == "text_chat" else "image"),
        "input_schema": input_schema,
        "inputs": input_schema["inputs"],
        "is_default": w.id == registry.default_workflow_id,
        "workflow_json": getattr(w, "workflow_json", ""),
        "dependencies": dependency["dependencies"],
        "required_models": dependency["required_models"],
        "missing_models": dependency["missing_models"],
        "unverified_models": dependency["unverified_models"],
        "required_nodes": dependency["required_nodes"],
        "missing_nodes": dependency["missing_nodes"],
        "nodes_verified": dependency["nodes_verified"],
        "dependency_status": dependency_status,
        "validation_status": validation_status,
        "available": available,
    }

def _model_group_for_type(model_type: str) -> str:
    text = (model_type or "").lower()
    if "video" in text or "flf2v" in text:
        return "video"
    if "text" in text or "chat" in text:
        return "text"
    return "image"


def _local_model_status(base_dir: Path | None = None, requirements: dict | None = None) -> dict:
    groups = check_model_groups(
        Path(base_dir or BASE_DIR) / "models",
        requirements or MODEL_REQUIREMENTS,
    )
    return {
        "qwen35": groups.get("Qwen3.5") == "完整",
        "flux2": groups.get("Flux2") == "完整",
        "wan21": groups.get("Wan2.1") == "完整",
    }


def _local_runtime_status(base_dir: Path | None = None) -> dict:
    base_dir = Path(base_dir or BASE_DIR)
    missing = missing_runtime_paths(base_dir)
    missing_set = set(missing)
    python_path = REQUIRED_RUNTIME_PATHS[0].as_posix()
    comfyui_path = REQUIRED_RUNTIME_PATHS[1].as_posix()
    return {
        "status": "installed" if not missing else "missing",
        "python": python_path not in missing_set,
        "comfyui": comfyui_path not in missing_set,
        "missing": missing,
    }


def _workflow_available(model_id: str, model_type: str) -> bool:
    status = _local_model_status()
    text = str(model_id or "").lower()
    if "wan" in text or "wan2.1" in text:
        return status["wan21"]
    if "flux" in text:
        return status["flux2"]
    if "qwen" in text or "千问" in text:
        return status["qwen35"]
    return True


def _models_from_workflows() -> list:
    models = []
    seen = set()
    for wf in registry.workflows:
        payload = _workflow_payload(wf)
        model_id = payload["id"]
        available = bool(payload.get("available"))
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({
            "id": model_id,
            "model": model_id,
            "name": payload.get("name") or model_id,
            "label": payload.get("name") or model_id,
            "type": payload.get("type", ""),
            "group": _model_group_for_type(payload.get("type", "")),
            "available": available,
            "workflowId": model_id,
            "workflow_id": model_id,
            "description": payload.get("description", ""),
            "input_schema": payload.get("input_schema") or {},
            "inputs": payload.get("inputs") or [],
        })
    return models


def _active_public_base_url() -> str:
    if tunnel and tunnel.is_online and state and state.base_url:
        return state.base_url.rstrip("/")
    return ""


def _tunnel_health_status() -> str:
    if not tunnel:
        return "offline"
    if tunnel.is_online:
        return "online"
    status = str(getattr(tunnel.state, "status", "offline") or "offline")
    if status == "failed":
        return "unavailable"
    if status in ("starting", "retrying"):
        return status
    return "offline"


def _public_base_url() -> str:
    public_url = _active_public_base_url()
    if public_url:
        return public_url
    if state and state.local_api:
        return state.local_api.rstrip("/")
    return "http://127.0.0.1:18188"


async def _restart_tunnel_manager() -> dict:
    """Clear stale publication state before restarting Tunnel off the event loop."""
    global tunnel
    state.set_offline()
    if tunnel is None:
        start_tunnel(config)
    else:
        await asyncio.to_thread(tunnel.restart)
    return {
        "ok": bool(tunnel and tunnel.state.status in ("starting", "retrying", "online")),
        "status": tunnel.state.status if tunnel else "offline",
        "url": tunnel.state.base_url if tunnel else "",
        "error": tunnel.state.error if tunnel else "Tunnel manager not initialized",
    }


def _output_download_path(task_id: str, filename: str) -> str:
    safe_task_id = quote(str(task_id or ""), safe="")
    safe_filename = quote(Path(str(filename or "")).name, safe="")
    return f"/v1/files/{safe_task_id}/{safe_filename}"


def _with_output_urls(task_id: str, outputs: list) -> list:
    normalized = []
    for item in outputs or []:
        if not isinstance(item, dict):
            continue
        if item.get("text") is not None:
            normalized.append({
                **item,
                "type": "text",
                "text": str(item.get("text") or ""),
            })
            continue
        filename = str(item.get("filename") or item.get("file") or "").strip()
        if not filename:
            continue
        safe_filename = Path(filename).name
        download_path = _output_download_path(task_id, safe_filename)
        url = f"{_public_base_url()}{download_path}"
        normalized.append({
            **item,
            "filename": safe_filename,
            "url": url,
            "download_url": url,
            "download_path": download_path,
        })
    return normalized


def _read_text_output_file(task_id: str, item: dict) -> str:
    filename = str(item.get("filename") or item.get("file") or "").strip()
    if not filename:
        return ""
    ext = Path(filename).suffix.lower()
    if ext not in (".txt", ".json", ".md", ".log", ".csv"):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith("text/") and "json" not in guessed:
            return ""
    file_path = _find_output_file_from_record(task_id, filename, item)
    if not file_path:
        return ""
    try:
        if file_path.stat().st_size > 2 * 1024 * 1024:
            return ""
        return file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _find_output_file_from_record(task_id: str, filename: str, item: dict = None) -> Optional[Path]:
    safe_name = Path(filename).name
    if not safe_name:
        return None
    item = item or {}
    candidates = []
    subfolder = str(item.get("subfolder") or "").strip().replace("\\", "/")
    if subfolder and ".." not in subfolder.split("/"):
        candidates.append(BASE_DIR / "outputs" / subfolder / safe_name)
    candidates.extend([
        BASE_DIR / "outputs" / safe_name,
        BASE_DIR / "outputs" / task_id / safe_name,
        BASE_DIR / "runtime" / "outputs" / task_id / safe_name,
    ])
    roots = [
        (BASE_DIR / "outputs").resolve(),
        (BASE_DIR / "runtime" / "outputs").resolve(),
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_file() and any(_path_is_within(resolved, root) for root in roots):
                return resolved
        except Exception:
            continue
    return None


def _task_text_output(record: dict) -> str:
    task_id = str(record.get("task_id") or record.get("id") or "")
    parts = []
    for item in record.get("outputs") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or item.get("value") or "").strip()
        if not text:
            text = _read_text_output_file(task_id, item).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _task_api_response(record: dict) -> dict:
    payload = dict(record or {})
    for private_field in ("prompt", "log_offset", "started_at_ts"):
        payload.pop(private_field, None)
    task_id = payload.get("task_id") or payload.get("id") or ""
    payload["id"] = task_id
    payload["task_id"] = task_id
    outputs = _with_output_urls(task_id, payload.get("outputs") or [])
    if outputs:
        payload["outputs"] = outputs
        first = outputs[0]
        payload["output"] = {
            "filename": first.get("filename", ""),
            "type": first.get("type", "image"),
            "url": first.get("url", ""),
            "download_url": first.get("download_url", ""),
            "download_path": first.get("download_path", ""),
        }
        if _guess_media_type(str(first.get("filename") or "")).startswith("image/"):
            payload["data"] = [{"url": first.get("url", "")}]
    text_output = _task_text_output(payload)
    if text_output:
        payload["text"] = text_output
        payload["output"] = {
            **(payload.get("output") if isinstance(payload.get("output"), dict) else {}),
            "text": text_output,
        }
    return payload


def _set_task_record(task_id: str, patch: dict):
    with _task_lock:
        record = task_records.get(task_id, {})
        record.update(patch)
        record["updated_at"] = time.time()
        task_records[task_id] = record
        _prune_task_records_locked()
        if current_task.get("task_id") == task_id:
            current_task.update(record)


def _prune_task_records_locked(max_records: int = MAX_TASK_RECORDS):
    """Bound in-memory history without discarding the currently running task."""
    overflow = len(task_records) - max(1, int(max_records))
    if overflow <= 0:
        return
    current_id = str(current_task.get("task_id") or "")
    candidates = sorted(
        (
            (task_id, float(record.get("updated_at") or record.get("started_at_ts") or 0))
            for task_id, record in task_records.items()
            if task_id != current_id
        ),
        key=lambda item: item[1],
    )
    for task_id, _updated_at in candidates[:overflow]:
        task_records.pop(task_id, None)


def _cleanup_request_records(requests_dir: Path):
    """Remove old request metadata only; generated user outputs are never touched."""
    try:
        files = sorted(
            Path(requests_dir).glob("task_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    cutoff = time.time() - REQUEST_RECORD_MAX_AGE_SECONDS
    for index, path in enumerate(files):
        try:
            if index >= MAX_REQUEST_RECORDS or path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _clean_task_text(value, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:limit]


def _task_title_from_body(body: dict, workflow_id: str) -> str:
    for key in ("sceneTitle", "scene_title", "shotTitle", "shot_title", "title", "name"):
        text = _clean_task_text(body.get(key), 80)
        if text:
            return text
    prefix = _clean_task_text(body.get("filename_prefix"), 80)
    if prefix:
        return prefix.rsplit("-", 1)[0] or prefix
    return workflow_id


def _size_to_dimensions(body: dict) -> tuple[int, int]:
    width = int(body.get("width") or 0) if str(body.get("width") or "").isdigit() else 0
    height = int(body.get("height") or 0) if str(body.get("height") or "").isdigit() else 0
    if width > 0 and height > 0:
        return width, height

    size = str(body.get("size") or "").strip()
    if "x" in size.lower():
        left, _, right = size.lower().partition("x")
        if left.strip().isdigit() and right.strip().isdigit():
            return int(left.strip()), int(right.strip())
    compact = size.replace(" ", "").upper()
    if compact == "1K":
        return 1024, 1024
    if compact == "2K":
        return 2048, 2048
    if compact == "4K":
        return 4096, 4096
    return 1024, 1024


def _bounded_integer(value, *, name: str, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise HTTPException(422, detail=f"{name} 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, detail=f"{name} 必须是整数") from None
    if result < minimum or result > maximum:
        raise HTTPException(422, detail=f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def _decode_workflow_image(value, *, name: str) -> tuple[bytes, str, str]:
    """Decode a browser data URL after bounding its type and decoded size."""
    if not isinstance(value, str) or not value.startswith("data:image/"):
        raise HTTPException(422, detail=f"{name} 必须是 PNG、JPEG 或 WebP 图片")
    match = re.fullmatch(
        r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        raise HTTPException(422, detail=f"{name} 图片数据格式无效")
    mime_type = match.group(1).lower()
    encoded = match.group(2)
    if len(encoded) > ((MAX_WORKFLOW_IMAGE_BYTES + 2) // 3) * 4:
        raise HTTPException(413, detail=f"{name} 不能超过 {MAX_WORKFLOW_IMAGE_BYTES // (1024 * 1024)} MB")
    try:
        image_data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, detail=f"{name} 图片数据格式无效") from None
    if not image_data or len(image_data) > MAX_WORKFLOW_IMAGE_BYTES:
        raise HTTPException(413, detail=f"{name} 不能超过 {MAX_WORKFLOW_IMAGE_BYTES // (1024 * 1024)} MB")

    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime_type]
    valid_signature = (
        (mime_type == "image/png" and image_data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime_type == "image/jpeg" and image_data.startswith(b"\xff\xd8\xff"))
        or (
            mime_type == "image/webp"
            and len(image_data) >= 12
            and image_data[:4] == b"RIFF"
            and image_data[8:12] == b"WEBP"
        )
    )
    if not valid_signature:
        raise HTTPException(422, detail=f"{name} 图片内容与声明格式不一致")
    return image_data, mime_type, extension


def _load_image_body_key(node: dict) -> str:
    if str(node.get("class_type") or "") != "LoadImage":
        return ""
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    metadata = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    hint = f"{metadata.get('title', '')} {inputs.get('image', '')}".lower()
    if "start" in hint or "首帧" in hint:
        return "start_image"
    if "end" in hint or "尾帧" in hint:
        return "end_image"
    return ""


def _upload_workflow_images(client, workflow_data: dict, body: dict, task_id: str) -> None:
    """Upload request images to ComfyUI and bind them to matching LoadImage nodes."""
    mapped_nodes: dict[str, list[dict]] = {"start_image": [], "end_image": []}
    for node in workflow_data.values():
        if not isinstance(node, dict):
            continue
        key = _load_image_body_key(node)
        if key:
            mapped_nodes[key].append(node)

    labels = {"start_image": "首帧图片", "end_image": "尾帧图片"}
    prepared: dict[str, tuple[bytes, str, str]] = {}
    for key, nodes in mapped_nodes.items():
        if not nodes:
            continue
        if not body.get(key):
            raise HTTPException(422, detail=f"缺少{labels[key]}")
        prepared[key] = _decode_workflow_image(body[key], name=labels[key])

    for key, (image_data, mime_type, extension) in prepared.items():
        filename = f"lingjing_{task_id}_{'start' if key == 'start_image' else 'end'}.{extension}"
        uploaded_name = client.upload_input_image(image_data, filename, mime_type)
        for node in mapped_nodes[key]:
            node.setdefault("inputs", {})["image"] = uploaded_name
        body[key] = uploaded_name


def _validated_generation_body(body: dict) -> dict:
    """Return a bounded copy before values reach ComfyUI or local persistence."""
    if not isinstance(body, dict):
        raise HTTPException(422, detail="请求正文必须是 JSON 对象")
    result = dict(body)
    prompt = str(result.get("prompt") or "")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(422, detail=f"prompt 最多允许 {MAX_PROMPT_CHARS} 个字符")
    result["prompt"] = prompt
    negative_prompt = str(result.get("negative_prompt") or result.get("negativePrompt") or "")
    if len(negative_prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(422, detail=f"negative_prompt 最多允许 {MAX_PROMPT_CHARS} 个字符")
    if "negative_prompt" in result or "negativePrompt" in result:
        result["negative_prompt"] = negative_prompt
    raw_width = result.get("width")
    raw_height = result.get("height")
    size_text = str(result.get("size") or "").strip()
    dimensions_explicit = (
        raw_width not in (None, "")
        or raw_height not in (None, "")
        or bool(size_text)
    )
    if raw_width not in (None, "") or raw_height not in (None, ""):
        if raw_width in (None, "") or raw_height in (None, ""):
            raise HTTPException(422, detail="width 和 height 必须同时提供")
        width = _bounded_integer(
            raw_width, name="width", default=1024, minimum=64, maximum=8192
        )
        height = _bounded_integer(
            raw_height, name="height", default=1024, minimum=64, maximum=8192
        )
    else:
        compact_size = size_text.replace(" ", "").upper()
        if size_text and not (
            compact_size in {"1K", "2K", "4K"}
            or re.fullmatch(r"\d{2,5}[xX]\d{2,5}", compact_size)
        ):
            raise HTTPException(422, detail="size 必须是 WIDTHxHEIGHT、1K、2K 或 4K")
        width, height = _size_to_dimensions(result)
        width = _bounded_integer(width, name="width", default=1024, minimum=64, maximum=8192)
        height = _bounded_integer(height, name="height", default=1024, minimum=64, maximum=8192)
    if width * height > 33_554_432:
        raise HTTPException(422, detail="图片总像素不能超过 33554432")
    result["width"] = width
    result["height"] = height
    result["_dimensions_explicit"] = dimensions_explicit
    result["steps"] = _bounded_integer(
        result.get("steps"), name="steps", default=20, minimum=1, maximum=200
    )
    if "duration" in result:
        result["duration"] = _bounded_integer(
            result.get("duration"), name="duration", default=5, minimum=1, maximum=30
        )
    if "fps" in result:
        result["fps"] = _bounded_integer(
            result.get("fps"), name="fps", default=16, minimum=1, maximum=60
        )
    if "seed" in result:
        result["seed"] = _normalize_seed(result.get("seed"))
    return result


def _image_workflow_from_model(model: str) -> str:
    text = str(model or "").strip().lower().replace("_", "-")
    if not text:
        return "flux_t2i_v1"
    aliases = {
        "flux",
        "flux2",
        "flux-t2i-v1",
        "flux2-klein-9b-fp8",
        "flux-2-klein-9b-fp8",
        "doubao-seedream-4-5-251128",
        "doubao-seedream-5-0-260128",
    }
    if text in aliases or "seedream" in text or "flux" in text:
        return "flux_t2i_v1"
    return model


def _prefers_async_image_response(body: dict, prefer_header: str = "") -> bool:
    if body.get("async") is True:
        return True
    for preference in str(prefer_header or "").split(","):
        if preference.strip().split(";", 1)[0].lower() == "respond-async":
            return True
    return False


def _image_task_submission_payload(submitted: dict, model: str) -> dict:
    task_id = str(submitted.get("task_id") or submitted.get("id") or "")
    status_path = f"/v1/tasks/{quote(task_id, safe='')}"
    return {
        "id": task_id,
        "task_id": task_id,
        "object": "image.generation.task",
        "created": int(time.time()),
        "status": "submitted",
        "model": model or submitted.get("workflow_id") or "",
        "workflow": submitted.get("workflow_id") or "",
        "workflow_id": submitted.get("workflow_id") or "",
        "workflow_name": submitted.get("workflow_name") or "",
        "prompt_id": submitted.get("prompt_id") or "",
        "status_path": status_path,
        "status_url": f"{_public_base_url()}{status_path}",
        "poll_after_ms": 3000,
        "data": [],
    }


def _workflow_task_submission_payload(
    submitted: dict,
    requested_workflow: str = "",
    output_type: str = "",
) -> dict:
    task_id = str(submitted.get("task_id") or submitted.get("id") or "")
    status_path = f"/v1/tasks/{quote(task_id, safe='')}"
    return {
        "id": task_id,
        "task_id": task_id,
        "object": "workflow.task",
        "created": int(time.time()),
        "status": "submitted",
        "requested_workflow": requested_workflow,
        "workflow": submitted.get("workflow_id") or "",
        "workflow_id": submitted.get("workflow_id") or "",
        "workflow_name": submitted.get("workflow_name") or "",
        "output_type": output_type,
        "prompt_id": submitted.get("prompt_id") or "",
        "status_path": status_path,
        "status_url": f"{_public_base_url()}{status_path}",
        "poll_after_ms": 3000,
        "data": [],
    }


def _normalize_seed(value) -> int:
    """ComfyUI 的 noise_seed 不接受 -1；外部 API 里 -1 统一表示随机种子。"""
    try:
        seed = int(value)
    except Exception:
        seed = -1
    if seed < 0:
        return random.randint(0, 2**32 - 1)
    return seed


def _guess_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    if ext == ".mp4":
        return "video/mp4"
    if ext == ".mov":
        return "video/quicktime"
    if ext == ".webm":
        return "video/webm"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ── 请求边界与鉴权中间件 ──────────────────────────────
async def _send_json_response(send, status: int, payload: dict, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response_headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    response_headers.extend(headers or [])
    await send({"type": "http.response.start", "status": status, "headers": response_headers})
    await send({"type": "http.response.body", "body": body})


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        content_length = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    content_length = int(value.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    await _send_json_response(
                        send, 400, {"error": {"code": "invalid_content_length", "message": "Content-Length 无效"}}
                    )
                    return
                if content_length < 0:
                    await _send_json_response(
                        send, 400, {"error": {"code": "invalid_content_length", "message": "Content-Length 无效"}}
                    )
                    return
                break
        if content_length is not None and content_length > self.max_bytes:
            await _send_json_response(
                send, 413, {"error": {"code": "request_too_large", "message": "请求正文过大"}}
            )
            return

        consumed = 0
        response_started = False

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if not response_started:
                await _send_json_response(
                    send, 413, {"error": {"code": "request_too_large", "message": "请求正文过大"}}
                )


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secure_send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secure_send)


class AuthMiddleware:
    """纯 ASGI 中间件 — Bearer API Key 鉴权"""
    def __init__(self, app):
        self.app = app
        self._rate_windows = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if str(scope.get("method") or "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        auth = ""
        for header in scope.get("headers", []):
            if header[0] == b"authorization":
                # ASGI headers are arbitrary bytes.  Latin-1 is lossless and
                # guarantees malformed unauthenticated input becomes a clean
                # 401 instead of escaping as UnicodeDecodeError/500.
                auth = header[1].decode("latin-1")
                break

        if not auth.startswith("Bearer "):
            await self._error(send, 401, "unauthorized", "Missing Authorization header")
            return

        token = auth[7:].strip()
        api_key = str(getattr(state, "api_key", "") or "")
        admin_key = str(getattr(state, "admin_key", "") or "")
        management = path in MANAGEMENT_PATHS
        expected = admin_key if management else api_key
        token_bytes = token.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
        if not expected or not secrets.compare_digest(token_bytes, expected_bytes):
            if (
                management
                and api_key
                and secrets.compare_digest(token_bytes, api_key.encode("utf-8"))
            ):
                await self._error(send, 403, "forbidden", "Management credential required")
            else:
                await self._error(send, 401, "unauthorized", "Invalid API key")
            return

        if not self._within_rate_limit(token, path, str(scope.get("method") or "GET")):
            await self._error(
                send,
                429,
                "rate_limit_exceeded",
                "请求过于频繁，请稍后重试",
                headers=[(b"retry-after", b"60")],
            )
            return

        await self.app(scope, receive, send)

    def _within_rate_limit(self, token: str, path: str, method: str) -> bool:
        now = time.monotonic()
        is_management = path in MANAGEMENT_PATHS
        is_generation = method.upper() == "POST" and not is_management
        limit = 10 if is_management else 30 if is_generation else 300
        category = "admin" if is_management else "generation" if is_generation else "read"
        key = (token, category)
        window = [stamp for stamp in self._rate_windows.get(key, []) if now - stamp < 60]
        if len(window) >= limit:
            self._rate_windows[key] = window
            return False
        window.append(now)
        self._rate_windows[key] = window
        return True

    async def _error(self, send, status: int, code: str, msg: str, headers=None):
        await _send_json_response(
            send,
            status,
            {"error": {"code": code, "message": msg}},
            headers=headers,
        )


# ── 创建应用 ──────────────────────────────────────────
def create_app() -> FastAPI:
    global config, state, registry, tunnel

    # 如果 main() 已经初始化过，则跳过重复初始化
    if config is None:
        config = Config(BASE_DIR)
    if state is None:
        state = RuntimeState(config.runtime_dir)
        state.start_session(config.server_port)

    registry = WorkflowRegistry(
        config_path=BASE_DIR / "runtime" / "workflow_config.json",
        workflows_dir=BASE_DIR / "workflows",
    )
    registry.scan_folder()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            # Graceful server shutdown must also stop its Tunnel child.
            if tunnel is not None:
                tunnel.stop()
            if state is not None:
                state.set_offline()

    app = FastAPI(title="Local AI API Gateway", version="1.0.0", lifespan=lifespan)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Prefer", "Accept", "Range"],
        expose_headers=["Location", "Retry-After", "Preference-Applied", "Content-Range", "Accept-Ranges"],
    )

    def _inject_workflow_params(workflow_data: dict, body: dict):
        prompt = body.get("prompt", "")
        response_format = body.get("response_format") or body.get("responseFormat") or {}
        wants_json = (
            (isinstance(response_format, dict) and str(response_format.get("type") or "").lower() == "json_object")
            or str(response_format).lower() == "json_object"
        )
        if wants_json and prompt and "STRICT OUTPUT CONTRACT" not in str(prompt):
            prompt = (
                f"{prompt}\n\n"
                "STRICT OUTPUT CONTRACT:\n"
                "- Return exactly one valid JSON object.\n"
                "- Do not wrap the JSON in Markdown fences.\n"
                "- Do not add explanations, comments, prefixes, suffixes, or extra text.\n"
                "- The JSON fields must match the upstream system prompt."
            )
        negative_prompt = body.get("negative_prompt", body.get("negativePrompt", ""))
        seed = _normalize_seed(body.get("seed", -1))
        steps = body.get("steps", 20)
        width = body.get("width", 1024)
        height = body.get("height", 1024)
        dimensions_explicit = body.get("_dimensions_explicit") is True
        requested_duration = body.get("duration")
        requested_fps = body.get("fps")
        effective_fps = requested_fps
        if effective_fps is None:
            for candidate in workflow_data.values():
                candidate_inputs = candidate.get("inputs", {}) if isinstance(candidate, dict) else {}
                if "fps" in candidate_inputs:
                    try:
                        effective_fps = max(1, int(float(candidate_inputs["fps"])))
                    except (TypeError, ValueError, OverflowError):
                        effective_fps = None
                    if effective_fps is not None:
                        break
        frame_length = None
        if requested_duration is not None and effective_fps is not None:
            target_frames = max(1, int(requested_duration) * int(effective_fps))
            # Wan video latent lengths use the 4n+1 sequence. Pick the nearest
            # valid length so the UI's seconds control has a real effect.
            frame_length = max(1, ((target_frames + 1) // 4) * 4 + 1)

        for node_id, node in workflow_data.items():
            ctype = node.get("class_type", "")
            inputs = node.get("inputs", {})
            title = node.get("_meta", {}).get("title", "")
            title_lower = title.lower()
            if ctype == "CLIPTextEncode" and "text" in inputs:
                if "negative" in title_lower:
                    inputs["text"] = negative_prompt
                elif title == "CLIP Text Encode (Positive Prompt)" or not title:
                    inputs["text"] = prompt
            elif ctype == "RandomNoise":
                inputs["noise_seed"] = seed
            elif ctype == "Flux2Scheduler":
                inputs["steps"] = steps
                if dimensions_explicit:
                    inputs["width"] = width
                    inputs["height"] = height
            elif ctype == "EmptyFlux2LatentImage":
                if dimensions_explicit:
                    inputs["width"] = width
                    inputs["height"] = height
            elif ctype == "SaveImage":
                prefix = body.get("filename_prefix", "Flux2-Klein")
                inputs["filename_prefix"] = prefix
            else:
                for text_key in ("prompt", "text", "query", "content", "message", "messages", "user_prompt", "input", "instruction"):
                    if text_key in inputs and isinstance(inputs.get(text_key), str):
                        inputs[text_key] = prompt
                if dimensions_explicit and "width" in inputs:
                    inputs["width"] = width
                if dimensions_explicit and "height" in inputs:
                    inputs["height"] = height
                if "steps" in inputs:
                    inputs["steps"] = steps
                if requested_fps is not None and "fps" in inputs:
                    inputs["fps"] = requested_fps
                if frame_length is not None and "length" in inputs and (
                    "wan" in ctype.lower() or "video" in ctype.lower()
                ):
                    inputs["length"] = frame_length
                if "seed" in inputs:
                    inputs["seed"] = seed
                if "noise_seed" in inputs:
                    inputs["noise_seed"] = seed

    def _start_workflow_task(workflow_id: Optional[str], body: dict) -> dict:
        wf = registry.resolve(workflow_id)
        if wf is None:
            raise HTTPException(404, detail=f"Workflow not found: {workflow_id or '(default)'}")
        body = _validated_generation_body(body)
        task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        reservation = {
            "id": task_id,
            "task_id": task_id,
            "workflow_id": wf.id,
            "workflow_name": wf.name,
            "status": "reserving",
            "phase": "正在提交",
            "progress_label": "正在提交",
            "progress_percent": 0,
            "started_at_ts": time.time(),
        }
        with _task_lock:
            if current_task and current_task.get("status") in (
                "reserving", "running", "pending", "submitted"
            ):
                raise HTTPException(429, detail="已有任务正在执行，请稍后重试")
            current_task.clear()
            current_task.update(reservation)
            task_records[task_id] = dict(reservation)
            _prune_task_records_locked()

        wf_json_path = wf.folder / "workflow.json"
        try:
            if not wf_json_path.exists():
                raise HTTPException(500, detail="Workflow JSON 文件不存在")
            with open(wf_json_path, "r", encoding="utf-8") as f:
                workflow_data = json.load(f)
            comfy_log_path = config.logs_dir / "comfyui.log"
            try:
                log_offset = comfy_log_path.stat().st_size
            except OSError:
                log_offset = 0
            client = ComfyUIClient(config.comfyui_url, log_path=comfy_log_path)
            _upload_workflow_images(client, workflow_data, body, task_id)
            _inject_workflow_params(workflow_data, body)
            prompt_id = client.queue_prompt(workflow_data)
        except HTTPException:
            with _task_lock:
                task_records.pop(task_id, None)
                if current_task.get("task_id") == task_id:
                    current_task.clear()
            raise
        except Exception as exc:
            print(f"[API] ComfyUI queue failed ({type(exc).__name__}): {exc}")
            with _task_lock:
                task_records.pop(task_id, None)
                if current_task.get("task_id") == task_id:
                    current_task.clear()
            raise HTTPException(502, detail="ComfyUI 暂不可用，请稍后重试") from None

        steps = int(body.get("steps") or 20)
        task_title = _task_title_from_body(body, wf.id)
        prompt_summary = _clean_task_text(body.get("prompt", ""), 220)
        task_info = {
            "id": task_id,
            "task_id": task_id,
            "prompt_id": prompt_id,
            "workflow_id": wf.id,
            "workflow_name": wf.name,
            "prompt_summary": prompt_summary,
            "title": task_title,
            "phase": "排队中",
            "progress_label": "排队中",
            "progress_percent": 0,
            "status": "pending",
            "progress": 0,
            "progress_max": steps,
            "started_at": time.strftime("%H:%M:%S"),
            "started_at_ts": time.time(),
            "log_offset": log_offset,
            "elapsed": 0,
            "elapsed_seconds": 0,
            "outputs": [],
        }
        with _task_lock:
            current_task.clear()
            current_task.update(task_info)
            task_records[task_id] = dict(task_info)

        req_dir = config.requests_dir
        try:
            req_dir.mkdir(parents=True, exist_ok=True)
            request_metadata = {
                "task_id": task_id,
                "workflow_id": wf.id,
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "parameters": {
                    key: body.get(key)
                    for key in ("model", "width", "height", "steps", "seed", "size")
                    if body.get(key) not in (None, "")
                },
            }
            with open(req_dir / f"{task_id}.json", "w", encoding="utf-8") as f:
                json.dump(request_metadata, f, ensure_ascii=False, indent=2)
            _cleanup_request_records(req_dir)
        except OSError as exc:
            print(f"[API] request metadata was not persisted: {exc}")

        def _bg_execute():
            try:
                c = ComfyUIClient(config.comfyui_url, log_path=config.logs_dir / "comfyui.log")
                start = time.time()
                missing_progress_count = 0
                progress_high_water = 0

                while True:
                    prog = c.get_progress(prompt_id, expected_steps=steps, log_offset=log_offset)
                    elapsed = int(time.time() - start)
                    if prog is None:
                        missing_progress_count += 1
                        if missing_progress_count <= 30:
                            progress_high_water = _next_task_progress_percent(progress_high_water, None)
                            _set_task_record(task_id, {
                                "status": "running",
                                "progress": steps,
                                "progress_max": steps,
                                "progress_percent": progress_high_water,
                                "phase": "保存结果",
                                "progress_label": "保存结果",
                                "elapsed": elapsed,
                                "elapsed_seconds": elapsed,
                            })
                            time.sleep(2)
                            continue
                        raise RuntimeError("ComfyUI 任务结束后未返回 history，无法确认输出文件")
                    missing_progress_count = 0
                    progress_high_water = _next_task_progress_percent(progress_high_water, prog)
                    _set_task_record(task_id, {
                        "status": prog["status"],
                        "progress": prog["value"],
                        "progress_max": prog["max"],
                        "progress_percent": progress_high_water,
                        "phase": prog.get("phase", ""),
                        "progress_label": prog.get("label", prog.get("phase", "")),
                        "elapsed": elapsed,
                        "elapsed_seconds": elapsed,
                    })
                    if prog["status"] == "completed":
                        break
                    time.sleep(2)

                history = c.get_history(prompt_id)
                outputs = []
                if prompt_id in history:
                    outputs = c.get_output_files(history[prompt_id])
                outputs = _with_output_urls(task_id, outputs)
                _set_task_record(task_id, {
                    "status": "completed",
                    "outputs": outputs,
                    "elapsed": int(time.time() - start),
                    "elapsed_seconds": int(time.time() - start),
                    "progress": 1,
                    "progress_max": 1,
                    "progress_percent": 100,
                    "phase": "已完成",
                    "progress_label": "已完成",
                })

            except Exception as ex:
                print(f"[API] task {task_id} failed ({type(ex).__name__}): {ex}")
                _set_task_record(task_id, {
                    "status": "failed",
                    "error": "任务执行失败，请查看本地日志",
                })

        threading.Thread(target=_bg_execute, daemon=True).start()
        return _task_api_response(task_info)

    def _wait_for_task(task_id: str, timeout_sec: int = 1800) -> dict:
        started = time.time()
        while time.time() - started < timeout_sec:
            with _task_lock:
                record = dict(task_records.get(task_id, {}))
            status = str(record.get("status", "")).lower()
            if status in ("completed", "succeeded", "success", "done", "ready"):
                return _task_api_response(record)
            if status in ("failed", "error", "cancelled", "canceled"):
                raise HTTPException(502, detail=record.get("error") or "Generation failed")
            time.sleep(1)
        raise HTTPException(504, detail="Local client image generation timed out")

    def _find_output_file(task_id: str, filename: str) -> Optional[Path]:
        safe_name = Path(filename).name
        if not safe_name or safe_name != filename:
            return None
        with _task_lock:
            record = dict(task_records.get(task_id, {}))
        if not record or str(record.get("task_id") or record.get("id") or "") != task_id:
            return None
        candidates = []
        matched = False
        for item in record.get("outputs") or []:
            if Path(str(item.get("filename", ""))).name != safe_name:
                continue
            matched = True
            subfolder = str(item.get("subfolder") or "").strip().replace("\\", "/")
            if subfolder and ".." not in subfolder.split("/"):
                candidates.append(BASE_DIR / "outputs" / subfolder / safe_name)
            elif not subfolder:
                candidates.extend([
                    BASE_DIR / "outputs" / safe_name,
                    BASE_DIR / "outputs" / task_id / safe_name,
                    BASE_DIR / "runtime" / "outputs" / task_id / safe_name,
                ])
        if not matched:
            return None
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if not resolved.is_file():
                    continue
                roots = [
                    (BASE_DIR / "outputs").resolve(),
                    (BASE_DIR / "runtime" / "outputs").resolve(),
                ]
                if any(_path_is_within(resolved, root) for root in roots):
                    return resolved
            except Exception:
                continue
        return None

    # ── 路由 ──
    @app.get("/health")
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": "1.0.0"}

    @app.get("/v1/status")
    def status():
        # ComfyUI 连通性检测
        comfyui_status = "offline"
        comfyui_url = config.comfyui_url
        try:
            import requests as _r
            resp = _r.get(f"{comfyui_url}/system_stats", timeout=3)
            if resp.status_code == 200:
                comfyui_status = "online"
        except Exception:
            pass
        with _comfy_nodes_lock:
            if comfyui_status == "online":
                if (
                    _comfy_nodes_cache["url"] == str(comfyui_url).rstrip("/")
                    and _comfy_nodes_cache["nodes"] is None
                ):
                    _comfy_nodes_cache["checked_at"] = 0.0
            else:
                _comfy_nodes_cache.update(
                    {
                        "url": str(comfyui_url).rstrip("/"),
                        "checked_at": time.monotonic(),
                        "nodes": None,
                    }
                )

        # 运行时环境检查
        runtime_status = _local_runtime_status()

        # 模型完整性检查
        model_status = _local_model_status()
        flux2_ok = model_status["flux2"]
        wan21_ok = model_status["wan21"]
        qwen35_ok = model_status["qwen35"]
        models_all_ok = qwen35_ok and flux2_ok and wan21_ok

        # 当前任务进度
        with _task_lock:
            task_copy = dict(current_task)
        for private_field in ("prompt", "log_offset", "started_at_ts"):
            task_copy.pop(private_field, None)

        model_list = _models_from_workflows()
        model_groups = {"image": [], "video": [], "text": []}
        for item in model_list:
            model_groups.setdefault(item.get("group") or _model_group_for_type(item.get("type", "")), []).append(item)

        public_base_url = _active_public_base_url()
        tunnel_status = _tunnel_health_status()

        return {
            "status": "ok",
            "version": "1.0.0",
            "session_id": state.session_id,
            "base_url": public_base_url,
            "local_api": state.local_api,
            "api": {"status": "online", "port": config.server_port},
            "tunnel": {
                "provider": "cloudflare_quick_tunnel",
                "status": tunnel_status,
                "url": public_base_url,
                "error": tunnel.state.error if tunnel else "",
            },
            "comfyui": {
                "url": comfyui_url,
                "status": comfyui_status,
            },
            "runtime": runtime_status,
            "models": {
                "status": "complete" if models_all_ok else "incomplete",
                "Flux2": "complete" if flux2_ok else "missing",
                "Wan2.1": "complete" if wan21_ok else "missing",
                "Qwen3.5": "complete" if qwen35_ok else "missing",
                "list": model_list,
                "image": model_groups.get("image", []),
                "video": model_groups.get("video", []),
                "text": model_groups.get("text", []),
            },
            "model_list": model_list,
            "modelGroups": model_groups,
            "model_groups": model_groups,
            "workflows": [_workflow_payload(w) for w in registry.workflows],
            "workflow_count": len(registry.workflows),
            "default_workflow": registry.default_workflow_id,
            "current_task": task_copy if task_copy else None,
        }

    @app.get("/v1/models")
    async def list_models():
        models = _models_from_workflows()
        return {
            "models": models,
            "data": models,
            "image": [item for item in models if item.get("group") == "image"],
            "video": [item for item in models if item.get("group") == "video"],
            "text": [item for item in models if item.get("group") == "text"],
        }

    @app.get("/v1/workflows")
    @app.get("/v1/workflows/list")
    async def list_workflows():
        return {
            "workflows": [_workflow_payload(w) for w in registry.workflows],
            "default_workflow": registry.default_workflow_id,
        }

    @app.post("/v1/workflows/reload")
    @app.post("/v1/workflows/rescan")
    async def reload_workflows():
        registry.load()
        new_workflows = registry.scan_folder()
        return {
            "ok": True,
            "workflows": [_workflow_payload(w) for w in registry.workflows],
            "default_workflow": registry.default_workflow_id,
            "new_workflows": [_workflow_payload(w) for w in new_workflows],
        }

    @app.post("/v1/tunnel/restart")
    async def restart_tunnel():
        return await _restart_tunnel_manager()

    @app.post("/v1/workflows/run")
    @app.post("/v1/workflows/run/{workflow_id}")
    async def run_workflow(request: Request, workflow_id: Optional[str] = None):
        """执行工作流 — 非阻塞：提交后立即返回，后台执行"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        task_info = await asyncio.to_thread(_start_workflow_task, workflow_id, body)
        status_path = f"/v1/tasks/{quote(task_info['task_id'], safe='')}"
        return {
            "id": task_info["task_id"],
            "task_id": task_info["task_id"],
            "status": "submitted",
            "workflow": task_info["workflow_id"],
            "workflow_id": task_info["workflow_id"],
            "workflow_name": task_info["workflow_name"],
            "prompt_id": task_info["prompt_id"],
            "status_path": status_path,
            "status_url": f"{_public_base_url()}{status_path}",
            "message": f"任务已提交，通过 /v1/tasks/{task_info['task_id']} 查询进度",
        }

    def _resolve_short_workflow(workflow_alias: Optional[str]):
        requested = str(workflow_alias or "").strip()
        if not requested:
            return registry.resolve(None)

        exact = registry.resolve(requested)
        if exact is not None:
            return exact

        normalized = requested.casefold()
        id_matches = [
            workflow
            for workflow in registry.enabled_workflows
            if str(workflow.id or "").strip().casefold() == normalized
        ]
        if id_matches:
            return id_matches[0]

        name_matches = [
            workflow
            for workflow in registry.enabled_workflows
            if str(workflow.name or "").strip().casefold() == normalized
        ]
        if len(name_matches) > 1:
            raise HTTPException(
                409,
                detail=f"Workflow alias is ambiguous: {requested}. Use the workflow ID instead.",
            )
        return name_matches[0] if name_matches else None

    def _short_workflow_body(workflow, body: dict) -> dict:
        normalized = dict(body)
        output_type = str(getattr(workflow, "output_type", "") or "").lower()
        if output_type == "image":
            width, height = _size_to_dimensions(normalized)
            normalized["width"] = width
            normalized["height"] = height
            normalized.setdefault(
                "filename_prefix",
                f"{workflow.id}-{int(time.time())}",
            )
        return normalized

    @app.post("/")
    @app.post("/{workflow_alias}")
    async def run_short_workflow(
        request: Request,
        workflow_alias: Optional[str] = None,
    ):
        """稳定短 URL：根路径走默认工作流，一级路径按 ID 或名称选工作流。"""
        requested = str(workflow_alias or "").strip()
        workflow = _resolve_short_workflow(requested)
        if workflow is None:
            missing = requested or "(default)"
            raise HTTPException(404, detail=f"Workflow not found: {missing}")

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        submitted = await asyncio.to_thread(
            _start_workflow_task,
            workflow.id,
            _short_workflow_body(workflow, body),
        )
        status_path = f"/v1/tasks/{quote(submitted['task_id'], safe='')}"
        return JSONResponse(
            status_code=202,
            headers={
                "Location": status_path,
                "Retry-After": "3",
                "Preference-Applied": "respond-async",
            },
            content=_workflow_task_submission_payload(
                submitted,
                requested_workflow=requested,
                output_type=str(getattr(workflow, "output_type", "") or ""),
            ),
        )

    def _chat_prompt_from_body(body: dict) -> str:
        def ensure_json_contract(text: str) -> str:
            response_format = body.get("response_format") or body.get("responseFormat") or {}
            wants_json = (
                (isinstance(response_format, dict) and str(response_format.get("type") or "").lower() == "json_object")
                or str(response_format).lower() == "json_object"
            )
            if not wants_json or "STRICT OUTPUT CONTRACT" in text:
                return text
            return (
                f"{text}\n\n"
                "STRICT OUTPUT CONTRACT:\n"
                "- Return exactly one valid JSON object.\n"
                "- Do not wrap JSON in Markdown fences.\n"
                "- Do not add explanations, comments, prefixes, suffixes, or extra text."
            )

        prompt = str(body.get("prompt") or body.get("input") or "").strip()
        if prompt:
            return ensure_json_contract(prompt)
        messages = body.get("messages") or []
        if not isinstance(messages, list):
            return ""
        parts = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user").strip()
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict):
                        text_parts.append(str(item.get("text") or item.get("content") or ""))
                    else:
                        text_parts.append(str(item or ""))
                content = "\n".join(part for part in text_parts if part)
            content = str(content or "").strip()
            if content:
                parts.append(f"{role}: {content}")
        return ensure_json_contract("\n\n".join(parts).strip())

    def _text_workflow_id_from_model(model: str) -> str:
        model = str(model or "").strip()
        if model:
            found = registry.resolve(model)
            if found:
                return found.id
        for wf in registry.workflows:
            if str(getattr(wf, "output_type", "") or "").lower() == "text":
                return wf.id
        for wf in registry.workflows:
            text = f"{wf.id} {wf.name}".lower()
            if "text" in text or "chat" in text or "llm" in text:
                return wf.id
        return model

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = _chat_prompt_from_body(body)
        if not prompt:
            return JSONResponse(status_code=400, content={
                "error": {
                    "code": "prompt_required",
                    "message": "prompt or messages is required",
                }
            })

        workflow_id = _text_workflow_id_from_model(body.get("model") or "")
        if not workflow_id:
            return JSONResponse(status_code=404, content={
                "error": {
                    "code": "text_workflow_not_found",
                    "message": "No text workflow is available.",
                }
            })

        workflow_body = {
            "prompt": prompt,
            "filename_prefix": f"{workflow_id}-{int(time.time())}",
            "response_format": body.get("response_format") or {"type": "json_object"},
        }
        try:
            submitted = await asyncio.to_thread(
                _start_workflow_task, workflow_id, workflow_body
            )
            timeout_sec = _bounded_integer(
                body.get("timeout") or body.get("timeout_sec"),
                name="timeout",
                default=1800,
                minimum=1,
                maximum=3600,
            )
            completed = await asyncio.to_thread(
                _wait_for_task,
                submitted["task_id"],
                max(30, timeout_sec),
            )
            content = _task_text_output(completed)
            if not content:
                return JSONResponse(status_code=502, content={
                    "error": {
                        "code": "local_client_text_output_missing",
                        "message": "Text workflow completed but no text output was found.",
                    },
                    "task_id": submitted["task_id"],
                })
            return {
                "id": submitted["task_id"],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model") or workflow_id,
                "workflow": workflow_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "task_id": submitted["task_id"],
            }
        except HTTPException:
            raise
        except Exception as ex:
            print(f"[API] text response failed ({type(ex).__name__}): {ex}")
            return JSONResponse(status_code=500, content={
                "error": {
                    "code": "local_client_text_generation_failed",
                    "message": "本地文字生成失败，请查看客户端日志。",
                }
            })

    @app.get("/v1/tasks/status")
    async def task_status():
        """获取当前任务执行进度"""
        with _task_lock:
            return _task_api_response(current_task) if current_task else {"status": "idle"}

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str):
        with _task_lock:
            record = dict(task_records.get(task_id, {}))
        if not record:
            raise HTTPException(404, detail="Task not found")
        return _task_api_response(record)

    @app.get("/v1/files/{task_id}/{filename}")
    async def get_file(task_id: str, filename: str):
        file_path = _find_output_file(task_id, filename)
        if not file_path:
            raise HTTPException(404, detail="Output file not found")
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type=_guess_media_type(file_path.name),
        )

    @app.post("/api/v3/images/generations")
    async def ark_compatible_image_generation(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        if body.get("stream") is True:
            return {
                "error": {
                    "code": "stream_not_supported",
                    "message": "Local client image generation does not support stream=true yet.",
                }
            }

        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(status_code=400, content={
                "error": {
                    "code": "prompt_required",
                    "message": "prompt is required",
                }
            })

        response_format = str(body.get("response_format") or "url").strip().lower()
        if response_format not in ("", "url"):
            return JSONResponse(status_code=400, content={
                "error": {
                    "code": "unsupported_response_format",
                    "message": "Only response_format=url is supported.",
                }
            })

        workflow_id = _image_workflow_from_model(body.get("model") or "flux_t2i_v1")
        width, height = _size_to_dimensions(body)
        workflow_body = {
            "prompt": prompt,
            "negative_prompt": body.get("negative_prompt", ""),
            "width": width,
            "height": height,
            "steps": body.get("steps", 20),
            "seed": body.get("seed", 0),
            "filename_prefix": f"{workflow_id}-{int(time.time())}",
        }

        try:
            submitted = await asyncio.to_thread(
                _start_workflow_task, workflow_id, workflow_body
            )
            if _prefers_async_image_response(body, request.headers.get("prefer", "")):
                status_path = f"/v1/tasks/{quote(submitted['task_id'], safe='')}"
                return JSONResponse(
                    status_code=202,
                    headers={
                        "Location": status_path,
                        "Retry-After": "3",
                        "Preference-Applied": "respond-async",
                    },
                    content=_image_task_submission_payload(
                        submitted,
                        str(body.get("model") or workflow_id),
                    ),
                )
            completed = await asyncio.to_thread(
                _wait_for_task,
                submitted["task_id"],
                1800,
            )
            outputs = completed.get("outputs") or []
            if not outputs:
                return JSONResponse(status_code=502, content={
                    "error": {
                        "code": "local_client_output_missing",
                        "message": "Generation completed but no output file was found.",
                    }
                })
            return {
                "created": int(time.time()),
                "data": [
                    {
                        "url": outputs[0]["url"],
                    }
                ],
                "model": body.get("model") or workflow_id,
                "task_id": submitted["task_id"],
            }
        except HTTPException:
            raise
        except Exception as ex:
            print(f"[API] image response failed ({type(ex).__name__}): {ex}")
            return JSONResponse(status_code=500, content={
                "error": {
                    "code": "local_client_generation_failed",
                    "message": "本地图片生成失败，请查看客户端日志。",
                }
            })

    return app


# ── 启动逻辑 ──────────────────────────────────────────
def start_tunnel(cfg):
    global tunnel
    global config
    config = cfg

    tunnel = CloudflaredManager(
        cloudflared_path=cfg.cloudflared_path,
        local_url=f"http://{cfg.server_host}:{cfg.server_port}",
        protocol=cfg.tunnel_protocol,
    )

    def on_url(url: str):
        state.set_online(url)
        print(f"[Tunnel] Online: {url}")

    tunnel.set_on_url(on_url)
    ok = tunnel.start()
    if ok:
        print("[Tunnel] Starting cloudflared...")
    else:
        print(f"[Tunnel] 未启动: {tunnel.state.error}")
    return tunnel


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Local AI API Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-tunnel", action="store_true")
    args = parser.parse_args()

    global config, state
    config = Config(BASE_DIR)
    host = args.host
    port = args.port or config.server_port

    state = RuntimeState(config.runtime_dir)
    state.start_session(port)

    try:
        if not args.no_tunnel:
            start_tunnel(config)

        app = create_app()
        print(f"[API] Listening on http://{host}:{port}")
        print("[API] Authentication is enabled; access keys are not written to logs")
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if tunnel is not None:
            tunnel.stop()
        state.set_offline()


if __name__ == "__main__":
    main()

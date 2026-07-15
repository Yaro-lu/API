"""
Local AI API Gateway — API 服务器

集成 FastAPI + Bearer 鉴权 + Cloudflare Quick Tunnel
端点：
  POST /v1/workflows/run             → 默认工作流
  POST /v1/workflows/run/{wf_id}     → 指定工作流
  GET  /v1/workflows/list            → 工作流列表
  GET  /health                       → 健康检查（免鉴权）
"""
import os
import sys
import json
import random
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

from app.config import Config
from app.core.model_maintenance import MODEL_REQUIREMENTS, check_model_groups
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS, missing_runtime_paths
from app.core.runtime_state import RuntimeState
from app.workflow_registry import WorkflowRegistry
from app.tunnel.cloudflared_manager import CloudflaredManager
from app.engines.comfyui_client import ComfyUIClient

# ── 全局 ──────────────────────────────────────────────
config: Optional[Config] = None
state: Optional[RuntimeState] = None
registry: Optional[WorkflowRegistry] = None
tunnel: Optional[CloudflaredManager] = None

# 当前任务状态（由后台线程写入，/health 读取）
current_task: dict = {}
task_records: dict = {}

PUBLIC_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}

# mutex for current_task
_task_lock = threading.Lock()


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
        workflow_json = getattr(w, "workflow_json", "") or "workflow.json"
        path = getattr(w, "folder", BASE_DIR / "workflows" / getattr(w, "id", "")) / workflow_json
        if not Path(path).exists():
            path = getattr(w, "folder", BASE_DIR / "workflows" / getattr(w, "id", "")) / "workflow.json"
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


def _workflow_payload(w) -> dict:
    workflow_type = _workflow_type(w.id, getattr(w, "output_type", ""))
    if workflow_type == "text_chat" and _workflow_has_image_input(w):
        workflow_type = "text_vision"
    input_schema = _normalize_input_schema(getattr(w, "input_schema", {}) or {}, workflow_type)
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
        available = payload.get("enabled", True) and _workflow_available(model_id, payload.get("type", ""))
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


def _public_base_url() -> str:
    if state and state.base_url:
        return state.base_url.rstrip("/")
    if state and state.local_api:
        return state.local_api.rstrip("/")
    return "http://127.0.0.1:18188"


def _output_url(task_id: str, filename: str) -> str:
    return f"{_public_base_url()}/v1/files/{quote(task_id)}/{quote(filename)}"


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
        url = _output_url(task_id, Path(filename).name)
        normalized.append({
            **item,
            "filename": Path(filename).name,
            "url": url,
            "download_url": url,
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
            if resolved.is_file() and any(str(resolved).startswith(str(root)) for root in roots):
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
        }
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
        if current_task.get("task_id") == task_id:
            current_task.update(record)


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
    return "image/png"


# ── 鉴权中间件 ────────────────────────────────────────
class AuthMiddleware:
    """纯 ASGI 中间件 — Bearer API Key 鉴权"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        auth = ""
        for header in scope.get("headers", []):
            if header[0] == b"authorization":
                auth = header[1].decode()
                break

        if not auth.startswith("Bearer "):
            await self._unauthorized(send, "Missing Authorization header")
            return

        token = auth[7:]
        if token != state.api_key:
            await self._unauthorized(send, "Invalid API key")
            return

        await self.app(scope, receive, send)

    async def _unauthorized(self, send, msg: str):
        body = json.dumps({
            "error": {"code": "unauthorized", "message": msg}
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


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

    app = FastAPI(title="Local AI API Gateway", version="0.2.0", lifespan=lifespan)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
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
                inputs["width"] = width
                inputs["height"] = height
            elif ctype == "EmptyFlux2LatentImage":
                inputs["width"] = width
                inputs["height"] = height
            elif ctype == "SaveImage":
                prefix = body.get("filename_prefix", "Flux2-Klein")
                inputs["filename_prefix"] = prefix
            else:
                for text_key in ("prompt", "text", "query", "content", "message", "messages", "user_prompt", "input", "instruction"):
                    if text_key in inputs and isinstance(inputs.get(text_key), str):
                        inputs[text_key] = prompt
                if "width" in inputs:
                    inputs["width"] = width
                if "height" in inputs:
                    inputs["height"] = height
                if "steps" in inputs:
                    inputs["steps"] = steps
                if "seed" in inputs:
                    inputs["seed"] = seed
                if "noise_seed" in inputs:
                    inputs["noise_seed"] = seed

    def _start_workflow_task(workflow_id: Optional[str], body: dict) -> dict:
        wf = registry.resolve(workflow_id)
        if wf is None:
            raise HTTPException(404, detail=f"Workflow not found: {workflow_id or '(default)'}")

        with _task_lock:
            if current_task and current_task.get("status") in ("running", "pending", "submitted"):
                raise HTTPException(429, detail="已有任务正在执行，请稍后重试")

        task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        wf_json_path = wf.folder / "workflow.json"
        if not wf_json_path.exists():
            raise HTTPException(500, detail="Workflow JSON 文件不存在")

        with open(wf_json_path, "r", encoding="utf-8") as f:
            workflow_data = json.load(f)

        _inject_workflow_params(workflow_data, body)

        req_dir = config.requests_dir
        with open(req_dir / f"{task_id}.json", "w", encoding="utf-8") as f:
            json.dump({
                "task_id": task_id,
                "workflow_id": wf.id,
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "body": body,
            }, f, ensure_ascii=False, indent=2)

        comfy_log_path = config.logs_dir / "comfyui.log"
        try:
            log_offset = comfy_log_path.stat().st_size
        except Exception:
            log_offset = 0
        client = ComfyUIClient(config.comfyui_url, log_path=comfy_log_path)
        try:
            prompt_id = client.queue_prompt(workflow_data)
        except Exception as e:
            raise HTTPException(502, detail=f"ComfyUI 连接失败: {e}")

        steps = int(body.get("steps") or 20)
        task_title = _task_title_from_body(body, wf.id)
        prompt_summary = _clean_task_text(body.get("prompt", ""), 220)
        task_info = {
            "id": task_id,
            "task_id": task_id,
            "prompt_id": prompt_id,
            "workflow_id": wf.id,
            "workflow_name": wf.name,
            "prompt": body.get("prompt", ""),
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
                _set_task_record(task_id, {
                    "status": "failed",
                    "error": str(ex),
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
        candidates = []
        for item in record.get("outputs") or []:
            if Path(str(item.get("filename", ""))).name != safe_name:
                continue
            subfolder = str(item.get("subfolder") or "").strip().replace("\\", "/")
            if subfolder and ".." not in subfolder.split("/"):
                candidates.append(BASE_DIR / "outputs" / subfolder / safe_name)
        candidates.extend([
            BASE_DIR / "outputs" / safe_name,
            BASE_DIR / "outputs" / task_id / safe_name,
            BASE_DIR / "runtime" / "outputs" / task_id / safe_name,
        ])
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if not resolved.is_file():
                    continue
                roots = [
                    (BASE_DIR / "outputs").resolve(),
                    (BASE_DIR / "runtime" / "outputs").resolve(),
                ]
                if any(str(resolved).startswith(str(root)) for root in roots):
                    return resolved
            except Exception:
                continue
        return None

    # ── 路由 ──
    @app.get("/health")
    async def health():
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

        model_list = _models_from_workflows()
        model_groups = {"image": [], "video": [], "text": []}
        for item in model_list:
            model_groups.setdefault(item.get("group") or _model_group_for_type(item.get("type", "")), []).append(item)

        return {
            "status": "ok",
            "version": "0.2.0",
            "session_id": state.session_id,
            "base_url": state.base_url,
            "local_api": state.local_api,
            "api": {"status": "online", "port": config.server_port},
            "tunnel": {
                "provider": "cloudflare_quick_tunnel",
                "status": "online" if (tunnel and tunnel.is_online) else (
                    "unavailable" if (tunnel and tunnel.state.status == "failed") else "offline"
                ),
                "url": state.base_url,
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
        global tunnel
        if tunnel is None:
            start_tunnel(config)
        else:
            tunnel.restart()
        return {
            "ok": bool(tunnel and tunnel.state.status in ("starting", "retrying", "online")),
            "status": tunnel.state.status if tunnel else "offline",
            "url": tunnel.state.base_url if tunnel else "",
            "error": tunnel.state.error if tunnel else "Tunnel manager not initialized",
        }

    @app.post("/v1/workflows/run")
    @app.post("/v1/workflows/run/{workflow_id}")
    async def run_workflow(request: Request, workflow_id: Optional[str] = None):
        """执行工作流 — 非阻塞：提交后立即返回，后台执行"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        task_info = _start_workflow_task(workflow_id, body)
        return {
            "id": task_info["task_id"],
            "task_id": task_info["task_id"],
            "status": "submitted",
            "workflow": task_info["workflow_id"],
            "workflow_id": task_info["workflow_id"],
            "workflow_name": task_info["workflow_name"],
            "prompt_id": task_info["prompt_id"],
            "message": f"任务已提交，通过 /health 查看进度",
        }

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
            submitted = _start_workflow_task(workflow_id, workflow_body)
            timeout_sec = int(body.get("timeout") or body.get("timeout_sec") or 1800)
            completed = _wait_for_task(submitted["task_id"], timeout_sec=max(30, timeout_sec))
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
            return JSONResponse(status_code=500, content={
                "error": {
                    "code": "local_client_text_generation_failed",
                    "message": str(ex),
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
            submitted = _start_workflow_task(workflow_id, workflow_body)
            completed = _wait_for_task(submitted["task_id"], timeout_sec=1800)
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
            return JSONResponse(status_code=500, content={
                "error": {
                    "code": "local_client_generation_failed",
                    "message": str(ex),
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
        print(f"[API] API Key: {state.api_key}")
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if tunnel is not None:
            tunnel.stop()
        state.set_offline()


if __name__ == "__main__":
    main()

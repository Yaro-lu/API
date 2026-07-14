from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path
import uvicorn
import threading
import time
from typing import Optional
import os

from app.worker_service.config import Config
from app.worker_service.log_manager import LogManager
from app.worker_service.gpu_check import GPUChecker
from app.worker_service.torch_check import TorchChecker
from app.worker_service.comfy_manager import ComfyUIManager
from app.worker_service.model_manager import ModelManager
from app.worker_service.comfy_client import ComfyUIClient
from app.worker_service.task_runner import TaskRunner
from app.worker_service.repair import RepairManager
from app.worker_service.server_client import ServerClient
from app.worker_service.task_poller import TaskPoller
from app.worker_service.schemas import (
    HealthResponse,
    StatusResponse,
    ClientStatus,
    LocalVideoFLF2VRequest,
    LocalImageT2IRequest,
    TaskResponse,
    TaskDetailResponse,
    TaskListResponse,
    TaskStatus,
    ModelInfo,
    GPUInfo,
    TorchInfo,
    ComfyUIBridgeImageT2IRequest
)


app = FastAPI(title="AI Worker Local API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = Config()
log_manager = LogManager(config.logs_dir)
logger = log_manager.get_logger()

gpu_checker = GPUChecker()
torch_checker = TorchChecker(config.python_path)
comfy_manager = ComfyUIManager(
    config.comfyui_path,
    config.python_path,
    config.comfyui_port,
    config.models_dir,
    log_manager.get_logger("comfyui")
)
model_manager = ModelManager(
    config.models_dir,
    config.base_dir / "config" / "model_manifest.yaml",
    logger
)
comfy_client = ComfyUIClient(f"http://127.0.0.1:{config.comfyui_port}", logger)
task_runner = TaskRunner(
    comfy_client,
    model_manager,
    config.comfyui_path,
    config.outputs_dir,
    logger
)
repair_manager = RepairManager(config.base_dir, logger)

server_client: Optional[ServerClient] = None
task_poller: Optional[TaskPoller] = None
server_connected = False


@app.on_event("startup")
async def startup_event():
    task_runner.start_worker()
    logger.info("AI Worker API started")


@app.on_event("shutdown")
async def shutdown_event():
    task_runner.stop_worker()
    if task_poller:
        task_poller.stop()
    logger.info("AI Worker API stopped")


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        version=config.get("client.version", "1.0.0")
    )


@app.get("/status", response_model=StatusResponse)
async def status():
    gpu_result = gpu_checker.check()
    torch_result = torch_checker.check()
    comfyui_running = comfy_manager.is_running()

    client_status = ClientStatus.READY
    if not gpu_result.success or not torch_result.success:
        client_status = ClientStatus.ERROR

    tasks = task_runner.get_all_tasks()
    if any(t.status in [TaskStatus.RUNNING, TaskStatus.PENDING] for t in tasks):
        client_status = ClientStatus.BUSY

    gpu_info = None
    if gpu_result.success:
        gpu_info = GPUInfo(
            name=gpu_result.gpu_name,
            vram_gb=gpu_result.vram_gb,
            driver=gpu_result.driver_version
        )

    torch_info = None
    if torch_result.success:
        torch_info = TorchInfo(
            cuda_available=torch_result.cuda_available,
            version=torch_result.torch_version,
            cuda=torch_result.cuda_version
        )

    all_models = model_manager.get_all_models()
    models = [
        ModelInfo(
            id=m.id,
            name=m.name,
            type=m.type,
            available=model_manager.is_model_available(m.id)
        )
        for m in all_models
    ]

    return StatusResponse(
        client_status=client_status,
        server_connected=server_connected,
        comfyui_running=comfyui_running,
        gpu=gpu_info,
        torch=torch_info,
        models=models
    )


@app.post("/repair")
async def repair(mode: str = "quick"):
    if mode == "full":
        success = repair_manager.full_repair()
    else:
        success = repair_manager.quick_repair()

    if success:
        return {"status": "success", "message": "Repair completed"}
    else:
        raise HTTPException(status_code=500, detail="Repair failed")


@app.post("/tasks/local/image-t2i", response_model=TaskResponse)
async def submit_image_t2i(request: LocalImageT2IRequest):
    try:
        model_config = model_manager.get_model_config(request.model)
        if not model_config:
            raise HTTPException(status_code=404, detail=f"Model {request.model} not found")

        if not model_manager.is_model_available(request.model):
            raise HTTPException(status_code=400, detail=f"Model {request.model} not available")

        task = task_runner.submit_image_t2i(request.dict())

        return TaskResponse(
            task_id=task.task_id,
            status=TaskStatus.PENDING,
            message="Task submitted successfully"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit image t2i task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/local/video-flf2v", response_model=TaskResponse)
async def submit_video_flf2v(request: LocalVideoFLF2VRequest):
    try:
        model_config = model_manager.get_model_config(request.model)
        if not model_config:
            raise HTTPException(status_code=404, detail=f"Model {request.model} not found")

        if not model_manager.is_model_available(request.model):
            raise HTTPException(status_code=400, detail=f"Model {request.model} not available")

        task = task_runner.submit_video_flf2v(request.dict())

        return TaskResponse(
            task_id=task.task_id,
            status=TaskStatus.PENDING,
            message="Task submitted successfully"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit video flf2v task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tasks", response_model=TaskListResponse)
async def list_tasks():
    tasks = task_runner.get_all_tasks()
    return TaskListResponse(
        tasks=[
            TaskDetailResponse(
                task_id=t.task_id,
                type=t.type,
                status=t.status,
                progress=t.progress,
                created_at=t.created_at,
                started_at=t.started_at,
                completed_at=t.completed_at,
                input=t.input,
                output_file=t.output_file,
                error=t.error
            )
            for t in tasks
        ]
    )


@app.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task(task_id: str):
    task = task_runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskDetailResponse(
        task_id=task.task_id,
        type=task.type,
        status=task.status,
        progress=task.progress,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        input=task.input,
        output_file=task.output_file,
        error=task.error
    )


@app.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: str):
    success = task_runner.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")

    task = task_runner.get_task(task_id)
    return TaskResponse(
        task_id=task_id,
        status=TaskStatus.CANCELLED,
        message="Task cancelled"
    )


@app.get("/tasks/{task_id}/output")
async def download_task_output(task_id: str):
    task = task_runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Task not completed")

    if not task.output_file:
        raise HTTPException(status_code=404, detail="Output file not found")

    output_path = Path(task.output_file)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Output file does not exist")

    return FileResponse(
        path=str(output_path),
        filename=output_path.name,
        media_type="application/octet-stream"
    )


@app.post("/comfyui/start")
async def start_comfyui(lowvram: bool = False):
    try:
        success = comfy_manager.start(lowvram=lowvram)
        if success:
            return {"status": "success", "message": "ComfyUI started"}
        else:
            raise HTTPException(status_code=500, detail="Failed to start ComfyUI")
    except Exception as e:
        logger.error(f"Failed to start ComfyUI: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/comfyui/stop")
async def stop_comfyui():
    try:
        comfy_manager.stop()
        return {"status": "success", "message": "ComfyUI stopped"}
    except Exception as e:
        logger.error(f"Failed to stop ComfyUI: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models")
async def list_models():
    try:
        models = model_manager.get_all_models()
        return {
            "status": "success",
            "models": [
                {
                    "id": m.id,
                    "name": m.name,
                    "type": m.type,
                    "description": m.description,
                    "tags": m.tags,
                    "author": m.author,
                    "version": m.version,
                    "min_vram_gb": m.min_vram_gb,
                    "recommended_vram_gb": m.recommended_vram_gb,
                    "available": model_manager.is_model_available(m.id)
                }
                for m in models
            ]
        }
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models/{model_id}")
async def get_model(model_id: str):
    try:
        config = model_manager.get_model_config(model_id)
        if not config:
            raise HTTPException(status_code=404, detail="Model not found")

        status = model_manager.get_model_status(model_id)
        return {
            "status": "success",
            "model": {
                "id": config.id,
                "name": config.name,
                "type": config.type,
                "description": config.description,
                "tags": config.tags,
                "author": config.author,
                "version": config.version,
                "min_vram_gb": config.min_vram_gb,
                "recommended_vram_gb": config.recommended_vram_gb,
                "workflow": config.workflow,
                "defaults": config.defaults,
                "required_files": [
                    {
                        "path": f.path,
                        "required": f.required,
                        "url": f.url,
                        "size_bytes": f.size_bytes
                    }
                    for f in config.required_files
                ]
            },
            "model_status": status
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get model: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models/{model_id}/status")
async def get_model_status(model_id: str):
    try:
        status = model_manager.get_model_status(model_id)
        if "error" in status:
            raise HTTPException(status_code=404, detail=status["error"])
        return {"status": "success", "data": status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get model status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/models/{model_id}/download")
async def download_model(model_id: str):
    try:
        task_id = model_manager.download_model(model_id)
        return {
            "status": "success",
            "message": "Download started",
            "task_id": task_id
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to start download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/models/{model_id}")
async def delete_model(model_id: str):
    try:
        success = model_manager.delete_model_files(model_id)
        if success:
            return {"status": "success", "message": "Model files deleted"}
        else:
            raise HTTPException(status_code=404, detail="Model not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete model: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/downloads")
async def list_downloads():
    try:
        downloads = model_manager.get_all_downloads()
        return {"status": "success", "downloads": downloads}
    except Exception as e:
        logger.error(f"Failed to list downloads: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/downloads/{task_id}")
async def get_download_status(task_id: str):
    try:
        task = model_manager.get_download_status(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Download task not found")
        return {
            "status": "success",
            "task": {
                "model_id": task.model_id,
                "file_path": task.file_path,
                "status": task.status.value,
                "progress": task.progress,
                "downloaded_bytes": task.downloaded_bytes,
                "total_bytes": task.total_bytes,
                "error": task.error
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get download status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/downloads/{task_id}/pause")
async def pause_download(task_id: str):
    try:
        model_manager.pause_download(task_id)
        return {"status": "success", "message": "Download paused"}
    except Exception as e:
        logger.error(f"Failed to pause download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/downloads/{task_id}/resume")
async def resume_download(task_id: str):
    try:
        model_manager.resume_download(task_id)
        return {"status": "success", "message": "Download resumed"}
    except Exception as e:
        logger.error(f"Failed to resume download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/downloads/{task_id}/cancel")
async def cancel_download(task_id: str):
    try:
        model_manager.cancel_download(task_id)
        return {"status": "success", "message": "Download cancelled"}
    except Exception as e:
        logger.error(f"Failed to cancel download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/storage")
async def get_storage_info():
    try:
        info = model_manager.get_storage_info()
        return {"status": "success", "storage": info}
    except Exception as e:
        logger.error(f"Failed to get storage info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/server/connect")
async def connect_to_server(base_url: str, token: str = ""):
    global server_client, task_poller, server_connected
    try:
        server_client = ServerClient(base_url, token, logger)

        client_id = config.get("client.id", "")
        if not client_id:
            import uuid
            client_id = str(uuid.uuid4())

        gpu_result = gpu_checker.check()
        torch_result = torch_checker.check()

        gpu_info = {
            "name": gpu_result.gpu_name,
            "vram_gb": gpu_result.vram_gb,
            "driver_version": gpu_result.driver_version
        } if gpu_result.success else None

        runtime_info = {
            "torch_version": torch_result.torch_version,
            "cuda_available": torch_result.cuda_available,
            "cuda_version": torch_result.cuda_version
        } if torch_result.success else None

        models = model_manager.get_available_models()

        registered = server_client.register(client_id, config.get("client.version", "1.0.0"), gpu_info, runtime_info, models)

        if registered:
            task_poller = TaskPoller(
                server_client,
                task_runner,
                comfy_manager,
                config,
                log_manager,
                config.get("task.poll_interval_sec", 5)
            )
            task_poller.start()
            server_connected = True
            logger.info(f"Connected to server: {base_url}")
            return {"status": "success", "message": "Connected to server", "client_id": client_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to register with server")

    except Exception as e:
        logger.error(f"Failed to connect to server: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/server/disconnect")
async def disconnect_from_server():
    global server_client, task_poller, server_connected
    try:
        if task_poller:
            task_poller.stop()
            task_poller = None

        server_client = None
        server_connected = False
        logger.info("Disconnected from server")
        return {"status": "success", "message": "Disconnected from server"}
    except Exception as e:
        logger.error(f"Failed to disconnect: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/server/status")
async def get_server_status():
    return {
        "connected": server_connected,
        "base_url": config.get("server.base_url", ""),
        "poller_state": task_poller.state.value if task_poller else "stopped",
        "current_task": task_poller.current_task if task_poller else None
    }


# ========== ComfyUI Bridge（外接已运行的 ComfyUI）==========

import requests as http_requests
import json as _json

@app.post("/bridge/comfyui/image-t2i", response_model=TaskResponse)
async def bridge_comfyui_image_t2i(request: ComfyUIBridgeImageT2IRequest):
    """对接外部已运行的 ComfyUI，直接提交文生图任务"""
    comfyui_url = request.comfyui_url.rstrip("/")

    # 1. 加载 workflow
    workflows_dir = config.base_dir / "workflows"
    workflow_path = workflows_dir / request.workflow_name
    if not workflow_path.exists():
        raise HTTPException(status_code=404, detail=f"Workflow not found: {request.workflow_name}")

    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow = _json.load(f)

    # 2. 替换参数
    seed = request.seed if request.seed != -1 else int(time.time())
    for node_id, node in workflow.items():
        inputs = node.get("inputs", {})
        title = node.get("_meta", {}).get("title", "")

        if "text" in inputs:
            if "Negative" in title or "negative" in title.lower():
                inputs["text"] = request.negative_prompt
            elif isinstance(inputs["text"], str):
                inputs["text"] = request.prompt
        if "width" in inputs:
            inputs["width"] = request.width
        if "height" in inputs:
            inputs["height"] = request.height
        if "steps" in inputs:
            inputs["steps"] = request.steps
        if "cfg" in inputs:
            inputs["cfg"] = request.cfg
        if "seed" in inputs:
            inputs["seed"] = seed
        if "noise_seed" in inputs:
            inputs["noise_seed"] = seed

    # 3. 提交到外部 ComfyUI
    try:
        resp = http_requests.post(
            f"{comfyui_url}/prompt",
            json={"prompt": workflow},
            timeout=10
        )
        resp.raise_for_status()
        prompt_id = resp.json()["prompt_id"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI submit failed: {e}")

    # 4. 等待完成
    start = time.time()
    timeout_sec = 600
    while time.time() - start < timeout_sec:
        try:
            hist = http_requests.get(f"{comfyui_url}/history/{prompt_id}", timeout=10).json()
        except Exception:
            time.sleep(2)
            continue
        if prompt_id in hist:
            break
        time.sleep(2)
    else:
        raise HTTPException(status_code=504, detail="ComfyUI generation timeout")

    # 5. 提取输出文件
    outputs = hist[prompt_id].get("outputs", {})
    files = []
    for node_output in outputs.values():
        for img in node_output.get("images", []):
            files.append({
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
                "view_url": f"{comfyui_url}/view?filename={img['filename']}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'output')}"
            })

    if not files:
        raise HTTPException(status_code=500, detail="No output files from ComfyUI")

    return TaskResponse(
        task_id=prompt_id,
        status=TaskStatus.COMPLETED,
        progress=1.0,
        message="Generation completed",
        output_file=files[0]["view_url"]
    )


if __name__ == "__main__":
    logger.info(f"Starting AI Worker on 127.0.0.1:{config.local_api_port}")
    logger.info(f"API docs available at: http://127.0.0.1:{config.local_api_port}/docs")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=config.local_api_port
    )

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ClientStatus(str, Enum):
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"


class GPUInfo(BaseModel):
    name: str
    vram_gb: int
    driver: str
    cuda_driver_version: Optional[str] = None


class TorchInfo(BaseModel):
    cuda_available: bool
    version: str
    cuda: Optional[str] = None


class ModelInfo(BaseModel):
    id: str
    name: str
    type: str
    available: bool


class HealthResponse(BaseModel):
    status: str
    version: str


class StatusResponse(BaseModel):
    client_status: ClientStatus
    server_connected: bool
    comfyui_running: bool
    gpu: Optional[GPUInfo] = None
    torch: Optional[TorchInfo] = None
    models: List[ModelInfo]


class LocalVideoFLF2VRequest(BaseModel):
    model: str
    prompt: str
    first_frame: str
    last_frame: str
    width: int = 768
    height: int = 432
    frames: int = 49
    steps: int = 20
    cfg: float = 3.0
    shift: float = 8.0
    seed: int = -1


class LocalImageT2IRequest(BaseModel):
    model: str
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 20
    cfg: float = 5.0
    seed: int = -1


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: Optional[float] = None
    message: Optional[str] = None
    output_file: Optional[str] = None
    error: Optional[str] = None


class TaskDetailResponse(BaseModel):
    task_id: str
    type: str
    status: TaskStatus
    progress: float
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    input: Dict[str, Any]
    output_file: Optional[str] = None
    error: Optional[str] = None


class TaskListResponse(BaseModel):
    tasks: List[TaskDetailResponse]


class ComfyUIBridgeImageT2IRequest(BaseModel):
    """外接 ComfyUI 文生图请求"""
    comfyui_url: str = Field(..., description="ComfyUI 地址，如 http://127.0.0.1:8188")
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 20
    cfg: float = 5.0
    seed: int = -1
    workflow_name: str = Field(default="image_flux2_text_to_image_9b_api.json", description="workflow 文件名（在 workflows/ 目录下）")

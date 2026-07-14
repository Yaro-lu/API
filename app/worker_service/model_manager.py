import yaml
import hashlib
import requests
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum
import logging


class DownloadStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class ModelFile:
    path: str
    required: bool
    sha256: Optional[str] = None
    url: Optional[str] = None
    size_bytes: Optional[int] = None


@dataclass
class ModelConfig:
    id: str
    name: str
    type: str
    min_vram_gb: int
    recommended_vram_gb: int
    workflow: str
    required_files: List[ModelFile]
    defaults: Dict
    description: str = ""
    tags: List[str] = None
    author: str = ""
    version: str = "1.0"


@dataclass
class DownloadTask:
    model_id: str
    file_path: str
    url: str
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None


class ModelManager:
    def __init__(self, models_dir: Path, manifest_path: Path, logger: Optional[logging.Logger] = None):
        self.models_dir = models_dir
        self.manifest_path = manifest_path
        self._manifest = self._load_manifest()
        self.logger = logger or logging.getLogger(__name__)
        self._download_tasks: Dict[str, DownloadTask] = {}
        self._download_threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._progress_callbacks: List[Callable] = []

    def _load_manifest(self) -> Dict:
        if not self.manifest_path.exists():
            return {}
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _save_manifest(self):
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(self._manifest, f, allow_unicode=True, default_flow_style=False)

    def get_model_config(self, model_id: str) -> Optional[ModelConfig]:
        models = self._manifest.get("models", {})
        if model_id not in models:
            return None

        data = models[model_id]
        return ModelConfig(
            id=model_id,
            name=data["name"],
            type=data["type"],
            min_vram_gb=data["min_vram_gb"],
            recommended_vram_gb=data["recommended_vram_gb"],
            workflow=data["workflow"],
            required_files=[ModelFile(**f) for f in data["required_files"]],
            defaults=data["defaults"],
            description=data.get("description", ""),
            tags=data.get("tags", []),
            author=data.get("author", ""),
            version=data.get("version", "1.0")
        )

    def get_all_models(self) -> List[ModelConfig]:
        models = self._manifest.get("models", {})
        return [self.get_model_config(model_id) for model_id in models.keys()]

    def is_model_available(self, model_id: str, verify_hash: bool = False) -> bool:
        config = self.get_model_config(model_id)
        if not config:
            return False

        for file in config.required_files:
            file_path = self.models_dir / file.path
            if file.required and not file_path.exists():
                return False
            if verify_hash and file.sha256:
                if self._compute_sha256(file_path) != file.sha256:
                    return False
        return True

    def get_available_models(self) -> List[Dict]:
        models = self._manifest.get("models", {})
        result = []
        for model_id in models:
            config = self.get_model_config(model_id)
            result.append({
                "id": model_id,
                "name": config.name,
                "type": config.type,
                "available": self.is_model_available(model_id),
                "min_vram_gb": config.min_vram_gb,
                "recommended_vram_gb": config.recommended_vram_gb
            })
        return result

    def get_workflow_path(self, model_id: str) -> Optional[Path]:
        config = self.get_model_config(model_id)
        if not config:
            return None
        return Path(__file__).parent.parent.parent / config.workflow

    def set_models_dir(self, new_path: Path):
        self.models_dir = new_path

    def _compute_sha256(self, file_path: Path) -> str:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(8192), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def add_model(self, config: ModelConfig):
        if "models" not in self._manifest:
            self._manifest["models"] = {}
        
        model_data = {
            "name": config.name,
            "type": config.type,
            "min_vram_gb": config.min_vram_gb,
            "recommended_vram_gb": config.recommended_vram_gb,
            "workflow": config.workflow,
            "required_files": [
                {
                    "path": f.path,
                    "required": f.required,
                    "sha256": f.sha256,
                    "url": f.url,
                    "size_bytes": f.size_bytes
                }
                for f in config.required_files
            ],
            "defaults": config.defaults,
            "description": config.description,
            "tags": config.tags or [],
            "author": config.author,
            "version": config.version
        }
        
        self._manifest["models"][config.id] = model_data
        self._save_manifest()

    def remove_model(self, model_id: str):
        if "models" in self._manifest and model_id in self._manifest["models"]:
            del self._manifest["models"][model_id]
            self._save_manifest()

    def get_model_file_path(self, model_id: str, file_path: str) -> Path:
        return self.models_dir / file_path

    def get_model_status(self, model_id: str) -> Dict:
        config = self.get_model_config(model_id)
        if not config:
            return {"error": "Model not found"}
        
        files_status = []
        all_available = True
        
        for file in config.required_files:
            file_path = self.models_dir / file.path
            exists = file_path.exists()
            file_info = {
                "path": file.path,
                "exists": exists,
                "required": file.required,
                "size_bytes": file_path.stat().st_size if exists else 0,
                "hash_match": None
            }
            
            if exists and file.sha256:
                file_info["hash_match"] = self._compute_sha256(file_path) == file.sha256
            
            if file.required and not exists:
                all_available = False
            
            files_status.append(file_info)
        
        return {
            "model_id": model_id,
            "name": config.name,
            "available": all_available,
            "files": files_status
        }

    def download_model(self, model_id: str, callback: Optional[Callable] = None) -> str:
        config = self.get_model_config(model_id)
        if not config:
            raise ValueError(f"Model {model_id} not found")
        
        task_id = f"{model_id}_{int(time.time())}"
        self._stop_events[task_id] = threading.Event()
        
        thread = threading.Thread(
            target=self._download_model_thread,
            args=(model_id, task_id, callback),
            daemon=True
        )
        thread.start()
        self._download_threads[task_id] = thread
        
        return task_id

    def _download_model_thread(self, model_id: str, task_id: str, callback: Optional[Callable]):
        config = self.get_model_config(model_id)
        if not config:
            return
        
        stop_event = self._stop_events.get(task_id)
        
        try:
            for file in config.required_files:
                if not file.url:
                    continue
                
                file_path = self.models_dir / file.path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                
                download_task = DownloadTask(
                    model_id=model_id,
                    file_path=file.path,
                    url=file.url
                )
                self._download_tasks[f"{task_id}_{file.path}"] = download_task
                
                self._download_file(file, file_path, download_task, stop_event, callback)
                
            if callback:
                callback({"type": "completed", "model_id": model_id, "task_id": task_id})
                
        except Exception as e:
            self.logger.error(f"Download failed: {e}")
            if callback:
                callback({"type": "error", "model_id": model_id, "task_id": task_id, "error": str(e)})
        finally:
            self._cleanup_task(task_id)

    def _download_file(self, file: ModelFile, file_path: Path, task: DownloadTask, 
                      stop_event: Optional[threading.Event], callback: Optional[Callable]):
        task.status = DownloadStatus.DOWNLOADING
        task.start_time = time.time()
        
        try:
            headers = {}
            
            # 支持断点续传
            if file_path.exists():
                downloaded_bytes = file_path.stat().st_size
                if file.size_bytes and downloaded_bytes < file.size_bytes:
                    headers["Range"] = f"bytes={downloaded_bytes}-"
                    task.downloaded_bytes = downloaded_bytes
            
            response = requests.get(file.url, headers=headers, stream=True, timeout=30)
            response.raise_for_status()
            
            # 获取文件大小
            if file.size_bytes is None:
                task.total_bytes = int(response.headers.get("content-length", 0))
            else:
                task.total_bytes = file.size_bytes
            
            mode = "ab" if "Range" in headers else "wb"
            with open(file_path, mode) as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if stop_event and stop_event.is_set():
                        task.status = DownloadStatus.PAUSED
                        return
                    
                    if chunk:
                        f.write(chunk)
                        task.downloaded_bytes += len(chunk)
                        task.progress = (task.downloaded_bytes / task.total_bytes * 100 
                                       if task.total_bytes > 0 else 0)
                        
                        if callback:
                            callback({
                                "type": "progress",
                                "model_id": task.model_id,
                                "file_path": task.file_path,
                                "progress": task.progress,
                                "downloaded_bytes": task.downloaded_bytes,
                                "total_bytes": task.total_bytes
                            })
            
            # 验证哈希
            if file.sha256:
                computed_hash = self._compute_sha256(file_path)
                if computed_hash != file.sha256:
                    raise ValueError(f"Hash mismatch for {file.path}")
            
            task.status = DownloadStatus.COMPLETED
            task.end_time = time.time()
            
        except Exception as e:
            task.status = DownloadStatus.FAILED
            task.error = str(e)
            raise

    def get_download_status(self, task_id: str) -> Optional[DownloadTask]:
        for key, task in self._download_tasks.items():
            if key.startswith(task_id):
                return task
        return None

    def get_all_downloads(self) -> List[Dict]:
        return [
            {
                "model_id": task.model_id,
                "file_path": task.file_path,
                "status": task.status.value,
                "progress": task.progress,
                "downloaded_bytes": task.downloaded_bytes,
                "total_bytes": task.total_bytes,
                "error": task.error
            }
            for task in self._download_tasks.values()
        ]

    def pause_download(self, task_id: str):
        if task_id in self._stop_events:
            self._stop_events[task_id].set()

    def resume_download(self, task_id: str, callback: Optional[Callable] = None):
        if task_id in self._stop_events:
            del self._stop_events[task_id]
        # 重新启动下载
        for key, task in self._download_tasks.items():
            if key.startswith(task_id):
                self.download_model(task.model_id, callback)
                break

    def cancel_download(self, task_id: str):
        if task_id in self._stop_events:
            self._stop_events[task_id].set()
        self._cleanup_task(task_id)

    def _cleanup_task(self, task_id: str):
        keys_to_delete = [k for k in self._download_tasks.keys() if k.startswith(task_id)]
        for key in keys_to_delete:
            del self._download_tasks[key]
        if task_id in self._stop_events:
            del self._stop_events[task_id]
        if task_id in self._download_threads:
            del self._download_threads[task_id]

    def delete_model_files(self, model_id: str) -> bool:
        config = self.get_model_config(model_id)
        if not config:
            return False
        
        for file in config.required_files:
            file_path = self.models_dir / file.path
            if file_path.exists():
                file_path.unlink()
        
        return True

    def get_storage_info(self) -> Dict:
        total_size = 0
        file_count = 0
        
        if self.models_dir.exists():
            for item in self.models_dir.rglob("*"):
                if item.is_file():
                    total_size += item.stat().st_size
                    file_count += 1
        
        return {
            "models_dir": str(self.models_dir),
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "total_size_gb": total_size / (1024 * 1024 * 1024),
            "file_count": file_count
        }

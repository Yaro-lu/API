"""
任务队列 — 异步视频/图片生成任务管理
"""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from app.core.task_store import TaskStore
from app.core.file_store import FileStore


STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_RUNNING = "running"
STATUS_SAVING = "saving"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUSES = [
    STATUS_QUEUED,
    STATUS_DOWNLOADING,
    STATUS_RUNNING,
    STATUS_SAVING,
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
]


@dataclass
class TaskDef:
    task_id: str
    task_type: str          # "video.first_last_to_video" | "image.text_to_image"
    status: str = STATUS_QUEUED
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    model: str = ""
    input: Dict[str, Any] = field(default_factory=dict)
    output_file: Optional[str] = None
    error: Optional[str] = None
    _cancel_flag: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "type": self.task_type,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "model": self.model,
            "output_file": self.output_file,
            "error": self.error,
        }

    def to_api_response(self, base_url: str = "") -> dict:
        """API 响应格式（文档第 15 节）"""
        resp: dict = {
            "id": self.task_id,
            "object": "task",
            "status": self.status,
            "progress": int(self.progress * 100),
            "created": int(self.created_at),
            "updated": int(self.updated_at),
        }

        if self.status == STATUS_SUCCEEDED and self.output_file:
            fname = Path(self.output_file).name
            resp["output"] = {
                "type": "video",
                "url": f"{base_url}/v1/files/{self.task_id}/{fname}",
            }
        elif self.status == STATUS_FAILED:
            resp["error"] = {
                "code": "generation_failed",
                "message": self.error or "Generation failed",
            }
        elif self.status in (STATUS_RUNNING, STATUS_DOWNLOADING, STATUS_SAVING):
            resp["message"] = {
                STATUS_DOWNLOADING: "正在下载输入素材",
                STATUS_RUNNING: "正在生成",
                STATUS_SAVING: "正在保存输出",
            }.get(self.status, "")

        return resp


class TaskQueue:
    def __init__(
        self,
        task_store: TaskStore,
        file_store: FileStore,
        max_concurrent: int = 1,
        max_pending: int = 10,
    ):
        self.task_store = task_store
        self.file_store = file_store
        self.max_concurrent = max_concurrent
        self.max_pending = max_pending

        self._pending: List[TaskDef] = []
        self._running: List[TaskDef] = []
        self._all: Dict[str, TaskDef] = {}
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._running_flag = False

        # 执行器注册
        self._executors: Dict[str, Callable] = {}

    def register_executor(self, task_type: str, func: Callable):
        self._executors[task_type] = func

    def start(self):
        if self._running_flag:
            return
        self._running_flag = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._running_flag = False
        if self._worker:
            self._worker.join(timeout=5)

    def submit(self, task_type: str, model: str, params: dict) -> TaskDef:
        with self._lock:
            pending_count = len(self._pending)
            if pending_count >= self.max_pending:
                raise RuntimeError("queue_full")

            task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            task = TaskDef(
                task_id=task_id,
                task_type=task_type,
                model=model,
                input=params,
            )
            self._pending.append(task)
            self._all[task_id] = task

            # 持久化
            self.task_store.save(task_id, task.to_dict())

        return task

    def get(self, task_id: str) -> Optional[TaskDef]:
        with self._lock:
            return self._all.get(task_id)

    def get_all(self) -> List[TaskDef]:
        with self._lock:
            return list(self._all.values())

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._all.get(task_id)
            if task is None:
                return False
            if task.status in (STATUS_QUEUED, STATUS_DOWNLOADING):
                task.status = STATUS_CANCELLED
                task._cancel_flag = True
                self._pending = [t for t in self._pending if t.task_id != task_id]
                self.task_store.update_status(task_id, STATUS_CANCELLED)
                return True
            if task.status == STATUS_RUNNING:
                task._cancel_flag = True
                return True
        return False

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def _loop(self):
        while self._running_flag:
            try:
                task = None
                with self._lock:
                    if self._pending and len(self._running) < self.max_concurrent:
                        task = self._pending.pop(0)

                if task:
                    self._execute(task)

                time.sleep(0.5)
            except Exception:
                time.sleep(1)

    def _execute(self, task: TaskDef):
        with self._lock:
            self._running.append(task)

        try:
            task.status = STATUS_DOWNLOADING
            task.started_at = time.time()
            self.task_store.update_status(task.task_id, STATUS_DOWNLOADING)

            executor = self._executors.get(task.task_type)
            if executor is None:
                raise ValueError(f"No executor for task type: {task.task_type}")

            task.status = STATUS_RUNNING
            self.task_store.update_status(task.task_id, STATUS_RUNNING, 0.1)

            def progress_callback(pct: float):
                task.progress = pct
                task.updated_at = time.time()
                self.task_store.update_status(
                    task.task_id, STATUS_RUNNING, pct
                )

            result = executor(task, progress_callback)

            if task._cancel_flag:
                task.status = STATUS_CANCELLED
                self.task_store.update_status(task.task_id, STATUS_CANCELLED)
                return

            task.status = STATUS_SAVING
            self.task_store.update_status(task.task_id, STATUS_SAVING, 0.9)

            if result and isinstance(result, (str, Path)):
                result_path = Path(result)
                saved = self.file_store.copy_to_output(task.task_id, result_path)
                task.output_file = str(saved) if saved else str(result_path)

            task.status = STATUS_SUCCEEDED
            task.progress = 1.0
            task.completed_at = time.time()
            self.task_store.update_status(
                task.task_id,
                STATUS_SUCCEEDED,
                1.0,
                output_file=task.output_file,
            )

        except Exception as e:
            task.status = STATUS_FAILED
            task.error = str(e)
            task.completed_at = time.time()
            self.task_store.update_status(
                task.task_id, STATUS_FAILED, task.progress, error=str(e)
            )

        finally:
            with self._lock:
                self._running = [t for t in self._running if t.task_id != task.task_id]

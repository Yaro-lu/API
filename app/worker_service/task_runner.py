import json
import uuid
import shutil
import base64
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import logging


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    task_id: str
    type: str
    status: TaskStatus
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    input: Dict[str, Any] = field(default_factory=dict)
    output_file: Optional[str] = None
    error: Optional[str] = None
    _cancelled: bool = False


class TaskRunner:
    def __init__(
        self,
        comfy_client,
        model_manager,
        comfyui_path: Path,
        outputs_dir: Path,
        logger: Optional[logging.Logger] = None
    ):
        self.comfy_client = comfy_client
        self.model_manager = model_manager
        self.comfyui_path = comfyui_path
        self.outputs_dir = outputs_dir
        self.logger = logger or logging.getLogger(__name__)
        self.tasks: Dict[str, Task] = {}
        self._lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

    def start_worker(self):
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self.logger.info("Task runner worker started")

    def stop_worker(self):
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        self.logger.info("Task runner worker stopped")

    def _worker_loop(self):
        while self._running:
            try:
                with self._lock:
                    for task in self.tasks.values():
                        if task.status == TaskStatus.PENDING and not task._cancelled:
                            self._execute_task(task)
                            break
                time.sleep(0.5)
            except Exception as e:
                self.logger.error(f"Worker loop error: {e}")
                time.sleep(1)

    def _execute_task(self, task: Task):
        try:
            with self._lock:
                task.status = TaskStatus.RUNNING
                task.started_at = time.time()

            self.logger.info(f"Starting task {task.task_id}")

            if task.type == "image_t2i":
                result = self._run_image_t2i_sync(task)
            elif task.type == "video_flf2v":
                result = self._run_video_flf2v_sync(task)
            else:
                raise ValueError(f"Unknown task type: {task.type}")

            with self._lock:
                task.status = TaskStatus.COMPLETED
                task.progress = 1.0
                task.completed_at = time.time()
                task.output_file = result.get("output_file")

            self.logger.info(f"Task {task.task_id} completed successfully")

        except Exception as e:
            self.logger.error(f"Task {task.task_id} failed: {e}")
            with self._lock:
                task.status = TaskStatus.FAILED
                task.completed_at = time.time()
                task.error = str(e)

    def submit_image_t2i(self, request: Dict) -> Task:
        task_id = f"local_{uuid.uuid4().hex[:8]}"
        task = Task(
            task_id=task_id,
            type="image_t2i",
            status=TaskStatus.PENDING,
            input=request
        )
        with self._lock:
            self.tasks[task_id] = task
        self.logger.info(f"Submitted image t2i task: {task_id}")
        return task

    def submit_video_flf2v(self, request: Dict) -> Task:
        task_id = f"local_{uuid.uuid4().hex[:8]}"
        task = Task(
            task_id=task_id,
            type="video_flf2v",
            status=TaskStatus.PENDING,
            input=request
        )
        with self._lock:
            self.tasks[task_id] = task
        self.logger.info(f"Submitted video flf2v task: {task_id}")
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self.tasks.get(task_id)

    def get_all_tasks(self) -> list[Task]:
        with self._lock:
            return list(self.tasks.values())

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if task and task.status in [TaskStatus.PENDING, TaskStatus.RUNNING]:
                task._cancelled = True
                task.status = TaskStatus.CANCELLED
                task.completed_at = time.time()
                return True
        return False

    def _run_image_t2i_sync(self, task: Task) -> Dict:
        request = task.input
        model_id = request["model"]
        model_config = self.model_manager.get_model_config(model_id)

        if not model_config:
            raise ValueError(f"Model {model_id} not found")

        if not self.model_manager.is_model_available(model_id):
            raise Exception(f"Model {model_id} not available")

        workflow_path = self.model_manager.get_workflow_path(model_id)
        if not workflow_path or not workflow_path.exists():
            raise Exception(f"Workflow not found: {workflow_path}")

        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)

        workflow = self._replace_image_t2i_params(workflow, request, model_config)

        with self._lock:
            task.progress = 0.1

        output_files = self.comfy_client.execute_workflow(
            workflow, task.task_id, self.outputs_dir.parent / "logs"
        )

        with self._lock:
            task.progress = 0.8

        if not output_files:
            raise Exception("No output files from ComfyUI")

        result_file = self._get_latest_output(output_files)

        with self._lock:
            task.progress = 1.0

        return {
            "task_id": task.task_id,
            "status": "completed",
            "output_file": str(result_file)
        }

    def _run_video_flf2v_sync(self, task: Task) -> Dict:
        request = task.input
        model_id = request["model"]
        model_config = self.model_manager.get_model_config(model_id)

        if not model_config:
            raise ValueError(f"Model {model_id} not found")

        if not self.model_manager.is_model_available(model_id):
            raise Exception(f"Model {model_id} not available")

        workflow_path = self.model_manager.get_workflow_path(model_id)
        if not workflow_path or not workflow_path.exists():
            raise Exception(f"Workflow not found: {workflow_path}")

        with open(workflow_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)

        workflow = self._replace_video_flf2v_params(workflow, request, model_config)

        with self._lock:
            task.progress = 0.1

        output_files = self.comfy_client.execute_workflow(
            workflow, task.task_id, self.outputs_dir.parent / "logs"
        )

        with self._lock:
            task.progress = 0.8

        if not output_files:
            raise Exception("No output files from ComfyUI")

        result_file = self._get_latest_output(output_files)

        with self._lock:
            task.progress = 1.0

        return {
            "task_id": task.task_id,
            "status": "completed",
            "output_file": str(result_file)
        }

    def run_image_t2i(self, request: Dict, task_id: Optional[str] = None) -> Dict:
        task = self.submit_image_t2i(request)
        self._execute_task(task)
        return {
            "task_id": task.task_id,
            "status": task.status.value,
            "output_file": task.output_file
        }

    def run_video_flf2v(self, request: Dict, task_id: Optional[str] = None) -> Dict:
        task = self.submit_video_flf2v(request)
        self._execute_task(task)
        return {
            "task_id": task.task_id,
            "status": task.status.value,
            "output_file": task.output_file
        }

    def _save_base64_image(self, base64_data: str, filename: str) -> Path:
        input_dir = self.comfyui_path / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        if "," in base64_data:
            base64_data = base64_data.split(",")[1]

        image_data = base64.b64decode(base64_data)
        file_path = input_dir / filename

        with open(file_path, "wb") as f:
            f.write(image_data)

        return file_path

    def _replace_image_t2i_params(self, workflow: Dict, request: Dict, model_config) -> Dict:
        prompt = request["prompt"]
        negative_prompt = request.get("negative_prompt", "")
        width = request.get("width", model_config.defaults["width"])
        height = request.get("height", model_config.defaults["height"])
        steps = request.get("steps", model_config.defaults["steps"])
        cfg = request.get("cfg", model_config.defaults.get("cfg", 5.0))
        seed = request.get("seed", -1)

        for node_id, node in workflow.items():
            class_type = node.get("class_type", "")
            inputs = node.get("inputs", {})

            if "text" in inputs:
                title = node.get("_meta", {}).get("title", "")
                if "Negative" in title or "negative" in title.lower():
                    inputs["text"] = negative_prompt
                elif isinstance(inputs["text"], str):
                    inputs["text"] = prompt

            if "width" in inputs:
                inputs["width"] = width
            if "height" in inputs:
                inputs["height"] = height

            if "steps" in inputs:
                inputs["steps"] = steps

            if "cfg" in inputs:
                inputs["cfg"] = cfg

            if "seed" in inputs:
                inputs["seed"] = seed

            if "noise_seed" in inputs:
                inputs["noise_seed"] = seed if seed != -1 else 0

        return workflow

    def _replace_video_flf2v_params(self, workflow: Dict, request: Dict, model_config) -> Dict:
        prompt = request["prompt"]
        first_frame = request["first_frame"]
        last_frame = request["last_frame"]
        width = request.get("width", model_config.defaults["width"])
        height = request.get("height", model_config.defaults["height"])
        frames = request.get("frames", model_config.defaults["frames"])
        steps = request.get("steps", model_config.defaults["steps"])
        cfg = request.get("cfg", model_config.defaults.get("cfg", 3.0))
        shift = request.get("shift", model_config.defaults.get("shift", 8.0))
        seed = request.get("seed", -1)

        if first_frame.startswith("data:image"):
            first_frame_path = self._save_base64_image(first_frame, f"start_frame_{uuid.uuid4().hex[:8]}.png")
            first_frame = first_frame_path.name

        if last_frame.startswith("data:image"):
            last_frame_path = self._save_base64_image(last_frame, f"end_frame_{uuid.uuid4().hex[:8]}.png")
            last_frame = last_frame_path.name

        for node_id, node in workflow.items():
            class_type = node.get("class_type", "")
            inputs = node.get("inputs", {})
            title = node.get("_meta", {}).get("title", "")

            if "text" in inputs:
                if "Negative" in title or "negative" in title.lower():
                    pass
                elif isinstance(inputs["text"], str):
                    inputs["text"] = prompt

            if "width" in inputs:
                inputs["width"] = width
            if "height" in inputs:
                inputs["height"] = height

            if "length" in inputs:
                inputs["length"] = frames
            if "frames" in inputs:
                inputs["frames"] = frames

            if "steps" in inputs:
                inputs["steps"] = steps

            if "cfg" in inputs:
                inputs["cfg"] = cfg

            if "shift" in inputs:
                inputs["shift"] = shift

            if "seed" in inputs:
                inputs["seed"] = seed

            if class_type == "LoadImage":
                if "Start" in title or "start" in title.lower():
                    inputs["image"] = first_frame
                elif "End" in title or "end" in title.lower():
                    inputs["image"] = last_frame

        return workflow

    def _get_latest_output(self, output_files: list) -> Path:
        comfy_output_dir = self.comfyui_path / "output"
        latest_file = None
        latest_mtime = 0

        for file_info in output_files:
            file_path = comfy_output_dir / file_info.get("subfolder", "") / file_info["filename"]
            if file_path.exists():
                mtime = file_path.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = file_path

        if latest_file:
            dest_path = self.outputs_dir / latest_file.name
            shutil.copy2(latest_file, dest_path)
            return dest_path

        raise Exception("Output file not found")

import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any
import logging
from enum import Enum


class TaskPollerState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class TaskPoller:
    def __init__(
        self,
        server_client,
        task_runner,
        comfy_manager,
        config,
        log_manager,
        poll_interval: int = 5
    ):
        self.server_client = server_client
        self.task_runner = task_runner
        self.comfy_manager = comfy_manager
        self.config = config
        self.logger = log_manager.get_logger("poller")
        self.poll_interval = poll_interval

        self._state = TaskPollerState.STOPPED
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._current_task: Optional[Dict] = None

    @property
    def state(self) -> TaskPollerState:
        return self._state

    @property
    def current_task(self) -> Optional[Dict]:
        return self._current_task

    def start(self):
        if self._state != TaskPollerState.STOPPED:
            self.logger.warning("Poller is already running")
            return

        self._state = TaskPollerState.IDLE
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.logger.info("Task poller started")

    def stop(self):
        if self._state == TaskPollerState.STOPPED:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._state = TaskPollerState.STOPPED
        self.logger.info("Task poller stopped")

    def pause(self):
        if self._state == TaskPollerState.IDLE:
            self._state = TaskPollerState.PAUSED
            self.logger.info("Task poller paused")

    def resume(self):
        if self._state == TaskPollerState.PAUSED:
            self._state = TaskPollerState.IDLE
            self.logger.info("Task poller resumed")

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                if self._state == TaskPollerState.PAUSED:
                    time.sleep(self.poll_interval)
                    continue

                if self._state == TaskPollerState.IDLE:
                    self._try_poll_task()

                time.sleep(self.poll_interval)
            except Exception as e:
                self.logger.error(f"Poll loop error: {e}", exc_info=True)
                time.sleep(5)

    def _try_poll_task(self):
        try:
            task = self.server_client.get_next_task()
            if task:
                self.logger.info(f"Received new task: {task}")
                self._execute_task(task)
        except Exception as e:
            self.logger.error(f"Failed to poll task: {e}")

    def _execute_task(self, task: Dict[str, Any]):
        self._current_task = task
        self._state = TaskPollerState.RUNNING
        task_id = task.get("id")
        task_type = task.get("type")

        try:
            self.logger.info(f"Starting task {task_id} ({task_type})")

            if not self.comfy_manager.is_running():
                self.logger.info("Starting ComfyUI...")
                started = self.comfy_manager.start()
                if not started:
                    raise Exception("Failed to start ComfyUI")

            self.server_client.update_progress(task_id, 0.0, "Initializing...")

            if task_type == "video_flf2v":
                result = self._run_video_flf2v_task(task)
                self._handle_task_success(task_id, result)
            elif task_type == "image_t2i":
                result = self._run_image_t2i_task(task)
                self._handle_task_success(task_id, result)
            else:
                raise Exception(f"Unsupported task type: {task_type}")

        except Exception as e:
            self.logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            self._handle_task_error(task_id, str(e))
        finally:
            self._current_task = None
            self._state = TaskPollerState.IDLE

    def _run_video_flf2v_task(self, task: Dict[str, Any]) -> Dict:
        task_id = task["id"]
        params = task.get("params", {})

        self.server_client.update_progress(task_id, 0.1, "Loading workflow...")

        request = {
            "model": params.get("model", "wan2.1-flf2v-14b-fp8"),
            "prompt": params.get("prompt", ""),
            "first_frame": params.get("first_frame", ""),
            "last_frame": params.get("last_frame", ""),
            "width": params.get("width", 768),
            "height": params.get("height", 432),
            "frames": params.get("frames", 49),
            "seed": params.get("seed", -1)
        }

        self.server_client.update_progress(task_id, 0.2, "Generating video...")

        result = self.task_runner.run_video_flf2v(request, task_id)

        self.server_client.update_progress(task_id, 0.9, "Preparing result...")

        return result

    def _run_image_t2i_task(self, task: Dict[str, Any]) -> Dict:
        task_id = task["id"]
        params = task.get("params", {})

        self.server_client.update_progress(task_id, 0.1, "Loading workflow...")

        request = {
            "model": params.get("model", "flux-2-klein-9b"),
            "prompt": params.get("prompt", ""),
            "negative_prompt": params.get("negative_prompt", ""),
            "width": params.get("width", 1024),
            "height": params.get("height", 1024),
            "steps": params.get("steps", 28),
            "cfg": params.get("cfg", 3.5),
            "seed": params.get("seed", -1)
        }

        self.server_client.update_progress(task_id, 0.2, "Generating image...")

        result = self.task_runner.run_image_t2i(request, task_id)

        self.server_client.update_progress(task_id, 0.9, "Preparing result...")

        return result

    def _handle_task_success(self, task_id: str, result: Dict):
        self.logger.info(f"Task {task_id} completed successfully")
        self.server_client.update_progress(task_id, 1.0, "Completed")

        output_file = result.get("output_file")
        if output_file:
            file_path = Path(output_file)
            if file_path.exists():
                self.logger.info(f"Uploading result for task {task_id}")
                uploaded = self.server_client.upload_result(task_id, file_path)
                if uploaded:
                    self.logger.info(f"Result uploaded successfully for task {task_id}")
                else:
                    self.logger.warning(f"Failed to upload result for task {task_id}")

    def _handle_task_error(self, task_id: str, error: str):
        self.logger.error(f"Task {task_id} error: {error}")
        self.server_client.report_error(task_id, error)

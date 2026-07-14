import subprocess
import requests
import time
from pathlib import Path
from typing import Optional
from threading import Thread
import logging


class ComfyUIManager:
    def __init__(self, comfyui_path: Path, python_path: Path, port: int = 8188, models_path: Optional[Path] = None, logger: Optional[logging.Logger] = None):
        self.comfyui_path = comfyui_path
        self.python_path = python_path
        self.port = port
        self.models_path = models_path
        self.logger = logger or logging.getLogger(__name__)
        self._process: Optional[subprocess.Popen] = None
        self._log_thread: Optional[Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def is_running(self) -> bool:
        try:
            response = requests.get(f"{self.url}/system_stats", timeout=5)
            return response.status_code == 200
        except Exception:
            return False

    def start(self, lowvram: bool = False, wait: bool = True, timeout: int = 60) -> bool:
        if self.is_running():
            self.logger.info("ComfyUI is already running")
            return True

        comfyui_main = self.comfyui_path / "main.py"
        if not comfyui_main.exists():
            self.logger.error(f"ComfyUI main.py not found at {comfyui_main}")
            return False

        args = [
            str(self.python_path),
            str(comfyui_main),
            "--listen", "127.0.0.1",
            "--port", str(self.port),
            "--disable-auto-launch",
            "--normalvram"
        ]
        
        # 添加自定义模型路径
        if self.models_path:
            args.extend([
                "--extra-model-paths",
                str(self.models_path)
            ])

        if lowvram:
            args.remove("--normalvram")
            args.append("--lowvram")

        self.logger.info(f"Starting ComfyUI with args: {args}")

        try:
            self._process = subprocess.Popen(
                args,
                cwd=str(self.comfyui_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            self._log_thread = Thread(target=self._log_output, daemon=True)
            self._log_thread.start()

            if wait:
                return self._wait_for_ready(timeout)
            return True

        except Exception as e:
            self.logger.error(f"Failed to start ComfyUI: {e}")
            return False

    def _log_output(self):
        if self._process and self._process.stdout:
            for line in self._process.stdout:
                self.logger.debug(f"[ComfyUI] {line.rstrip()}")

    def _wait_for_ready(self, timeout: int) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_running():
                self.logger.info("ComfyUI is ready")
                return True
            time.sleep(2)
        self.logger.error("ComfyUI startup timeout")
        return False

    def stop(self):
        if self._process:
            self.logger.info("Stopping ComfyUI")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def __del__(self):
        self.stop()

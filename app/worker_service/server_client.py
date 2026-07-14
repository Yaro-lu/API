import requests
import json
from typing import Dict, Optional, Any
from pathlib import Path
import logging


class ServerClient:
    def __init__(self, base_url: str, token: str = "", logger: Optional[logging.Logger] = None):
        self.base_url = base_url
        self.token = token
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def set_token(self, token: str):
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"

    def register(self, client_id: str, version: str, gpu_info: Dict, runtime_info: Dict, models: list) -> bool:
        url = f"{self.base_url}/api/worker/register"
        payload = {
            "client_id": client_id,
            "version": version,
            "gpu": gpu_info,
            "runtime": runtime_info,
            "models": models
        }
        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return True
        except Exception as e:
            self.logger.error(f"Registration failed: {e}")
            return False

    def heartbeat(self) -> bool:
        url = f"{self.base_url}/api/worker/heartbeat"
        try:
            response = self.session.post(url, timeout=30)
            return response.status_code == 200
        except Exception as e:
            self.logger.error(f"Heartbeat failed: {e}")
            return False

    def get_next_task(self) -> Optional[Dict]:
        url = f"{self.base_url}/api/worker/tasks/next"
        try:
            response = self.session.get(url, timeout=30)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            self.logger.error(f"Get next task failed: {e}")
            return None

    def update_progress(self, task_id: str, progress: float, message: str = "") -> bool:
        url = f"{self.base_url}/api/worker/tasks/{task_id}/progress"
        payload = {"progress": progress, "message": message}
        try:
            response = self.session.post(url, json=payload, timeout=30)
            return response.status_code == 200
        except Exception as e:
            self.logger.error(f"Update progress failed: {e}")
            return False

    def upload_result(self, task_id: str, file_path: Path) -> bool:
        url = f"{self.base_url}/api/worker/tasks/{task_id}/result"
        try:
            with open(file_path, "rb") as f:
                files = {"file": f}
                response = self.session.post(url, files=files, timeout=300)
                return response.status_code == 200
        except Exception as e:
            self.logger.error(f"Upload result failed: {e}")
            return False

    def report_error(self, task_id: str, error: str) -> bool:
        url = f"{self.base_url}/api/worker/tasks/{task_id}/error"
        payload = {"error": error}
        try:
            response = self.session.post(url, json=payload, timeout=30)
            return response.status_code == 200
        except Exception as e:
            self.logger.error(f"Report error failed: {e}")
            return False

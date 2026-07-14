import json
import requests
import time
from pathlib import Path
from typing import Dict, Optional, Any
import logging


class ComfyUIClient:
    def __init__(self, url: str = "http://127.0.0.1:8188", logger: Optional[logging.Logger] = None):
        self.url = url
        self.logger = logger or logging.getLogger(__name__)

    def get_history(self, prompt_id: Optional[str] = None) -> Dict:
        if prompt_id:
            response = requests.get(f"{self.url}/history/{prompt_id}")
        else:
            response = requests.get(f"{self.url}/history")
        return response.json()

    def queue_prompt(self, workflow: Dict[str, Any]) -> str:
        payload = {"prompt": workflow}
        response = requests.post(f"{self.url}/prompt", json=payload, timeout=60)
        try:
            data = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError(f"ComfyUI returned non-JSON response: {response.text[:300]}")
        if not response.ok or "prompt_id" not in data:
            message = data.get("error") or data.get("message") or data.get("detail") or str(data)
            raise RuntimeError(f"ComfyUI prompt validation failed: {message}")
        return data["prompt_id"]

    def wait_for_completion(self, prompt_id: str, timeout: int = 3600) -> Optional[Dict]:
        start_time = time.time()
        while time.time() - start_time < timeout:
            history = self.get_history(prompt_id)
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(2)
        return None

    def get_output_files(self, history_item: Dict) -> list:
        outputs = history_item.get("outputs", {})
        files = []
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                for img in node_output["images"]:
                    filename = img["filename"]
                    subfolder = img.get("subfolder", "")
                    type_ = img.get("type", "output")
                    files.append({
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": type_
                    })
        return files

    def execute_workflow(self, workflow: Dict[str, Any], task_id: str, logs_dir: Path) -> list:
        workflows_log_dir = logs_dir / "workflows"
        workflows_log_dir.mkdir(parents=True, exist_ok=True)

        with open(workflows_log_dir / f"{task_id}.json", "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False, indent=2)

        prompt_id = self.queue_prompt(workflow)
        self.logger.info(f"Queued workflow with prompt_id: {prompt_id}")

        history = self.wait_for_completion(prompt_id)
        if not history:
            raise Exception("Workflow timeout")

        return self.get_output_files(history)

"""
请求保存 — runtime/requests/{task_id}.json
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


class RequestStore:
    def __init__(self, requests_dir: Path, unsafe_keep_auth: bool = False):
        self.requests_dir = Path(requests_dir)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.unsafe_keep_auth = unsafe_keep_auth

    def save(self, task_id: str, endpoint: str, headers: dict, body: Any):
        safe_headers = dict(headers)
        if not self.unsafe_keep_auth:
            safe_headers["authorization"] = "[redacted]"

        req = {
            "task_id": task_id,
            "received_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime()
            ),
            "endpoint": endpoint,
            "headers": safe_headers,
            "body": body,
        }
        filepath = self.requests_dir / f"{task_id}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=2)

    def load(self, task_id: str) -> Optional[dict]:
        filepath = self.requests_dir / f"{task_id}.json"
        if not filepath.exists():
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

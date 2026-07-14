"""
任务状态持久化 — runtime/tasks/{task_id}.json
"""
import json
import time
from pathlib import Path
from typing import Dict, List, Optional


class TaskStore:
    def __init__(self, tasks_dir: Path):
        self.tasks_dir = Path(tasks_dir)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def save(self, task_id: str, data: dict):
        data.setdefault("task_id", task_id)
        data.setdefault("updated_at", time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime()
        ))
        filepath = self.tasks_dir / f"{task_id}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, task_id: str) -> Optional[dict]:
        filepath = self.tasks_dir / f"{task_id}.json"
        if not filepath.exists():
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete(self, task_id: str):
        filepath = self.tasks_dir / f"{task_id}.json"
        if filepath.exists():
            filepath.unlink()

    def list_all(self) -> List[dict]:
        tasks = []
        for f in sorted(self.tasks_dir.glob("*.json"), reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    tasks.append(json.load(fp))
            except (json.JSONDecodeError, IOError):
                pass
        return tasks

    def update_status(
        self,
        task_id: str,
        status: str,
        progress: float = 0.0,
        error: Optional[str] = None,
        output_file: Optional[str] = None,
    ):
        data = self.load(task_id)
        if data is None:
            return
        data["status"] = status
        data["progress"] = progress
        if error is not None:
            data["error"] = error
        if output_file is not None:
            data["output_file"] = output_file
        self.save(task_id, data)

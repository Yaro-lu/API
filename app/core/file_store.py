"""
文件存储管理 — runtime/inputs/ + runtime/outputs/
"""
import shutil
from pathlib import Path
from typing import Optional


class FileStore:
    def __init__(self, inputs_dir: Path, outputs_dir: Path, temp_dir: Path):
        self.inputs_dir = Path(inputs_dir)
        self.outputs_dir = Path(outputs_dir)
        self.temp_dir = Path(temp_dir)
        for d in [self.inputs_dir, self.outputs_dir, self.temp_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def task_input_dir(self, task_id: str) -> Path:
        p = self.inputs_dir / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def task_output_dir(self, task_id: str) -> Path:
        p = self.outputs_dir / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_input_file(self, task_id: str, filename: str, content: bytes):
        dest = self.task_input_dir(task_id) / filename
        dest.write_bytes(content)

    def save_output_file(self, task_id: str, filename: str, content: bytes):
        dest = self.task_output_dir(task_id) / filename
        dest.write_bytes(content)

    def get_output_path(self, task_id: str, filename: str) -> Optional[Path]:
        p = self.task_output_dir(task_id) / filename
        return p if p.exists() else None

    def copy_to_output(self, task_id: str, src: Path) -> Optional[Path]:
        if not src or not src.exists():
            return None
        dest = self.task_output_dir(task_id) / src.name
        shutil.copy2(src, dest)
        return dest

    def list_outputs(self, task_id: str) -> list:
        d = self.task_output_dir(task_id)
        return [f.name for f in d.iterdir() if f.is_file()]

    def list_inputs(self, task_id: str) -> list:
        d = self.task_input_dir(task_id)
        return [f.name for f in d.iterdir() if f.is_file()]

    def delete_task_data(self, task_id: str):
        for d in [self.task_input_dir(task_id), self.task_output_dir(task_id)]:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

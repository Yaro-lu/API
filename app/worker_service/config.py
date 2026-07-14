import os
import yaml
from pathlib import Path
from typing import Dict, Any


class Config:
    def __init__(self, config_path: str = None):
        if config_path is None:
            base_dir = Path(__file__).parent.parent.parent
            config_path = base_dir / "config" / "client.yaml"
        self.config_path = Path(config_path)
        self._config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    @property
    def base_dir(self) -> Path:
        return Path(__file__).parent.parent.parent

    @property
    def runtime_dir(self) -> Path:
        return self.base_dir / "runtime"

    @property
    def python_path(self) -> Path:
        return self.base_dir / self.get("runtime.python_path")

    @property
    def comfyui_path(self) -> Path:
        return self.base_dir / self.get("runtime.comfyui_path")

    @property
    def ffmpeg_path(self) -> Path:
        return self.base_dir / self.get("runtime.ffmpeg_path")

    @property
    def models_dir(self) -> Path:
        return self.base_dir / self.get("paths.models")

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / self.get("paths.logs")

    @property
    def outputs_dir(self) -> Path:
        return self.base_dir / self.get("paths.outputs")

    @property
    def local_api_port(self) -> int:
        return self.get("runtime.local_api_port", 8090)

    @property
    def comfyui_port(self) -> int:
        return self.get("runtime.comfyui_port", 8188)

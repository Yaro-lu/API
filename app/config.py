"""
配置加载器 — 从 runtime/config.local.json 读取配置
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional


class Config:
    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).parent.parent
        self.config_path = self.base_dir / "runtime" / "config.local.json"
        self._config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return self._default_config()

    def _default_config(self) -> Dict[str, Any]:
        return {
            "server": {"host": "127.0.0.1", "port": 18188},
            "tunnel": {
                "provider": "cloudflare_quick_tunnel",
                "protocol": "auto",
                "cloudflared_path": "bin/cloudflared.exe",
            },
            "auth": {
                "api_key_mode": "auto_generate_on_start",
                "api_key_prefix": "sk-local",
            },
            "queue": {"max_concurrent": 1, "max_pending": 10},
            "storage": {
                "keep_requests": True,
                "keep_inputs": True,
                "keep_outputs": True,
                "keep_logs": True,
                "auto_cleanup": False,
                "unsafe_keep_auth_header": False,
            },
            "comfyui": {"base_url": "http://127.0.0.1:8188", "timeout_sec": 3600},
            "text_model": {
                "mode": "mock",
                "forward_base_url": "",
                "forward_api_key": "",
                "forward_model": "",
            },
            "model_aliases": {
                "seedance-1.5-pro": {
                    "type": "video.first_last_to_video",
                    "workflow_id": "wan_flf2v_v1",
                },
                "local-video-flf2v": {
                    "type": "video.first_last_to_video",
                    "workflow_id": "wan_flf2v_v1",
                },
                "local-text-default": {
                    "type": "text.chat",
                    "adapter": "chat_default",
                },
            },
        }

    def save(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value: Any):
        keys = key.split(".")
        target = self._config
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value

    @property
    def server_host(self) -> str:
        return self.get("server.host", "127.0.0.1")

    @property
    def server_port(self) -> int:
        return self.get("server.port", 18188)

    @property
    def comfyui_url(self) -> str:
        return self.get("comfyui.base_url", "http://127.0.0.1:8188")

    @property
    def comfyui_timeout(self) -> int:
        return self.get("comfyui.timeout_sec", 3600)

    @property
    def max_concurrent(self) -> int:
        return self.get("queue.max_concurrent", 1)

    @property
    def max_pending(self) -> int:
        return self.get("queue.max_pending", 10)

    @property
    def runtime_dir(self) -> Path:
        p = self.base_dir / "runtime"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def requests_dir(self) -> Path:
        p = self.runtime_dir / "requests"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def inputs_dir(self) -> Path:
        p = self.runtime_dir / "inputs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def outputs_dir(self) -> Path:
        p = self.runtime_dir / "outputs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tasks_dir(self) -> Path:
        p = self.runtime_dir / "tasks"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self.runtime_dir / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def temp_dir(self) -> Path:
        p = self.runtime_dir / "temp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cloudflared_path(self) -> Path:
        rel = self.get("tunnel.cloudflared_path", "bin/cloudflared.exe")
        return self.base_dir / rel

    @property
    def tunnel_protocol(self) -> str:
        return self.get("tunnel.protocol", "auto")

    @property
    def model_aliases(self) -> Dict[str, Any]:
        return self.get("model_aliases", {})

    @property
    def storage_config(self) -> Dict[str, Any]:
        return self.get("storage", {})

    def get_model_alias(self, model_id: str) -> Optional[Dict[str, Any]]:
        aliases = self.model_aliases
        for key, val in aliases.items():
            if key == model_id:
                return val
        return None

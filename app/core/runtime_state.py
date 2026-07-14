"""
运行时状态管理 — session.json
"""
import json
import secrets
import string
import time
from pathlib import Path
from typing import Optional


class RuntimeState:
    """全局运行时状态，管理 session.json"""

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.session_path = self.runtime_dir / "session.json"
        self._state = self._load()

    def _load(self) -> dict:
        if self.session_path.exists():
            try:
                with open(self.session_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return self._default_state()

    def _default_state(self) -> dict:
        return {
            "session_id": "",
            "base_url": "",
            "api_key": "",
            "local_api": "http://127.0.0.1:18188",
            "started_at": "",
            "status": "offline",
            "tunnel_provider": "cloudflare_quick_tunnel",
        }

    def save(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with open(self.session_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def start_session(self, local_port: int = 18188):
        """启动新会话"""
        self._state["session_id"] = self._generate_session_id()
        self._state["local_api"] = f"http://127.0.0.1:{local_port}"
        self._state["started_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime()
        )
        self._state["status"] = "starting"
        if not self._state.get("api_key"):
            self._state["api_key"] = self._generate_api_key()
        self.save()

    def set_online(self, base_url: str):
        """Tunnel 建立成功"""
        self._state["base_url"] = base_url
        self._state["status"] = "online"
        self.save()

    def set_error(self, reason: str):
        self._state["status"] = "error"
        self._state["error"] = reason
        self.save()

    @staticmethod
    def _generate_session_id() -> str:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        return f"sess_{ts}"

    @staticmethod
    def _generate_api_key() -> str:
        chars = string.ascii_letters + string.digits
        rand = "".join(secrets.choice(chars) for _ in range(40))
        return f"sk-local-{rand}"

    # ── Properties ──────────────────────────────────────
    @property
    def api_key(self) -> str:
        return self._state.get("api_key", "")

    @property
    def base_url(self) -> str:
        return self._state.get("base_url", "")

    @property
    def local_api(self) -> str:
        return self._state.get("local_api", "http://127.0.0.1:18188")

    @property
    def status(self) -> str:
        return self._state.get("status", "offline")

    @property
    def session_id(self) -> str:
        return self._state.get("session_id", "")

    def to_dict(self) -> dict:
        return dict(self._state)

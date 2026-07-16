"""
运行时状态管理 — session.json
"""
import json
import os
import secrets
import string
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from app.core.secret_store import protect_text, unprotect_text


class RuntimeState:
    """全局运行时状态，管理 session.json"""

    _thread_lock = threading.RLock()

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
            "api_key_protected": "",
            "admin_key_protected": "",
            "local_api": "http://127.0.0.1:18188",
            "started_at": "",
            "status": "offline",
            "tunnel_provider": "cloudflare_quick_tunnel",
        }

    @contextmanager
    def _locked_session(self, timeout: float = 5.0):
        """Serialize session mutations across GUI/API processes."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime_dir / ".session.lock"
        handle = open(lock_path, "a+b")
        locked = False
        deadline = time.time() + max(0.2, timeout)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            if os.name == "nt":
                import msvcrt

                while time.time() < deadline:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.03)
            else:
                import fcntl

                while time.time() < deadline:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        time.sleep(0.03)
            if not locked:
                raise TimeoutError("session.json 正在被其他进程更新")
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _write_atomic(self, state: dict):
        temp_path = self.session_path.with_name(
            f"{self.session_path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.session_path)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    def _update(self, patch):
        with self._thread_lock:
            with self._locked_session():
                latest = self._load()
                patch(latest)
                for name in ("api_key", "admin_key"):
                    value = self._secret_value(latest, name)
                    self._store_secret(latest, name, value)
                self._write_atomic(latest)
                self._state = latest

    def save(self):
        """Persist the current snapshot; field setters are preferred."""
        # A long-lived process may hold an older protected key.  ``save`` must
        # never copy that stale credential over a key rotated by another
        # process; secret setters are the only methods allowed to change keys.
        secret_fields = {
            "api_key",
            "api_key_protected",
            "admin_key",
            "admin_key_protected",
        }
        snapshot = {
            key: value for key, value in self._state.items() if key not in secret_fields
        }
        self._update(lambda latest: latest.update(snapshot))

    def start_session(self, local_port: int = 18188):
        """启动新会话"""
        def patch(latest):
            latest["session_id"] = self._generate_session_id()
            latest["local_api"] = f"http://127.0.0.1:{local_port}"
            latest["base_url"] = ""
            latest["started_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime()
            )
            latest["status"] = "starting"
            latest.pop("error", None)
            if not self._secret_value(latest, "api_key"):
                self._store_secret(latest, "api_key", self._generate_api_key())
            if not self._secret_value(latest, "admin_key"):
                self._store_secret(latest, "admin_key", self._generate_admin_key())

        self._update(patch)

    def set_offline(self):
        """Mark the local gateway offline without discarding its API key."""
        self._update(lambda latest: latest.update({"status": "offline", "base_url": ""}))

    def set_api_key(self, api_key: str):
        """Persist the local access key chosen in desktop settings."""
        value = str(api_key or "").strip()
        self._update(lambda latest: self._store_secret(latest, "api_key", value))

    def set_admin_key(self, admin_key: str):
        """Persist the private key used only by the local desktop UI."""
        value = str(admin_key or "").strip()
        self._update(lambda latest: self._store_secret(latest, "admin_key", value))

    def set_online(self, base_url: str):
        """Tunnel 建立成功"""
        self._update(lambda latest: latest.update({"base_url": base_url, "status": "online"}))

    def set_error(self, reason: str):
        self._update(lambda latest: latest.update({"status": "error", "error": reason}))

    @staticmethod
    def _generate_session_id() -> str:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        return f"sess_{ts}"

    @staticmethod
    def _generate_api_key() -> str:
        chars = string.ascii_letters + string.digits
        rand = "".join(secrets.choice(chars) for _ in range(40))
        return f"sk-local-{rand}"

    @staticmethod
    def _generate_admin_key() -> str:
        return f"sk-admin-{secrets.token_urlsafe(40)}"

    @staticmethod
    def _secret_value(data: dict, name: str) -> str:
        protected = str(data.get(f"{name}_protected") or "").strip()
        if protected:
            return unprotect_text(protected)
        return str(data.get(name) or "").strip()

    @staticmethod
    def _store_secret(data: dict, name: str, value: str):
        data.pop(name, None)
        protected_name = f"{name}_protected"
        if value:
            data[protected_name] = protect_text(value)
        else:
            data.pop(protected_name, None)

    # ── Properties ──────────────────────────────────────
    @property
    def api_key(self) -> str:
        return self._secret_value(self._state, "api_key")

    @property
    def admin_key(self) -> str:
        return self._secret_value(self._state, "admin_key")

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
        return {
            key: value
            for key, value in self._state.items()
            if key not in {"api_key", "api_key_protected", "admin_key", "admin_key_protected"}
        }

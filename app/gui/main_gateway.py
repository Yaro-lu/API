"""
灵镜造片厂 — 主界面
- 启动 ComfyUI + API 服务器 + Cloudflare Tunnel
- 环境检测 / 运行时检查 / 模型检查
- 进度监控面板
- 系统托盘（最小化到托盘）
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import subprocess
import sys
import threading
import time
import json
import os
import re
import shutil
import socket
import webbrowser
import zipfile
import msvcrt
import uuid
from pathlib import Path

try:
    import customtkinter as ctk
except Exception:
    ctk = None

BASE_DIR = Path(__file__).parent.parent.parent
GUI_ASSET_DIR = Path(__file__).parent / "assets"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.runtime_package import (
    REQUIRED_RUNTIME_PATHS,
    RUNTIME_PACKAGE_NAME,
    RUNTIME_RELEASE_URL,
    archive_extract_command,
    archive_list_command,
    find_extractor,
    invalid_archive_entries,
    missing_archive_entries,
    missing_runtime_paths,
    parse_archive_members,
    verify_sha256,
)
from app.core.process_supervisor import ProcessSupervisor
from app.core.model_maintenance import (
    MODEL_REQUIREMENTS,
    check_model_groups,
    cleanup_incomplete_imports,
    import_model_directory,
    model_file_ready,
)
from app.core.runtime_state import RuntimeState
from app.gui.dashboard_pages import StaticDashboardPages

_INSTANCE_LOCK_HANDLE = None
CTK_AVAILABLE = ctk is not None

# ── 配色 ──────────────────────────────────────────────
C = {
    "bg":       "#f1f5fc",
    "surface":  "#ffffff",
    "card":     "#ffffff",
    "primary":  "#5545fc",
    "primary2": "#6d5cff",
    "accent":   "#3844e1",
    "success":  "#20b15e",
    "warn":     "#ff9900",
    "error":    "#ef4444",
    "text":     "#172033",
    "text2":    "#52657f",
    "muted":    "#9ca9bf",
    "border":   "#c5d4ee",
    "border2":  "#d7e2f4",
    "entry":    "#fbfdff",
    "hover":    "#f4f8ff",
    "progress_bg": "#ecf0f5",
    "shadow":   "#dfe8f5",
    "button":   "#fbfdff",
    "sidebar":  "#ffffff",
    "sidebar_active": "#eeecff",
    "sidebar_border": "#dfe7f3",
    "soft_primary": "#eeecff",
    "soft_success": "#eaf9f1",
    "soft_warn": "#fff5e6",
    "soft_error": "#fff0f0",
}
F = {
    "brand":  ("Microsoft YaHei UI", 22, "bold"),
    "title":  ("Microsoft YaHei UI", 14, "bold"),
    "h2":     ("Microsoft YaHei UI", 11, "bold"),
    "bold":   ("Microsoft YaHei UI", 10, "bold"),
    "normal": ("Microsoft YaHei UI", 9),
    "button": ("Microsoft YaHei UI", 10),
    "body":   ("Microsoft YaHei UI", 10),
    "small":  ("Microsoft YaHei UI", 8),
    "tiny":   ("Microsoft YaHei UI", 8),
    "mono":   ("Consolas", 9),
    "url":    ("Consolas", 9),
}
LAYOUT = {
    "window_w": 1180,
    "window_h": 760,
    "min_w": 1090,
    "min_h": 700,
    "sidebar_w": 204,
    "outer": 18,
    "gap": 10,
    "top_h": 78,
    "status_h": 38,
    "info_h": 72,
    "left_w": 240,
    "actions_h": 42,
    "footer_h": 28,
}

API_PORT = 18188
COMFY_PORT = 8188
API_BASE = f"http://127.0.0.1:{API_PORT}"
COMFY_BASE = f"http://127.0.0.1:{COMFY_PORT}"
SERVER_SYNC_MAX_RETRIES = 3


# ══════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════

def _load_local_config():
    path = BASE_DIR / "runtime" / "config.local.json"
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _detect_gpu_memory_mb():
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        values = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
        return max(values) if values else 0
    except Exception:
        return 0


def _comfy_vram_args():
    comfy_config = _load_local_config().get("comfyui", {})
    explicit_args = comfy_config.get("launch_args")
    if isinstance(explicit_args, list):
        return [str(item) for item in explicit_args if str(item).strip()]
    mode = str(comfy_config.get("vram_mode", "auto")).strip().lower()
    if mode in ("high", "highvram"):
        return ["--highvram"]
    if mode in ("normal", "normalvram"):
        return []
    if mode in ("low", "lowvram"):
        return ["--lowvram"]
    if mode in ("none", "default", "auto-comfy"):
        return []
    gpu_memory_mb = _detect_gpu_memory_mb()
    if 0 < gpu_memory_mb <= 12288:
        return []
    if gpu_memory_mb >= 16384:
        return ["--highvram"]
    return []


def _port_in_use(port: int) -> bool:
    """检查端口是否被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _acquire_instance_lock() -> bool:
    """Use a Windows file lock so only one desktop gateway can run."""
    global _INSTANCE_LOCK_HANDLE
    lock_dir = BASE_DIR / "runtime"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_dir / "gateway.lock", "a+b")
    try:
        # msvcrt locks from the current file position.  Always lock byte 0;
        # locking at append/EOF lets a second instance lock a different byte.
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"0")
            lock_handle.flush()
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            lock_handle.close()
        except Exception:
            pass
        return False
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()).encode("ascii", errors="ignore"))
    lock_handle.flush()
    _INSTANCE_LOCK_HANDLE = lock_handle
    return True


def _release_instance_lock():
    global _INSTANCE_LOCK_HANDLE
    if not _INSTANCE_LOCK_HANDLE:
        return
    try:
        _INSTANCE_LOCK_HANDLE.seek(0)
        msvcrt.locking(_INSTANCE_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        _INSTANCE_LOCK_HANDLE.close()
    except Exception:
        pass
    _INSTANCE_LOCK_HANDLE = None


def _check_runtime_exists() -> bool:
    """检查 runtime 目录核心文件是否存在"""
    return not missing_runtime_paths(BASE_DIR)


def _runtime_has_package_files() -> bool:
    """Return whether this installation contains any managed runtime file."""
    return any((BASE_DIR / path).is_file() for path in REQUIRED_RUNTIME_PATHS)


def _model_file_ready(path: Path, expected_size: int | None = None) -> bool:
    """Compatibility wrapper around the shared model validator."""
    return model_file_ready(path, expected_size)


def _check_models_status() -> dict:
    """Check every model group used by the bundled workflows."""
    return check_model_groups(BASE_DIR / "models", MODEL_REQUIREMENTS)


def _model_download_active(control: dict) -> bool:
    return str(control.get("state") or "") in {"downloading", "resuming", "abandoning"}


def _check_torch(python_path: Path, run_command=subprocess.run) -> dict:
    """通过 runtime python 检查 PyTorch / CUDA"""
    check_script = """
import json
try:
    import torch
    r = {"torch_version": torch.__version__, "cuda_available": torch.cuda.is_available()}
    if torch.cuda.is_available():
        r["gpu_name"] = torch.cuda.get_device_name(0)
    print(json.dumps(r))
except ImportError:
    print(json.dumps({"error": "TORCH_IMPORT_FAILED"}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
"""
    try:
        proc = run_command(
            [str(python_path), "-c", check_script],
            capture_output=True, text=True, timeout=30,
            cwd=str(BASE_DIR),
        )
        if proc.returncode != 0:
            return {"success": False, "error": "PyTorch 调用失败"}
        data = json.loads(proc.stdout)
        if "error" in data:
            return {"success": False, "error": data["error"]}
        return {
            "success": data.get("cuda_available", False),
            "torch_version": data.get("torch_version", ""),
            "gpu_name": data.get("gpu_name", ""),
            "cuda_available": data.get("cuda_available", False),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "PyTorch 检测超时"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _check_system_env(run_command=subprocess.run) -> dict:
    """检查系统环境（nvidia-smi, GPU）"""
    try:
        result = run_command(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
        )
        if result.returncode != 0:
            return {"success": False, "error": "未检测到 NVIDIA 显卡驱动"}
        output = result.stdout
        lines = output.strip().split("\n")
        if not lines:
            return {"success": False, "error": "未检测到 NVIDIA 显卡"}
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) < 3:
            return {"success": False, "error": "无法解析 GPU 信息"}
        gpu_name = parts[0]
        driver = parts[1]
        vram_mb = int(parts[2])
        return {
            "success": True,
            "gpu_name": gpu_name,
            "driver_version": driver,
            "vram_gb": vram_mb // 1024,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"success": False, "error": "未检测到 NVIDIA 显卡驱动"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _ensure_extra_model_paths():
    """确保 ComfyUI 的 extra_model_paths.yaml 使用相对路径指向 models/"""
    yaml_path = BASE_DIR / "runtime" / "ComfyUI" / "extra_model_paths.yaml"
    content = """# 灵镜造片厂 — 自动生成的模型路径配置
comfyui:
    base_path: ../../models/
    checkpoints: checkpoints/
    loras: loras/
    vae: vae/
    text_encoders: |
         text_encoders/
         clip/
    diffusion_models: |
         unet/
         diffusion_models/
    clip_vision: clip_vision/
"""
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(content)


# ══════════════════════════════════════════════════════
# GatewayApp
# ══════════════════════════════════════════════════════

class RoundedFrame(tk.Frame):
    def __init__(self, parent, radius=10, fill=None, outline=None, **kwargs):
        parent_bg = kwargs.pop("bg", None)
        if parent_bg is None:
            try:
                parent_bg = parent.cget("bg")
            except Exception:
                parent_bg = C["bg"]
        super().__init__(parent, bg=parent_bg, bd=0, highlightthickness=0, **kwargs)
        self._radius = radius
        self._fill = fill or C["card"]
        self._outline = outline or C["border2"]
        self._bg_canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self._bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        # Canvas.lower() lowers canvas items, not the widget itself.
        # Use the Tcl widget command so fallback mode also starts cleanly.
        self.tk.call("lower", self._bg_canvas._w)
        self.bind("<Configure>", self._draw_bg)

    def _draw_bg(self, _event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        r = min(self._radius, w // 2, h // 2)
        c = self._bg_canvas
        c.delete("all")
        self._round_rect(c, 1, 1, w - 2, h - 2, r, fill=self._fill, outline=self._outline, width=1)

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=16, **kwargs)

WindowBase = ctk.CTk if CTK_AVAILABLE else tk.Tk


class GatewayApp(WindowBase):
    def __init__(self):
        super().__init__()
        self.title("灵镜造片厂")
        self.geometry(f"{LAYOUT['window_w']}x{LAYOUT['window_h']}")
        self.minsize(LAYOUT["min_w"], LAYOUT["min_h"])
        if CTK_AVAILABLE:
            self.configure(fg_color=C["bg"])
        else:
            self.configure(bg=C["bg"])
        self._image_refs = []
        self._set_window_icon()

        # 子进程
        self._comfy_proc = None
        self._api_proc = None
        self._process_supervisor = ProcessSupervisor(BASE_DIR)
        self._backend_action_lock = threading.Lock()
        self._backend_action_name = ""
        self._runtime_maintenance_lock = threading.RLock()
        self._runtime_maintenance_in_progress = False
        self._model_transfer_lock = threading.RLock()
        self._active_model_transfers = {}
        self._tunnel_url = ""
        self._api_key = ""
        self._poll_run = False
        self._health_poll_thread = None
        self._health_poll_lock = threading.Lock()
        self._health_poll_generation = 0
        self._shutting_down = False
        self._last_health = {}
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_url_value = "https://ai.lol-lu.site"
        self._server_account_profile = {}
        # Local mode is the product default.  Platform login is optional and
        # must never block the local API gateway from starting.
        self._server_mode = "guest"
        self._server_sync_running = False
        self._server_sync_fail_count = 0
        self._heartbeat_run = True
        self._offline_notice_sent = False
        self._login_prompt_shown = False
        self._login_popup = None
        self._runtime_maintenance_popup = None
        self._model_import_in_progress = False
        self._account_status_text = ""
        self._initial_session_sync_done = False
        self._comfy_starting_until = 0
        self._client_instance_id = self._load_client_instance_id()
        self._last_completed_outputs = []
        self._last_completed_task_id = ""
        self._task_history = []
        self._task_history_ids = set()
        self._workflow_mode = tk.StringVar(value="default")
        self._load_account_session_state()
        self._server_url_var = tk.StringVar(value=self._server_url_value or "https://ai.lol-lu.site")
        self._server_email_var = tk.StringVar(value=self._server_user_email)
        self._server_password_var = tk.StringVar(value="")

        # 系统托盘
        self._tray = None
        self._tray_thread = None

        # 状态缓存
        cleanup_incomplete_imports(BASE_DIR / "models")
        self._model_status = _check_models_status()
        self._environment_status = {}
        self._current_task_text = "无任务"

        self._setup_style()
        self._build_app_shell()
        self._build_sidebar()
        self._build_title_bar()
        self._build_page_host()
        self._build_bottom_lights()
        self._build_info_cards()
        self._build_main_area()
        self._build_workflow_model_panel()
        self._build_progress_panel()
        self._build_action_buttons()
        self._build_static_pages()
        self._build_footer()
        self._show_page("overview")
        if self._server_mode == "guest":
            self._set_light("server", "online", "本地模式")

        self.center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_minimize)

        # 异步启动序列
        self._request_backend_start("启动后台")
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def center(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(
            f"{LAYOUT['window_w']}x{LAYOUT['window_h']}"
            f"+{(sw-LAYOUT['window_w'])//2}+{(sh-LAYOUT['window_h'])//2}"
        )

    def _account_session_path(self) -> Path:
        return BASE_DIR / "runtime" / "account_session.json"

    def _client_instance_path(self) -> Path:
        return BASE_DIR / "runtime" / "client_instance.json"

    def _load_client_instance_id(self) -> str:
        path = self._client_instance_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                value = str(data.get("instance_id") or "").strip()
                if value:
                    return value
        except Exception:
            pass
        value = f"desktop-{uuid.uuid4().hex}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"instance_id": value}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return value

    def _load_account_session_state(self):
        path = self._account_session_path()
        try:
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            mode = str(data.get("mode") or "unset").strip()
            if mode not in ("logged_in", "guest"):
                return
            self._server_mode = mode
            self._server_url_value = str(data.get("server_url") or self._server_url_value).strip() or self._server_url_value
            self._server_user_email = str(data.get("email") or "").strip()
            self._server_session_token = str(data.get("session_token") or "").strip() if mode == "logged_in" else ""
            profile = data.get("profile")
            self._server_account_profile = profile if isinstance(profile, dict) else {}
        except Exception as ex:
            print(f"[Account] load session failed: {ex}")

    def _entry_text(self, entry_name: str, fallback: str = "") -> str:
        entry = getattr(self, entry_name, None)
        try:
            if entry is not None and entry.winfo_exists():
                return entry.get()
        except Exception:
            pass
        return fallback

    def _get_server_url(self) -> str:
        fallback = self._server_url_var.get() if hasattr(self, "_server_url_var") else self._server_url_value
        return (self._entry_text("_server_url_entry", fallback) or "https://ai.lol-lu.site").strip().rstrip("/")

    def _get_server_email(self) -> str:
        fallback = self._server_email_var.get() if hasattr(self, "_server_email_var") else self._server_user_email
        return self._entry_text("_server_email_entry", fallback).strip()

    def _get_server_password(self) -> str:
        fallback = self._server_password_var.get() if hasattr(self, "_server_password_var") else ""
        return self._entry_text("_server_password_entry", fallback)

    def _set_account_form_values(self, server_url=None, email=None, password=None):
        if server_url is not None:
            self._server_url_value = str(server_url).strip().rstrip("/") or self._server_url_value
        if email is not None and self._server_mode != "logged_in":
            self._server_user_email = str(email).strip()
        values = {
            "_server_url_entry": (server_url, "_server_url_var"),
            "_server_email_entry": (email, "_server_email_var"),
            "_server_password_entry": (password, "_server_password_var"),
        }
        for entry_name, (value, var_name) in values.items():
            if value is None:
                continue
            value = str(value)
            var = getattr(self, var_name, None)
            if var is not None:
                var.set(value)
            entry = getattr(self, entry_name, None)
            try:
                if entry is not None and entry.winfo_exists():
                    entry.delete(0, tk.END)
                    entry.insert(0, value)
            except Exception:
                pass

    def _save_account_session(self):
        path = self._account_session_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "mode": self._server_mode,
                "server_url": self._server_url_value,
                "email": self._server_user_email,
                "session_token": self._server_session_token if self._server_mode == "logged_in" else "",
                "profile": self._server_account_profile if isinstance(self._server_account_profile, dict) else {},
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            print(f"[Account] save session failed: {ex}")

    def _clear_account_session(self):
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_account_profile = {}
        self._server_mode = "unset"
        if hasattr(self, "_server_email_var"):
            self._server_email_var.set("")
        try:
            path = self._account_session_path()
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def _maybe_show_login_prompt(self):
        """Platform login is opt-in; startup always continues in local mode."""
        return

    def _setup_style(self):
        if CTK_AVAILABLE:
            ctk.set_appearance_mode("light")
            ctk.set_default_color_theme("blue")
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TFrame", background=C["bg"])
        s.configure("Card.TFrame", background=C["card"])
        s.configure("Progress.Horizontal.TProgressbar",
                     background=C["primary"], troughcolor=C["progress_bg"],
                     borderwidth=0, lightcolor=C["primary"], darkcolor=C["primary"])

    def _card(self, parent, **pack_kwargs):
        if CTK_AVAILABLE:
            frame = ctk.CTkFrame(
                parent,
                fg_color=C["card"],
                corner_radius=10,
                border_width=1,
                border_color=C["border2"],
            )
        else:
            frame = RoundedFrame(parent, radius=10, fill=C["card"], outline=C["border2"])
        if pack_kwargs:
            frame.pack(**pack_kwargs)
        return frame

    def _set_window_icon(self):
        icon_png = BASE_DIR / "icon.png"
        icon_ico = GUI_ASSET_DIR / "app.ico"
        try:
            if icon_ico.exists():
                self.iconbitmap(str(icon_ico))
        except Exception as exc:
            print(f"[GUI] iconbitmap failed: {exc}")
        try:
            if icon_png.exists():
                from PIL import Image, ImageTk
                icon = Image.open(icon_png).convert("RGBA").resize((32, 32), Image.LANCZOS)
                self._window_icon_img = ImageTk.PhotoImage(icon)
                self._image_refs.append(self._window_icon_img)
                self.iconphoto(True, self._window_icon_img)
        except Exception as exc:
            print(f"[GUI] iconphoto failed: {exc}")

    def _button(self, parent, text, command, variant="plain", width=None):
        if variant == "primary":
            bg, fg, active = C["primary"], "#ffffff", C["primary2"]
        elif variant == "success":
            bg, fg, active = C["success"], "#ffffff", "#22c55e"
        elif variant == "warn":
            bg, fg, active = C["button"], C["warn"], C["hover"]
        else:
            bg, fg, active = C["button"], C["text"], C["hover"]
        if CTK_AVAILABLE:
            return ctk.CTkButton(
                parent,
                text=text,
                font=F["button"],
                width=width or 76,
                height=30,
                corner_radius=7,
                fg_color=bg,
                hover_color=active,
                text_color=fg,
                border_width=0 if variant in ("primary", "success") else 1,
                border_color=C["border2"],
                command=command,
            )
        return tk.Button(
            parent,
            text=text,
            font=F["button"],
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief="flat",
            bd=0,
            width=width,
            cursor="hand2",
            command=command,
            highlightthickness=1,
            highlightbackground=C["border2"],
            padx=8,
            pady=2,
        )

    def _entry_widget(self, parent, show=None, width=None):
        if CTK_AVAILABLE:
            entry = ctk.CTkEntry(
                parent,
                font=F["small"],
                width=width or 180,
                height=34,
                corner_radius=8,
                fg_color=C["entry"],
                text_color=C["text"],
                border_color=C["border2"],
                border_width=1,
                show=show,
            )
            return entry
        return tk.Entry(
            parent,
            font=F["small"],
            bg=C["entry"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            width=width or 22,
            show=show,
            highlightthickness=1,
            highlightbackground=C["border2"],
        )

    def _section_title(self, parent, title, subtitle=""):
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x")
        tk.Label(row, text=title, font=F["h2"], fg=C["text"], bg=C["card"]).pack(side="left")
        if subtitle:
            tk.Label(row, text=subtitle, font=F["small"], fg=C["muted"], bg=C["card"]).pack(side="right")
        return row

    # ══════════════════════════════════════════════════════
    # 启动序列
    # ══════════════════════════════════════════════════════
    def _run_runtime_probe(self, role: str, args, **kwargs):
        """Launch an environment probe only while runtime replacement is excluded."""
        lock = self._runtime_maintenance_lock
        if not lock.acquire(blocking=False):
            raise RuntimeError("运行环境正在维护")
        try:
            if self._shutting_down or self._runtime_maintenance_in_progress:
                raise RuntimeError("运行环境正在维护")
            return self._process_supervisor.run(role, args, **kwargs)
        finally:
            lock.release()

    def _run_ui_backend_step(self, callback, timeout: float = 20.0) -> bool:
        """Run one Tk-bound lifecycle step and keep its action reservation."""
        completed = threading.Event()
        errors = []

        def invoke():
            try:
                callback()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        try:
            self.after(0, invoke)
        except Exception:
            completed.set()
            raise

        deadline = time.monotonic() + max(1.0, float(timeout))
        while not completed.wait(0.05):
            if self._shutting_down:
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError("界面未能及时执行后台服务操作")
        if errors:
            raise errors[0]
        return not self._shutting_down

    def _request_backend_start(self, action_name: str = "启动后台") -> bool:
        """Start the full environment/backend sequence as one serialized action."""
        if not self._begin_backend_action(action_name):
            return False
        try:
            threading.Thread(
                target=self._backend_start_worker,
                args=(action_name,),
                daemon=True,
            ).start()
        except Exception:
            self._end_backend_action()
            raise
        return True

    def _backend_start_worker(self, action_name: str):
        try:
            self._startup_sequence()
        except Exception as exc:
            self.after(
                0,
                lambda name=action_name, message=str(exc): self._report_backend_failure(
                    f"{name}失败：{message}"
                ),
            )
        finally:
            self._end_backend_action()

    def _startup_sequence(self):
        """主启动序列（后台线程）"""
        if self._shutting_down or self._runtime_maintenance_active():
            return

        # Check the package first so first-time users land in the single
        # environment maintenance center instead of the legacy install page.
        missing = missing_runtime_paths(BASE_DIR)
        if missing:
            result = {
                "package_ready": False,
                "ready": False,
                "missing": missing,
                "gpu_name": "",
                "message": f"运行环境不完整，缺少 {len(missing)} 项",
            }
            def mark_backend_waiting_for_install():
                if self._shutting_down:
                    return
                for key in ("comfyui", "api", "tunnel"):
                    self._set_light(key, "offline", "等待安装环境")
            self.after(0, lambda data=result: self._finish_runtime_recheck(data))
            self.after(0, mark_backend_waiting_for_install)
            self.after(0, self._open_runtime_maintenance)
            return

        self.after(0, lambda: self._set_light("env", "loading", "检查中"))
        system_info = _check_system_env(
            lambda args, **kwargs: self._run_runtime_probe(
                "environment-check", args, **kwargs
            )
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return

        python_exe = BASE_DIR / "runtime" / "python" / "python.exe"
        torch_info = _check_torch(
            python_exe,
            lambda args, **kwargs: self._run_runtime_probe(
                "torch-check", args, **kwargs
            ),
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return

        environment_ready = bool(system_info.get("success") and torch_info.get("success"))
        if not system_info.get("success"):
            message = str(system_info.get("error") or "NVIDIA 显卡环境检查未通过")
            print(f"[Startup] 系统环境异常: {message}")
        elif not torch_info.get("success"):
            message = str(torch_info.get("error") or "PyTorch / CUDA 检查未通过")
            print(f"[Startup] PyTorch 异常: {message}")
        else:
            message = ""
        environment_result = {
            "package_ready": True,
            "ready": environment_ready,
            "missing": [],
            "gpu_name": str(torch_info.get("gpu_name") or system_info.get("gpu_name") or ""),
            "message": message,
        }
        self.after(0, lambda data=environment_result: self._finish_runtime_recheck(data))

        if self._shutting_down or self._runtime_maintenance_active():
            return

        self.after(0, lambda: self._set_light("models", "loading"))
        self._model_status = _check_models_status()
        if self._model_status["all_ok"]:
            self.after(0, lambda: self._set_light("models", "online", "完整"))
        else:
            self.after(0, lambda: self._set_light("models", "offline", "缺失"))
        self.after(0, self._update_model_display)

        # 确保模型路径配置
        _ensure_extra_model_paths()

        if environment_ready and not self._runtime_maintenance_active():
            self._run_ui_backend_step(self._start_backend)
        elif not environment_ready:
            def mark_backend_blocked():
                if self._shutting_down:
                    return
                for key in ("comfyui", "api", "tunnel"):
                    self._set_light(key, "offline", "等待环境修复")
            self.after(0, mark_backend_blocked)

    # ══════════════════════════════════════════════════════
    # 应用外壳：侧栏 + 页面容器
    # ══════════════════════════════════════════════════════
    def _build_app_shell(self):
        self._app_shell = tk.Frame(self, bg=C["bg"])
        self._app_shell.pack(fill="both", expand=True)

        self._sidebar = tk.Frame(
            self._app_shell,
            bg=C["sidebar"],
            width=LAYOUT["sidebar_w"],
            highlightthickness=1,
            highlightbackground=C["sidebar_border"],
        )
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        self._content_root = tk.Frame(self._app_shell, bg=C["bg"])
        self._content_root.pack(side="left", fill="both", expand=True)

    def _build_sidebar(self):
        brand = tk.Frame(self._sidebar, bg=C["sidebar"])
        brand.pack(fill="x", padx=16, pady=(18, 15))

        icon_path = BASE_DIR / "icon.png"
        if icon_path.exists():
            try:
                from PIL import Image, ImageTk
                icon = Image.open(icon_path).convert("RGBA").resize((38, 38), Image.LANCZOS)
                self._sidebar_brand_icon = ImageTk.PhotoImage(icon)
                self._image_refs.append(self._sidebar_brand_icon)
                tk.Label(brand, image=self._sidebar_brand_icon, bg=C["sidebar"]).pack(side="left", padx=(0, 9))
            except Exception:
                tk.Label(brand, text="▷", font=("Microsoft YaHei UI", 20, "bold"), fg=C["primary"], bg=C["sidebar"]).pack(side="left", padx=(0, 10))

        brand_text = tk.Frame(brand, bg=C["sidebar"])
        brand_text.pack(side="left", fill="x", expand=True)
        tk.Label(brand_text, text="灵镜造片厂", font=("Microsoft YaHei UI", 13, "bold"), fg=C["text"], bg=C["sidebar"]).pack(anchor="w")
        tk.Label(brand_text, text="LOCAL AI GATEWAY", font=("Consolas", 7), fg=C["muted"], bg=C["sidebar"]).pack(anchor="w", pady=(2, 0))

        tk.Frame(self._sidebar, bg=C["sidebar_border"], height=1).pack(fill="x", padx=14, pady=(0, 12))
        tk.Label(self._sidebar, text="工作台", font=F["tiny"], fg=C["muted"], bg=C["sidebar"]).pack(anchor="w", padx=22, pady=(0, 7))

        nav_specs = [
            ("overview", "▦", "控制台"),
            ("workflows", "◇", "工作流"),
            ("resources", "▣", "模型与环境"),
            ("settings", "⚙", "设置"),
        ]
        self._nav_buttons = {}
        for page_id, icon, label in nav_specs:
            if CTK_AVAILABLE:
                button = ctk.CTkButton(
                    self._sidebar,
                    text=f"{icon}    {label}",
                    font=F["button"],
                    anchor="w",
                    height=39,
                    corner_radius=8,
                    fg_color="transparent",
                    hover_color=C["hover"],
                    text_color=C["text2"],
                    command=lambda key=page_id: self._show_page(key),
                )
            else:
                button = tk.Button(
                    self._sidebar,
                    text=f"{icon}    {label}",
                    font=F["button"],
                    anchor="w",
                    bg=C["sidebar"],
                    fg=C["text2"],
                    activebackground=C["hover"],
                    activeforeground=C["text"],
                    relief="flat",
                    bd=0,
                    cursor="hand2",
                    command=lambda key=page_id: self._show_page(key),
                )
            button.pack(fill="x", padx=12, pady=2)
            self._nav_buttons[page_id] = button

        tk.Frame(self._sidebar, bg=C["sidebar"]).pack(fill="both", expand=True)

        node = tk.Frame(self._sidebar, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
        node.pack(fill="x", padx=12, pady=(8, 10))
        row = tk.Frame(node, bg=C["hover"])
        row.pack(fill="x", padx=10, pady=(9, 2))
        dot = tk.Canvas(row, width=10, height=10, bg=C["hover"], highlightthickness=0)
        dot.pack(side="left", padx=(0, 7), pady=(2, 0))
        dot.create_oval(1, 1, 9, 9, fill=C["success"], outline="")
        tk.Label(row, text="本机节点", font=F["bold"], fg=C["text"], bg=C["hover"]).pack(side="left")
        tk.Label(node, text="客户端正在运行", font=F["tiny"], fg=C["text2"], bg=C["hover"]).pack(anchor="w", padx=27, pady=(0, 9))

        try:
            version = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
        except Exception:
            version = "dev"
        tk.Label(self._sidebar, text=f"Desktop  {version}", font=("Consolas", 7), fg=C["muted"], bg=C["sidebar"]).pack(anchor="w", padx=18, pady=(0, 14))

    def _build_page_host(self):
        self._page_host = tk.Frame(self._content_root, bg=C["bg"])
        self._page_host.pack(fill="both", expand=True)
        self._pages = {}
        self._current_page_id = ""
        self._overview_page = tk.Frame(self._page_host, bg=C["bg"])
        self._pages["overview"] = self._overview_page

    def _build_static_pages(self):
        self._dashboard_pages = StaticDashboardPages(self, C, F)
        for page_id in ("workflows", "resources", "settings"):
            self._pages[page_id] = self._dashboard_pages.build(self._page_host, page_id)

    def _show_page(self, page_id: str):
        page = self._pages.get(page_id)
        if page is None:
            return
        if self._current_page_id == page_id and page.winfo_ismapped():
            return

        current = self._pages.get(self._current_page_id)
        if current is not None and current.winfo_ismapped():
            current.pack_forget()
        page.pack(fill="both", expand=True)
        self._current_page_id = page_id

        page_meta = {
            "overview": ("控制台", "服务状态、调用信息与当前任务"),
            "workflows": ("工作流", "添加、检查和管理本机生成能力"),
            "resources": ("模型与环境", "准备运行环境并补齐工作流所需模型"),
            "settings": ("设置", "访问密钥、文件位置与软件基础配置"),
        }
        title, subtitle = page_meta[page_id]
        if hasattr(self, "_page_title_label"):
            self._page_title_label.config(text=title)
            self._page_subtitle_label.config(text=subtitle)

        for key, button in self._nav_buttons.items():
            active = key == page_id
            if CTK_AVAILABLE:
                button.configure(
                    fg_color=C["sidebar_active"] if active else "transparent",
                    text_color=C["primary"] if active else C["text2"],
                )
            else:
                button.configure(
                    bg=C["sidebar_active"] if active else C["sidebar"],
                    fg=C["primary"] if active else C["text2"],
                )

    def _open_runtime_maintenance(self):
        """Open the shared maintenance center without starting an install."""
        self._show_page("resources")
        dashboard = getattr(self, "_dashboard_pages", None)
        if dashboard is not None:
            self.after_idle(dashboard.focus_runtime_maintenance)

    # ══════════════════════════════════════════════════════
    # 顶栏：品牌 + 账号信息
    # ══════════════════════════════════════════════════════
    def _build_title_bar(self):
        bar = self._card(self._content_root)
        bar.configure(height=LAYOUT["top_h"])
        bar.pack(fill="x", padx=16, pady=(12, 8))
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=C["surface"])
        left.pack(side="left", padx=18, pady=12)
        tk.Label(left, text="LOCAL AI GATEWAY  /  DESKTOP", font=("Consolas", 7), fg=C["primary"], bg=C["surface"]).pack(anchor="w")
        title_row = tk.Frame(left, bg=C["surface"])
        title_row.pack(anchor="w", pady=(1, 0))
        self._page_title_label = tk.Label(
            title_row,
            text="控制台",
            font=("Microsoft YaHei UI", 16, "bold"),
            fg=C["text"],
            bg=C["surface"],
        )
        self._page_title_label.pack(side="left")
        self._page_subtitle_label = tk.Label(
            title_row,
            text="服务状态、调用信息与当前任务",
            font=F["small"],
            fg=C["muted"],
            bg=C["surface"],
        )
        self._page_subtitle_label.pack(side="left", padx=(12, 0), pady=(4, 0))

        tk.Frame(bar, bg=C["surface"]).pack(side="left", fill="x", expand=True)

        right = tk.Frame(bar, bg=C["surface"])
        right.pack(side="right", padx=16, pady=18)

        self._account_badge = tk.Frame(right, bg=C["surface"], cursor="hand2")
        self._account_badge.pack(side="left", padx=(0, 12))
        self._account_avatar_label = tk.Canvas(
            self._account_badge,
            bg=C["primary"],
            width=34,
            height=34,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._account_avatar_label.pack(side="left", padx=(0, 8))
        self._account_summary_label = tk.Label(
            self._account_badge,
            text="未登录 · 普通用户 · 积分 0",
            font=F["small"],
            fg=C["text"],
            bg=C["surface"],
            cursor="hand2",
        )
        self._account_summary_label.pack(side="left")
        for widget in (self._account_badge, self._account_avatar_label, self._account_summary_label):
            widget.bind("<Button-1>", lambda _event: self._show_account_popup())
        self._render_account_badge()

    # ══════════════════════════════════════════════════════
    # 底部状态灯条
    # ══════════════════════════════════════════════════════
    def _build_bottom_lights(self):
        bar = self._card(self._overview_page, fill="x", padx=LAYOUT["outer"], pady=(4, 8))
        bar.configure(height=LAYOUT["status_h"])
        bar.pack_propagate(False)

        lights = [
            ("server",  "服务器"),
            ("env",     "运行环境"),
            ("comfyui", "ComfyUI"),
            ("tunnel",  "Tunnel"),
            ("api",     "API"),
        ]
        self._light_groups = {}
        self._light_states = {}

        for key, label_text in lights:
            group = tk.Frame(bar, bg=C["surface"])
            group.pack(side="left", fill="x", expand=True, padx=10, pady=9)
            dot = tk.Canvas(group, width=10, height=10, bg=C["surface"], highlightthickness=0)
            dot.pack(side="left", padx=(0, 8), pady=(2, 0))
            dot_id = dot.create_oval(1, 1, 9, 9, fill=C["muted"], outline="")
            lbl = tk.Label(group, text=label_text, font=F["normal"], fg=C["text"], bg=C["surface"])
            lbl.pack(side="left", padx=(0, 6))
            status_lbl = tk.Label(group, text="检测中", font=F["normal"], fg=C["warn"], bg=C["surface"])
            status_lbl.pack(side="left")
            retry_btn = tk.Button(
                group,
                text="重试",
                font=("Microsoft YaHei UI", 8),
                bg=C["hover"],
                fg=C["primary"],
                activebackground=C["border"],
                relief="flat",
                bd=0,
                cursor="hand2",
                command=lambda k=key: self._retry_component(k),
            )

            self._light_groups[key] = (dot, dot_id, lbl, status_lbl, retry_btn)
            self._light_states[key] = "loading"

        # 加载动画在 footer 创建后启动，避免启动早于底部标签。
        self._anim_running = True
        self._anim_frame = 0

    def _animate_loading(self):
        if not self._anim_running:
            return
        self._anim_frame += 1
        dots_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        spinner = dots_chars[self._anim_frame % len(dots_chars)]

        loading_count = sum(1 for s in self._light_states.values() if s == "loading")
        if loading_count > 0:
            self._loading_text.config(text="服务加载中", fg=C["warn"])
            self._loading_dots.config(text=spinner)
            flash_on = (self._anim_frame // 6) % 2 == 0
            for key, state in self._light_states.items():
                if state == "loading":
                    dot, dot_id, _, status_lbl, _ = self._light_groups[key]
                    color = C["warn"] if flash_on else C["border"]
                    dot.itemconfig(dot_id, fill=color)
                    status_lbl.config(text="加载中...", fg=C["warn"] if flash_on else C["text2"])
        else:
            self._loading_text.config(text="就绪", fg=C["success"])
            self._loading_dots.config(text="✓")

        self.after(150, self._animate_loading)

    # ══════════════════════════════════════════════════════
    # URL + Key 卡片
    # ══════════════════════════════════════════════════════
    def _build_info_cards(self):
        self._info_frame = tk.Frame(self._overview_page, bg=C["bg"])
        self._info_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(8, 10))

        # 公网 URL
        tunnel_card = self._card(self._info_frame)
        tunnel_card.configure(height=LAYOUT["info_h"])
        tunnel_card.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tunnel_card.pack_propagate(False)
        url_row = tk.Frame(tunnel_card, bg=C["card"])
        url_row.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(url_row, text="◎", font=("Microsoft YaHei UI", 16, "bold"),
                 fg=C["primary"], bg=C["card"], width=2).pack(side="left", padx=(0, 8))
        text_box = tk.Frame(url_row, bg=C["card"])
        text_box.pack(side="left", fill="x", expand=True)
        tk.Label(text_box, text="公网 URL", font=F["bold"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        self._url_label = tk.Label(text_box, text="等待隧道...", font=F["url"],
                                   fg=C["primary"], bg=C["card"], anchor="w")
        self._url_label.pack(anchor="w", pady=(5, 0))
        url_copy_box = tk.Frame(url_row, bg=C["card"], width=68, height=30)
        url_copy_box.pack(side="right", padx=(10, 0))
        url_copy_box.pack_propagate(False)
        self._button(url_copy_box, "复制", self._copy_public_url, "plain", width=66).pack(fill="both", expand=True)

        # API Key
        key_card = self._card(self._info_frame)
        key_card.configure(height=LAYOUT["info_h"])
        key_card.pack(side="left", fill="x", expand=True)
        key_card.pack_propagate(False)
        key_row = tk.Frame(key_card, bg=C["card"])
        key_row.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(key_row, text="⚿", font=("Microsoft YaHei UI", 18, "bold"),
                 fg=C["primary"], bg=C["card"], width=2).pack(side="left", padx=(0, 8))
        key_text_box = tk.Frame(key_row, bg=C["card"])
        key_text_box.pack(side="left", fill="x", expand=True)
        tk.Label(key_text_box, text="API Key", font=F["bold"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        self._key_label = tk.Label(key_text_box, text="生成中...", font=F["mono"],
                                   fg=C["success"], bg=C["card"], anchor="w")
        self._key_label.pack(anchor="w", pady=(5, 0))
        key_copy_box = tk.Frame(key_row, bg=C["card"], width=68, height=30)
        key_copy_box.pack(side="right", padx=(10, 0))
        key_copy_box.pack_propagate(False)
        self._button(key_copy_box, "复制", self._copy_api_key, "plain", width=66).pack(fill="both", expand=True)

    def _account_summary_text(self) -> str:
        if self._server_mode == "guest":
            return "本地模式"
        email = self._server_user_email or "未登录"
        profile = self._server_account_profile if isinstance(self._server_account_profile, dict) else {}
        user = profile.get("user") if isinstance(profile.get("user"), dict) else profile
        account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
        vip_level = (
            account.get("vipLevel")
            or account.get("vip_level")
            or account.get("vip")
            or user.get("vipLevel")
            or user.get("vip_level")
            or user.get("vip")
        )
        membership_value = (
            account.get("membership")
            or account.get("membershipStatus")
            or account.get("memberLevel")
            or account.get("member_level")
            or user.get("membership")
            or user.get("membershipStatus")
            or user.get("memberLevel")
            or user.get("member_level")
            or "普通用户"
        )
        if isinstance(membership_value, dict):
            membership = str(
                membership_value.get("name")
                or membership_value.get("label")
                or membership_value.get("level")
                or membership_value.get("status")
                or "普通用户"
            )
        else:
            membership = str(membership_value)
        if isinstance(vip_level, bool):
            vip_text = "VIP" if vip_level else membership
        elif vip_level not in (None, ""):
            vip_text = f"VIP {vip_level}"
        else:
            vip_text = membership
        points = (
            account.get("points")
            if account.get("points") not in (None, "")
            else account.get("score", account.get("credits", account.get("remainingCredits", account.get("creditsRemaining", None))))
        )
        if points in (None, ""):
            points = user.get("points", user.get("score", user.get("credits", user.get("remainingCredits", 0))))
        return f"{email} · {vip_text} · 积分 {points}"

    def _draw_account_avatar(self, text: str, color: str):
        if not hasattr(self, "_account_avatar_label"):
            return
        canvas = self._account_avatar_label
        canvas.configure(bg=C["surface"])
        canvas.delete("all")
        RoundedFrame._round_rect(canvas, 1, 1, 33, 33, 8, fill=color, outline=color, width=1)
        canvas.create_text(
            17,
            17,
            text=(text or "登")[:1].upper(),
            fill="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def _render_account_badge(self):
        if not hasattr(self, "_account_summary_label"):
            return
        if self._server_mode == "logged_in":
            avatar = (self._server_user_email[:1] or "账").upper()
            self._draw_account_avatar(avatar, C["primary"])
            self._account_summary_label.config(text=self._account_summary_text(), fg=C["text"])
        elif self._server_mode == "guest":
            self._draw_account_avatar("本", C["success"])
            self._account_summary_label.config(text="本地模式", fg=C["text"])
        else:
            self._draw_account_avatar("登", C["text2"])
            self._account_summary_label.config(text="未登录", fg=C["text2"])

    def _show_account_popup(self):
        """Show account details when logged in; otherwise offer optional login."""
        if self._server_mode != "logged_in" or not self._server_session_token:
            self._show_login_prompt(force=True)
            return

        existing = getattr(self, "_account_popup", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except Exception:
            pass

        popup = tk.Toplevel(self)
        self._account_popup = popup
        popup.title("账号信息")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        self._center_popup(popup, 460, 310)

        def close_popup():
            self._account_popup = None
            try:
                popup.destroy()
            except Exception:
                pass

        popup.protocol("WM_DELETE_WINDOW", close_popup)
        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)

        head = tk.Frame(panel, bg=C["card"])
        head.pack(fill="x", padx=24, pady=(22, 14))
        avatar = tk.Canvas(head, width=48, height=48, bg=C["card"], highlightthickness=0)
        avatar.pack(side="left", padx=(0, 12))
        RoundedFrame._round_rect(avatar, 1, 1, 47, 47, 12, fill=C["primary"], outline=C["primary"])
        avatar.create_text(24, 24, text=(self._server_user_email[:1] or "账").upper(), fill="#fff", font=("Microsoft YaHei UI", 14, "bold"))
        identity = tk.Frame(head, bg=C["card"])
        identity.pack(side="left", fill="x", expand=True)
        tk.Label(identity, text=self._server_user_email or "平台账号", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        account_summary = self._account_summary_text()
        email_prefix = f"{self._server_user_email} · "
        if account_summary.startswith(email_prefix):
            account_summary = account_summary[len(email_prefix):]
        tk.Label(identity, text=account_summary, font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", pady=(4, 0))
        self._badge_for_account_popup = tk.Label(head, text="  已同步  ", font=F["small"], fg=C["success"], bg=C["soft_success"], padx=4, pady=3)
        self._badge_for_account_popup.pack(side="right")

        info = tk.Frame(panel, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
        info.pack(fill="x", padx=24, pady=(0, 16))
        rows = [
            ("平台同步", "已开启"),
            ("本地服务", "退出登录后仍可正常使用"),
            ("公网连接", "由本机 Tunnel 独立提供"),
        ]
        for label, value in rows:
            row = tk.Frame(info, bg=C["hover"])
            row.pack(fill="x", padx=12, pady=6)
            tk.Label(row, text=label, font=F["small"], fg=C["muted"], bg=C["hover"]).pack(side="left")
            tk.Label(row, text=value, font=F["small"], fg=C["text"], bg=C["hover"]).pack(side="right")

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=24, pady=(0, 18))
        self._button(actions, "退出登录", lambda: self._logout_account(close_popup), "plain").pack(fill="x", expand=True)

    def _logout_account(self, close_callback=None):
        server_url = self._server_url_value
        token = self._server_session_token
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_account_profile = {}
        self._server_mode = "guest"
        self._initial_session_sync_done = False
        self._server_password_var.set("")
        self._save_account_session()
        self._render_account_badge()
        self._set_account_status("本地模式：不会连接平台服务端", "success")
        self._set_light("server", "online", "本地模式")
        if callable(close_callback):
            close_callback()
        if token:
            threading.Thread(
                target=self._notify_server_offline,
                kwargs={"server_url": server_url, "token": token},
                daemon=True,
            ).start()

    def _apply_account_visibility(self):
        if not hasattr(self, "_account_frame"):
            return
        if self._server_mode in ("logged_in", "guest"):
            if self._account_frame.winfo_ismapped():
                self._account_frame.pack_forget()
        else:
            if not self._account_frame.winfo_ismapped():
                self._account_frame.pack(fill="x", padx=16, pady=(4, 8), before=self._wf_frame)

    def _hide_account_panel(self):
        if self._account_frame.winfo_ismapped():
            self._account_frame.pack_forget()

    def _toggle_account_panel(self):
        if self._account_frame.winfo_ismapped():
            self._account_frame.pack_forget()
        else:
            self._account_frame.pack(fill="x", padx=16, pady=(4, 8), before=self._wf_frame)

    def _build_account_panel(self):
        self._account_frame = tk.Frame(self, bg=C["card"])
        self._account_frame.pack(fill="x", padx=16, pady=(4, 8))

        header = tk.Frame(self._account_frame, bg=C["card"])
        header.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(header, text="服务端连接", font=F["bold"], fg=C["text"], bg=C["card"]).pack(side="left")
        self._account_status_label = tk.Label(
            header,
            text="游客模式：可复制 URL / Key 给第三方调用",
            font=F["small"],
            fg=C["warn"],
            bg=C["card"],
            anchor="e",
        )
        self._account_status_label.pack(side="right")

        row = tk.Frame(self._account_frame, bg=C["card"])
        row.pack(fill="x", padx=12, pady=(0, 10))

        def field(parent, label, width=22, show=None):
            box = tk.Frame(parent, bg=C["card"])
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(box, text=label, font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w")
            entry = self._entry_widget(box, show=show, width=width * 8 if CTK_AVAILABLE else width)
            entry.pack(fill="x", ipady=4 if not CTK_AVAILABLE else 0)
            return entry

        self._server_url_entry = field(row, "服务端地址", 26)
        self._server_url_entry.insert(0, self._server_url_value or "https://ai.lol-lu.site")
        self._server_email_entry = field(row, "账号邮箱", 20)
        if self._server_user_email:
            self._server_email_entry.insert(0, self._server_user_email)
        self._server_password_entry = field(row, "密码", 18, show="*")

        btn_box = tk.Frame(row, bg=C["card"])
        btn_box.pack(side="left", padx=(0, 0), pady=(16, 0))
        tk.Button(
            btn_box,
            text="登录并同步",
            font=F["small"],
            bg=C["primary"],
            fg="#fff",
            activebackground=C["accent"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._login_and_sync,
        ).pack(side="left", ipadx=8, ipady=4, padx=(0, 6))
        tk.Button(
            btn_box,
            text="游客模式",
            font=F["small"],
            bg=C["surface"],
            fg=C["text"],
            activebackground=C["hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._use_guest_mode,
        ).pack(side="left", ipadx=8, ipady=4)
        tk.Button(
            btn_box,
            text="注册",
            font=F["small"],
            bg=C["surface"],
            fg=C["primary"],
            activebackground=C["hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_register,
        ).pack(side="left", ipadx=8, ipady=4, padx=(6, 0))
        tk.Button(
            btn_box,
            text="收起",
            font=F["small"],
            bg=C["surface"],
            fg=C["text2"],
            activebackground=C["hover"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._hide_account_panel,
        ).pack(side="left", ipadx=8, ipady=4, padx=(6, 0))

    def _build_main_area(self):
        self._main_area = tk.Frame(self._overview_page, bg=C["bg"])
        self._main_area.pack(fill="both", expand=True, padx=LAYOUT["outer"], pady=(0, 12))

        self._left_panel_parent = tk.Frame(self._main_area, bg=C["bg"], width=LAYOUT["left_w"])
        self._left_panel_parent.pack(side="left", fill="y", padx=(0, LAYOUT["gap"]))
        self._left_panel_parent.pack_propagate(False)

        self._right_panel_parent = tk.Frame(self._main_area, bg=C["bg"])
        self._right_panel_parent.pack(side="left", fill="both", expand=True)

    # ══════════════════════════════════════════════════════
    # 进度监控面板
    # ══════════════════════════════════════════════════════
    def _build_progress_panel(self):
        parent = getattr(self, "_right_panel_parent", self)
        self._prog_frame = self._card(parent)
        self._prog_frame.configure(height=300)
        self._prog_frame.pack(fill="both", expand=True)
        self._prog_frame.pack_propagate(False)

        # 任务状态文字行
        task_head = tk.Frame(self._prog_frame, bg=C["card"])
        task_head.pack(fill="x", padx=16, pady=(15, 8))
        self._task_status_label = tk.Label(
            task_head, text="当前任务", font=F["h2"],
            fg=C["text"], bg=C["card"], anchor="w")
        self._task_status_label.pack(side="left", fill="x", expand=True)
        self._preview_output_btn = tk.Button(
            task_head,
            text="预览结果",
            font=F["small"],
            bg=C["primary"],
            fg="#ffffff",
            activebackground=C["accent"],
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_last_output,
        )

        content_row = tk.Frame(self._prog_frame, bg=C["card"])
        content_row.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        progress_col = tk.Frame(content_row, bg=C["card"])
        progress_col.pack(side="left", fill="both", expand=True, padx=(0, 14))

        if CTK_AVAILABLE:
            preview_box = ctk.CTkFrame(
                content_row,
                width=126,
                height=126,
                corner_radius=10,
                fg_color=C["hover"],
                border_width=0,
            )
            preview_bg = C["hover"]
        else:
            preview_box = RoundedFrame(content_row, radius=10, fill=C["hover"], outline=C["hover"], width=126, height=126)
            preview_bg = C["hover"]
        preview_box.pack(side="right", anchor="n", pady=(6, 0))
        preview_box.pack_propagate(False)
        tk.Label(preview_box, text="▧", font=("Microsoft YaHei UI", 34),
                 fg=C["border"], bg=preview_bg).pack(expand=True)

        # 进度信息行
        self._prog_info = tk.Label(
            progress_col,
            text="暂无生成任务。网页端或第三方 API 发起任务后，这里会显示提示词、工作流和进度。",
            font=F["normal"],
            fg=C["text2"],
            bg=C["card"],
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self._prog_info.pack(fill="x", pady=(0, 10))

        self._workflow_info = tk.Label(
            progress_col,
            text="工作流：等待任务",
            font=F["normal"],
            fg=C["text"],
            bg=C["card"],
            anchor="w",
        )
        self._workflow_info.pack(fill="x", pady=(0, 18))

        # 进度条
        bar_row = tk.Frame(progress_col, bg=C["card"])
        bar_row.pack(fill="x", pady=(0, 12))
        self._prog_bar = tk.Canvas(
            bar_row,
            height=26,
            bg=C["card"],
            highlightthickness=0,
        )
        self._prog_bar.pack(fill="x")
        self._prog_bar.bind("<Configure>", lambda _event: self._redraw_progress_bar())
        self._progress_percent = 0
        self._progress_text = "等待任务"

        # 耗时详情
        self._prog_detail = tk.Label(
            progress_col, text="耗时：0s", font=F["small"], fg=C["muted"], bg=C["card"], anchor="w")
        self._prog_detail.pack(fill="x")

        history_head = tk.Frame(progress_col, bg=C["card"])
        history_head.pack(fill="x", pady=(12, 5))
        tk.Label(history_head, text="历史记录", font=F["bold"], fg=C["text"], bg=C["card"]).pack(side="left")
        self._history_hint = tk.Label(
            history_head, text="最近完成的任务", font=F["small"], fg=C["muted"], bg=C["card"])
        self._history_hint.pack(side="right")
        self._history_frame = tk.Frame(progress_col, bg=C["card"])
        self._history_frame.pack(fill="x")
        self._render_task_history()
        self._redraw_progress_bar()

    def _update_task_display(self, task: dict = None):
        """更新任务状态显示"""
        if task and task.get("status") in ("running", "pending", "submitted"):
            context_text = self._task_context_text(task)
            self._current_task_text = f"正在生成：{context_text}"
            self._last_completed_outputs = []
            if self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack_forget()
            # 显示进度面板
            self._show_progress_panel()

            status = task.get("status", "")
            wf_id = task.get("workflow_id", task.get("workflow_name", ""))
            phase = str(task.get("progress_label") or task.get("phase") or "").strip()
            prog = self._as_number(task.get("progress"), 0)
            prog_max = max(self._as_number(task.get("progress_max"), 1), 1)
            percent = self._task_progress_percent(task, prog, prog_max)
            elapsed = task.get("elapsed_seconds", task.get("elapsed", 0))

            self._task_status_label.config(
                text=f"正在生成 - {wf_id}", fg=C["primary"])
            self._prog_info.config(text=context_text)
            self._workflow_info.config(text=f"工作流：{wf_id or '默认工作流'}")
            progress_text = phase or "生成中"
            if prog_max > 1:
                progress_text = f"{progress_text} {int(prog)}/{int(prog_max)} · {int(percent)}%"
            else:
                progress_text = f"{progress_text} · {int(percent)}%"
            self._set_progress_bar(percent, progress_text)
            self._prog_detail.config(text=f"耗时：{elapsed}s")

        elif task and task.get("status") == "completed":
            self._current_task_text = "已完成"
            self._show_progress_panel()
            self._last_completed_outputs = task.get("outputs") or []
            self._last_completed_task_id = str(task.get("task_id") or task.get("id") or "")
            self._task_status_label.config(text="当前任务：已完成", fg=C["success"])
            elapsed = task.get("elapsed_seconds", task.get("elapsed", 0))
            self._prog_info.config(text=self._task_context_text(task))
            self._workflow_info.config(text=f"工作流：{task.get('workflow_id', task.get('workflow_name', '默认工作流'))}")
            self._set_progress_bar(100, "已完成 · 100%")
            self._prog_detail.config(text=f"耗时：{elapsed}s")
            if self._last_completed_outputs and not self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack(side="right", padx=(8, 0), ipadx=8, ipady=3)
            self._remember_task_history(task)
            self.after(15000, self._hide_progress)

        elif task and task.get("status") == "failed":
            self._current_task_text = "失败"
            self._last_completed_outputs = []
            self._last_completed_task_id = ""
            if self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack_forget()
            self._show_progress_panel()
            self._task_status_label.config(text="当前任务：失败", fg=C["error"])
            err = task.get("error", "未知错误")
            self._prog_info.config(text=self._task_context_text(task))
            self._workflow_info.config(text=f"工作流：{task.get('workflow_id', task.get('workflow_name', '默认工作流'))}")
            self._set_progress_bar(0, "失败")
            self._prog_detail.config(text=f"错误：{err}")
            self.after(15000, self._hide_progress)

        elif not task or not task.get("status"):
            self._current_task_text = "无任务"
            self._last_completed_outputs = []
            self._last_completed_task_id = ""
            if self._preview_output_btn.winfo_ismapped():
                self._preview_output_btn.pack_forget()
            self._hide_progress()

    def _hide_progress(self):
        self._task_status_label.config(text="当前任务", fg=C["text"])
        self._prog_info.config(text="暂无生成任务。网页端或第三方 API 发起任务后，这里会显示提示词、工作流和进度。")
        self._workflow_info.config(text="工作流：等待任务")
        self._set_progress_bar(0, "等待任务")
        self._prog_detail.config(text="耗时：0s")

    def _remember_task_history(self, task: dict):
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id or task_id in self._task_history_ids:
            return
        outputs = task.get("outputs") or []
        entry = {
            "task_id": task_id,
            "workflow": str(task.get("workflow_id") or task.get("workflow_name") or "默认工作流"),
            "context": self._task_context_text(task),
            "outputs": outputs,
            "kind": self._task_output_kind(outputs),
            "text": self._task_output_text(outputs),
            "elapsed": task.get("elapsed_seconds", task.get("elapsed", 0)),
        }
        self._task_history_ids.add(task_id)
        self._task_history.insert(0, entry)
        self._task_history = self._task_history[:8]
        self._task_history_ids = {item["task_id"] for item in self._task_history}
        self._render_task_history()

    def _task_output_kind(self, outputs: list) -> str:
        for item in outputs or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            filename = str(item.get("filename") or item.get("file") or item.get("url") or "").lower()
            if item_type in ("image", "video", "text"):
                return item_type
            if filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
                return "image"
            if filename.endswith((".mp4", ".mov", ".webm", ".avi")):
                return "video"
            if item.get("text"):
                return "text"
        return "text"

    def _task_output_text(self, outputs: list) -> str:
        for item in outputs or []:
            if isinstance(item, dict) and item.get("text"):
                return " ".join(str(item.get("text") or "").replace("\r", " ").replace("\n", " ").split())
        return ""

    def _render_task_history(self):
        if not hasattr(self, "_history_frame"):
            return
        for child in self._history_frame.winfo_children():
            child.destroy()
        if not self._task_history:
            tk.Label(
                self._history_frame,
                text="暂无完成记录",
                font=F["small"],
                fg=C["muted"],
                bg=C["card"],
                anchor="w",
            ).pack(fill="x")
            return
        for entry in self._task_history:
            row = tk.Frame(self._history_frame, bg=C["card"])
            row.pack(fill="x", pady=2)
            kind = entry.get("kind") or "text"
            if kind in ("image", "video"):
                label = f"{'图片' if kind == 'image' else '视频'} · {entry.get('workflow')} · {entry.get('context')}"
                tk.Label(
                    row,
                    text=self._short_middle(label, 42, 18),
                    font=F["small"],
                    fg=C["text2"],
                    bg=C["card"],
                    anchor="w",
                ).pack(side="left", fill="x", expand=True)
                self._button(
                    row,
                    "预览",
                    lambda outputs=list(entry.get("outputs") or []), task_id=entry.get("task_id", ""): self._open_outputs_for_items(outputs, task_id),
                    "plain",
                ).pack(side="right", ipadx=8, ipady=2)
            else:
                snippet = entry.get("text") or entry.get("context") or "文本任务已完成"
                label = f"文本 · {entry.get('workflow')}：{snippet}"
                tk.Label(
                    row,
                    text=self._short_middle(label, 58, 20),
                    font=F["small"],
                    fg=C["text2"],
                    bg=C["card"],
                    anchor="w",
                    justify="left",
                ).pack(fill="x")

    def _show_progress_panel(self):
        if not self._prog_frame.winfo_ismapped():
            self._prog_frame.pack(fill="both", expand=True)

    def _as_number(self, value, default=0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _task_progress_percent(self, task: dict, progress: float, progress_max: float) -> float:
        raw = task.get("progress_percent", task.get("progressPercent", task.get("percent", "")))
        percent = self._as_number(raw, None)
        if percent is None:
            percent = (progress / progress_max) * 100 if progress_max else 0
        elif percent <= 1:
            percent *= 100
        return max(0, min(100, percent))

    def _task_context_text(self, task: dict) -> str:
        title = str(task.get("title") or task.get("scene_title") or task.get("workflow_name") or task.get("workflow_id") or "").strip()
        prompt = str(task.get("prompt_summary") or task.get("prompt") or task.get("promptText") or "").strip()
        prompt = " ".join(prompt.replace("\r", " ").replace("\n", " ").split())
        if title and prompt:
            return self._short_middle(f"{title}：{prompt}", 62, 28)
        if prompt:
            return self._short_middle(prompt, 72, 28)
        return title or "本地生成任务"

    def _set_progress_bar(self, percent: float, text: str):
        self._progress_percent = max(0, min(100, float(percent or 0)))
        self._progress_text = str(text or "")
        self._redraw_progress_bar()

    def _redraw_progress_bar(self):
        if not hasattr(self, "_prog_bar"):
            return
        width = max(1, self._prog_bar.winfo_width())
        height = max(1, self._prog_bar.winfo_height())
        fill_width = int(width * self._progress_percent / 100)
        self._prog_bar.delete("all")
        radius = max(8, height // 2)
        RoundedFrame._round_rect(
            self._prog_bar,
            0,
            0,
            width,
            height,
            radius,
            fill=C["progress_bg"],
            outline=C["border2"],
        )
        if fill_width > 0:
            RoundedFrame._round_rect(
                self._prog_bar,
                0,
                0,
                max(fill_width, radius * 2),
                height,
                radius,
                fill=C["primary"],
                outline="",
            )
        self._prog_bar.create_text(width / 2, height / 2, text=self._progress_text, fill="#ffffff" if fill_width > width * 0.45 else C["text"], font=F["small"])

    # ══════════════════════════════════════════════════════
    # 工作流 + 模型状态面板
    # ══════════════════════════════════════════════════════
    def _build_workflow_model_panel(self):
        parent = getattr(self, "_left_panel_parent", self)
        self._wf_frame = self._card(parent)
        self._wf_frame.pack(fill="both", expand=True)

        # 标题行
        hdr = tk.Frame(self._wf_frame, bg=C["card"], height=30)
        hdr.pack(fill="x", padx=16, pady=(14, 7))
        hdr.pack_propagate(False)

        tk.Label(hdr, text="工作流 / 模型", font=F["h2"], fg=C["text"], bg=C["card"]).pack(side="left")
        self._button(hdr, "上传工作流", self._show_workflow_upload_dialog, "plain", width=72).pack(side="right")
        self._model_hint_label = tk.Label(
            hdr,
            text="",
            font=F["small"],
            fg=C["muted"],
            bg=C["card"],
        )
        self._model_hint_label.pack_forget()

        self._wf_rows = {}
        self._wf_sections = {}

        def section(title):
            box = tk.Frame(self._wf_frame, bg=C["card"])
            box.pack(fill="x", padx=16, pady=(7, 4))
            head = tk.Frame(box, bg=C["card"])
            head.pack(fill="x", pady=(0, 3))
            tk.Label(head, text="⌄", font=F["small"], fg=C["text2"], bg=C["card"]).pack(side="left", padx=(0, 6))
            tk.Label(head, text=title, font=F["bold"], fg=C["text"], bg=C["card"]).pack(side="left")
            return box

        self._wf_sections = {
            "image": section("图片模型"),
            "video": section("视频模型"),
            "text": section("文字模型"),
        }
        self._update_model_display()

    def _update_model_display(self):
        """更新工作流面板中的模型状态。"""
        self._update_workflow_display(self._last_health or {})

    def _workflow_group_for_display(self, workflow: dict) -> str:
        text = " ".join([
            str(workflow.get("type") or ""),
            str(workflow.get("output_type") or ""),
            str(workflow.get("id") or ""),
            str(workflow.get("name") or ""),
        ]).lower()
        if "video" in text or "flf2v" in text:
            return "video"
        if "text" in text or "chat" in text or "llm" in text:
            return "text"
        return "image"

    def _workflow_model_key(self, workflow: dict) -> str:
        text = " ".join([
            str(workflow.get("id") or ""),
            str(workflow.get("name") or ""),
            str(workflow.get("type") or ""),
            str(workflow.get("output_type") or ""),
        ]).lower()
        if "wan" in text or "wan2.1" in text:
            return "Wan2.1"
        if "flux" in text:
            return "Flux2"
        if "qwen" in text or "千问" in text:
            return "Qwen3.5"
        return ""

    def _workflow_display_name(self, workflow: dict) -> str:
        return str(workflow.get("name") or workflow.get("label") or workflow.get("id") or "未命名工作流").strip()

    def _workflow_status_text(self, workflow: dict, available: bool) -> str:
        if not workflow.get("enabled", True):
            return "已停用"
        if available:
            return "可用"
        key = self._workflow_model_key(workflow)
        missing_count = len(self._missing_model_items(key)) if key else 0
        return f"缺失 {missing_count} 个模型" if missing_count else "缺失模型"

    def _add_workflow_row(self, parent_box, workflow: dict, group: str):
        wf_id = str(workflow.get("id") or "").strip()
        title = self._workflow_display_name(workflow)
        available = workflow.get("enabled", True) and self._workflow_model_available(workflow)
        model_key = self._workflow_model_key(workflow)

        row = tk.Frame(parent_box, bg=C["card"])
        row.pack(fill="x", pady=4)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=0)
        row.columnconfigure(2, weight=0)
        row.columnconfigure(3, weight=0)

        if CTK_AVAILABLE:
            radio = ctk.CTkRadioButton(
                row,
                text=title,
                value=wf_id or "default",
                variable=self._workflow_mode,
                font=F["normal"],
                text_color=C["primary"] if available else C["muted"],
                fg_color=C["primary"],
                hover_color=C["primary2"],
                border_color=C["border"],
                state="normal" if available else "disabled",
                radiobutton_width=16,
                radiobutton_height=16,
                width=92,
            )
        else:
            radio = tk.Radiobutton(
                row,
                text=title,
                value=wf_id or "default",
                variable=self._workflow_mode,
                font=F["normal"],
                fg=C["primary"] if available else C["muted"],
                bg=C["card"],
                activeforeground=C["primary"],
                activebackground=C["card"],
                selectcolor=C["surface"],
                relief="flat",
                bd=0,
                cursor="hand2" if available else "arrow",
                disabledforeground=C["muted"],
                state="normal" if available else "disabled",
            )
        radio.grid(row=0, column=0, sticky="w")

        dot = tk.Canvas(row, width=9, height=9, bg=C["card"], highlightthickness=0)
        dot.grid(row=0, column=1, padx=(3, 6), pady=(7, 0), sticky="w")
        dot.create_oval(1, 1, 8, 8, fill=C["success"] if available else C["error"], outline="")

        status_text = self._workflow_status_text(workflow, available)
        status_lbl = tk.Label(
            row,
            text=status_text,
            font=F["small"],
            fg=C["success"] if available else C["error"],
            bg=C["card"],
            anchor="e",
            justify="right",
            wraplength=92,
        )

        if available:
            params_btn = self._button(row, "查看参数", lambda wf=dict(workflow): self._show_workflow_schema(wf), "plain", width=70)
            params_btn.grid(row=0, column=2, padx=(4, 6), sticky="e")

        if not available and model_key:
            help_btn = self._button(row, "查看", lambda key=model_key: self._show_model_install_help(key), "plain", width=54)
            help_btn.grid(row=0, column=2, padx=(4, 6), sticky="e")

        status_lbl.grid(row=0, column=3, sticky="e")

        if wf_id:
            self._wf_rows[wf_id] = row

    def _mirror_model_url(self, url: str) -> str:
        url = str(url or "").strip()
        if url.startswith("https://huggingface.co/"):
            return url.replace("https://huggingface.co/", "https://hf-mirror.com/", 1)
        return url

    def _missing_model_items(self, model_key: str) -> list:
        spec = MODEL_REQUIREMENTS.get(model_key, {})
        items = []
        models_dir = BASE_DIR / "models"
        for item in spec.get("items", []):
            rel_path = item.get("path", "")
            if rel_path and not _model_file_ready(models_dir / rel_path, item.get("size_bytes")):
                items.append(item)
        return items

    def _show_model_install_help(self, model_key: str):
        missing = self._missing_model_items(model_key)
        popup_w = 840
        popup_h = 600 if len(missing) >= 4 else 520
        popup = tk.Toplevel(self)
        popup.title("缺失模型")
        popup.geometry(f"{popup_w}x{popup_h}")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        self._center_popup(popup, popup_w, popup_h)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        title = MODEL_REQUIREMENTS.get(model_key, {}).get("title", model_key)
        tk.Label(panel, text=f"{title}：缺失模型", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=18, pady=(16, 4))
        tk.Label(panel, text="选择需要补齐的模型，客户端会在后台下载并放入对应目录。下载完成后会自动重新检测模型状态。",
                 font=F["normal"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=18, pady=(0, 10))

        list_outer = tk.Frame(panel, bg=C["card"])
        list_outer.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        if len(missing) <= 4:
            rows = tk.Frame(list_outer, bg=C["card"])
            rows.pack(fill="both", expand=True)
        else:
            canvas = tk.Canvas(list_outer, bg=C["card"], highlightthickness=0, bd=0)
            scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
            rows = tk.Frame(canvas, bg=C["card"])
            rows.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas_window = canvas.create_window((0, 0), window=rows, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            canvas.bind("<Configure>", lambda event: canvas.itemconfig(canvas_window, width=event.width))

        download_controls = []
        abandon_started = False
        if not missing:
            empty = self._card(rows)
            empty.pack(fill="x", pady=(0, 10))
            tk.Label(empty, text="模型文件已完整，无需下载。", font=F["body"], fg=C["success"], bg=C["card"]).pack(anchor="w", padx=16, pady=18)
        for index, item in enumerate(missing, 1):
            control = self._build_model_download_row(rows, model_key, item, index)
            download_controls.append(control)

        def abandon_paused_and_close():
            nonlocal abandon_started
            paused = [control for control in download_controls if control.get("state") == "paused"]
            if not paused:
                if not any(control.get("state") == "abandoning" for control in download_controls):
                    popup.destroy()
                return
            if abandon_started:
                return
            abandon_started = True
            try:
                popup.grab_release()
            except Exception:
                pass
            popup.withdraw()
            remaining = {id(control) for control in paused}

            def one_finished(control):
                remaining.discard(id(control))
                if not remaining:
                    try:
                        popup.destroy()
                    except Exception:
                        pass

            for control in paused:
                self._abandon_paused_model_download(
                    control,
                    lambda item=control: one_finished(item),
                )

        def cleanup_hidden_popup():
            try:
                if not popup.winfo_exists():
                    return
                if any(
                    control.get("state") in {"downloading", "resuming"}
                    for control in download_controls
                ):
                    popup.after(600, cleanup_hidden_popup)
                    return
                if any(control.get("state") in {"paused", "abandoning"} for control in download_controls):
                    abandon_paused_and_close()
                    return
                popup.destroy()
            except Exception:
                pass

        def close_popup():
            if any(
                control.get("state") in {"downloading", "resuming"}
                for control in download_controls
            ):
                try:
                    popup.grab_release()
                except Exception:
                    pass
                popup.withdraw()
                self._footer_label.config(text="  模型继续在后台下载，完成后会自动重新检查")
                popup.after(600, cleanup_hidden_popup)
                return
            if any(control.get("state") in {"paused", "abandoning"} for control in download_controls):
                abandon_paused_and_close()
                return
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", close_popup)

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=18, pady=(0, 14))

        def download_all():
            for control in download_controls:
                if control.get("state") != "done":
                    self._start_model_download(control)

        if download_controls:
            self._button(actions, "下载全部缺失", download_all, "primary", width=110).pack(side="left", ipadx=12, ipady=6)
        self._button(actions, "打开模型目录", self._open_models, "plain").pack(side="left", ipadx=12, ipady=6, padx=(10, 0))
        self._button(actions, "关闭", close_popup, "plain").pack(side="right", ipadx=12, ipady=6)

    def _build_model_download_row(self, parent, model_key: str, item: dict, index: int) -> dict:
        rel_path = str(item.get("path") or "").strip()
        url = self._mirror_model_url(item.get("url", ""))
        target = BASE_DIR / "models" / rel_path
        filename = Path(rel_path).name or f"model_{index}"
        rel_parent = Path(rel_path).parent if rel_path else Path("")
        display_path = str(Path("models") / rel_parent) if str(rel_parent) not in ("", ".") else "models"

        card = self._card(parent)
        card.pack(fill="x", pady=(0, 8))
        card.grid_columnconfigure(1, weight=3)
        card.grid_columnconfigure(2, weight=2)

        tk.Label(card, text=f"{index}", font=F["bold"], fg=C["primary"], bg=C["card"], width=3).grid(row=0, column=0, rowspan=3, padx=(12, 6), pady=10, sticky="n")
        tk.Label(card, text="镜像名称", font=F["small"], fg=C["text2"], bg=C["card"]).grid(row=0, column=1, sticky="w", pady=(9, 0))
        tk.Label(card, text=filename, font=F["bold"], fg=C["text"], bg=C["card"], anchor="w").grid(row=1, column=1, sticky="ew", pady=(1, 0))

        url_row = tk.Frame(card, bg=C["card"])
        url_row.grid(row=2, column=1, sticky="ew", pady=(7, 9))
        tk.Label(url_row, text="下载地址", font=F["small"], fg=C["text2"], bg=C["card"]).pack(side="left")
        url_label = tk.Label(url_row, text=self._short_middle(url, 34, 16), font=F["url"], fg=C["primary"], bg=C["card"], anchor="w", cursor="hand2")
        url_label.pack(side="left", padx=(10, 0), fill="x", expand=True)
        url_label.bind("<Button-1>", lambda _event, u=url: webbrowser.open(u))

        right = tk.Frame(card, bg=C["card"])
        right.grid(row=0, column=2, rowspan=3, sticky="nsew", padx=(12, 12), pady=9)
        top_line = tk.Frame(right, bg=C["card"])
        top_line.pack(fill="x")
        tk.Label(top_line, text="存放位置", font=F["small"], fg=C["text2"], bg=C["card"]).pack(side="left")
        path_label = tk.Label(top_line, text=display_path, font=F["small"], fg=C["text"], bg=C["card"], anchor="e")
        path_label.pack(side="right", fill="x", expand=True)

        progress_var = tk.DoubleVar(value=0)
        progress = ttk.Progressbar(right, orient="horizontal", mode="determinate", maximum=100, variable=progress_var, style="Progress.Horizontal.TProgressbar")
        progress.pack(fill="x", pady=(8, 4))
        status_var = tk.StringVar(value="等待下载")
        status = tk.Label(right, textvariable=status_var, font=F["small"], fg=C["text2"], bg=C["card"], anchor="w")
        status.pack(side="left", fill="x", expand=True)

        control = {
            "model_key": model_key,
            "item": item,
            "url": url,
            "target": target,
            "progress_var": progress_var,
            "status_var": status_var,
            "state": "idle",
            "pause_event": threading.Event(),
            "stop_event": threading.Event(),
            "worker": None,
        }
        button_row = tk.Frame(right, bg=C["card"])
        button_row.pack(side="right", padx=(10, 0))
        pause_button = self._button(button_row, "暂停", lambda c=control: self._pause_model_download(c), "plain", width=56)
        pause_button.pack(side="left", padx=(0, 6))
        pause_button.configure(state="disabled")
        button = self._button(button_row, "下载", lambda c=control: self._start_model_download(c), "primary", width=72)
        if not url:
            button.configure(state="disabled", text="无地址")
        button.pack(side="left")
        control["button"] = button
        control["pause_button"] = pause_button
        if self._model_transfer_active(target):
            control["state"] = "attached"
            control["status_var"].set("此模型已在另一个窗口后台下载")
            button.configure(state="disabled", text="后台下载中")
            pause_button.configure(state="disabled")
        return control

    def _model_transfer_key(self, target: Path) -> str:
        try:
            return os.path.normcase(str(Path(target).resolve()))
        except OSError:
            return os.path.normcase(str(Path(target).absolute()))

    def _model_transfer_state(self):
        lock = self.__dict__.get("_model_transfer_lock")
        if lock is None:
            lock = threading.RLock()
            self.__dict__["_model_transfer_lock"] = lock
        transfers = self.__dict__.setdefault("_active_model_transfers", {})
        return lock, transfers

    def _model_transfer_active(self, target: Path) -> bool:
        lock, transfers = self._model_transfer_state()
        with lock:
            return self._model_transfer_key(target) in transfers

    def _claim_model_transfer(self, control: dict) -> bool:
        lock, transfers = self._model_transfer_state()
        key = self._model_transfer_key(control["target"])
        with lock:
            if self.__dict__.get("_model_import_in_progress", False):
                return False
            owner = transfers.get(key)
            if owner is not None and owner is not control:
                return False
            transfers[key] = control
            return True

    def _release_model_transfer(self, control: dict):
        target = control.get("target")
        if target is None:
            return
        lock, transfers = self._model_transfer_state()
        key = self._model_transfer_key(target)
        with lock:
            if transfers.get(key) is control:
                transfers.pop(key, None)

    def _abandon_paused_model_download(self, control: dict, on_done=None):
        """Release a paused target only after its writer thread has exited."""
        if control.get("state") != "paused":
            if control.get("state") != "abandoning" and on_done:
                on_done()
            return
        control["state"] = "abandoning"
        control["status_var"].set("正在安全结束暂停任务...")
        button = control.get("button")
        pause_button = control.get("pause_button")
        if button:
            button.configure(state="disabled", text="结束中")
        if pause_button:
            pause_button.configure(state="disabled")
        worker = control.get("worker")

        def wait_for_writer():
            try:
                if worker and worker.is_alive():
                    worker.join()
            finally:
                def finish_abandon():
                    self._release_model_transfer(control)
                    if control.get("state") == "abandoning":
                        control["state"] = "abandoned"
                    if on_done:
                        on_done()

                if self._shutting_down:
                    self._release_model_transfer(control)
                else:
                    self.after(0, finish_abandon)

        threading.Thread(target=wait_for_writer, daemon=True).start()

    def _has_active_model_transfers(self) -> bool:
        lock, transfers = self._model_transfer_state()
        with lock:
            return bool(transfers)

    def _start_model_download(self, control: dict):
        if control.get("state") in {"downloading", "resuming", "abandoning", "attached"}:
            return
        if not self._claim_model_transfer(control):
            control["status_var"].set("另一个模型维护任务正在运行，请稍候")
            button = control.get("button")
            pause_button = control.get("pause_button")
            if button:
                button.configure(state="normal", text="稍后重试")
            if pause_button:
                pause_button.configure(state="disabled")
            return
        if control.get("state") == "paused":
            control["state"] = "resuming"
            button = control.get("button")
            pause_button = control.get("pause_button")
            if button:
                button.configure(state="disabled", text="恢复中")
            if pause_button:
                pause_button.configure(state="disabled", text="暂停")
            control["status_var"].set("正在安全恢复下载...")
            previous_worker = control.get("worker")

            def wait_then_resume():
                if previous_worker and previous_worker.is_alive():
                    previous_worker.join()
                if not self._shutting_down:
                    self.after(0, lambda: self._resume_model_download(control))

            threading.Thread(target=wait_then_resume, daemon=True).start()
            return
        control["state"] = "downloading"
        pause_event = control.get("pause_event")
        stop_event = control.get("stop_event")
        if pause_event:
            pause_event.clear()
        if stop_event:
            stop_event.clear()
        button = control.get("button")
        pause_button = control.get("pause_button")
        if button:
            button.configure(state="disabled", text="下载中")
        if pause_button:
            pause_button.configure(state="normal", text="暂停")
        control["status_var"].set("准备下载...")
        worker = threading.Thread(target=lambda: self._download_model_file(control), daemon=True)
        control["worker"] = worker
        worker.start()

    def _resume_model_download(self, control: dict):
        """Start a fresh worker only after the paused worker has fully exited."""
        if self._shutting_down or control.get("state") != "resuming":
            return
        pause_event = control.get("pause_event")
        if pause_event:
            pause_event.clear()
        control["state"] = "downloading"
        button = control.get("button")
        pause_button = control.get("pause_button")
        if button:
            button.configure(state="disabled", text="下载中")
        if pause_button:
            pause_button.configure(state="normal", text="暂停")
        control["status_var"].set("继续下载...")
        worker = threading.Thread(target=lambda: self._download_model_file(control), daemon=True)
        control["worker"] = worker
        worker.start()

    def _pause_model_download(self, control: dict):
        if control.get("state") != "downloading":
            return
        pause_event = control.get("pause_event")
        if pause_event:
            pause_event.set()
        control["state"] = "paused"
        control["status_var"].set("已暂停，点击继续可断点续传")
        button = control.get("button")
        pause_button = control.get("pause_button")
        if button:
            button.configure(state="normal", text="继续")
        if pause_button:
            pause_button.configure(state="disabled", text="暂停")

    def _download_model_file(self, control: dict):
        import urllib.request as ur
        url = control["url"]
        target: Path = control["target"]
        part_path = target.with_suffix(target.suffix + ".part")
        meta_path = part_path.with_suffix(part_path.suffix + ".json")
        expected_size = int((control.get("item") or {}).get("size_bytes") or 0)
        max_retries = 3

        def discard_partial():
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

        def load_resume_metadata() -> dict:
            if not part_path.is_file() or not meta_path.is_file():
                return {}
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            if not isinstance(data, dict):
                return {}
            try:
                valid = (
                    str(data.get("url") or "") == url
                    and int(data.get("expected_size") or 0) == expected_size
                    and bool(str(data.get("validator") or "").strip())
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                return {}
            return data

        def response_validator(headers) -> str:
            etag = str(headers.get("ETag") or "").strip()
            if etag and not etag.startswith("W/"):
                return etag
            return str(headers.get("Last-Modified") or "").strip()

        def save_resume_metadata(validator: str):
            if not validator:
                meta_path.unlink(missing_ok=True)
                return
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "expected_size": expected_size,
                        "validator": validator,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            resume_metadata = load_resume_metadata()
            if part_path.exists() and not resume_metadata:
                discard_partial()
            if expected_size and resume_metadata and model_file_ready(part_path, expected_size):
                os.replace(part_path, target)
                meta_path.unlink(missing_ok=True)
                self.after(0, lambda: self._finish_model_download(control))
                return
            if expected_size and part_path.exists() and part_path.stat().st_size > expected_size:
                discard_partial()
            attempt = 0
            while True:
                pause_event = control.get("pause_event")
                if pause_event and pause_event.is_set():
                    self.after(0, lambda: control["status_var"].set("已暂停，点击继续可断点续传"))
                    return

                resume_metadata = load_resume_metadata()
                if part_path.exists() and not resume_metadata:
                    discard_partial()
                resume_from = part_path.stat().st_size if part_path.exists() else 0
                headers = {"User-Agent": "lingjing-model-downloader/1.0"}
                if resume_from:
                    headers["Range"] = f"bytes={resume_from}-"
                    headers["If-Range"] = str(resume_metadata["validator"])
                req = ur.Request(url, headers=headers)
                try:
                    with ur.urlopen(req, timeout=30) as resp:
                        status_code = int(getattr(resp, "status", 200) or 200)
                        validator = response_validator(resp.headers)
                        if resume_from and status_code == 200:
                            discard_partial()
                            resume_from = 0
                        content_length = int(resp.headers.get("Content-Length") or 0)
                        if status_code == 206:
                            content_range = str(resp.headers.get("Content-Range") or "").strip()
                            match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
                            if not resume_from or not match:
                                discard_partial()
                                raise IOError("服务器返回了无效的断点续传范围")
                            start, end = int(match.group(1)), int(match.group(2))
                            total_value = match.group(3)
                            if start != resume_from or end < start:
                                discard_partial()
                                raise IOError("服务器返回的断点位置与本地文件不一致")
                            if content_length and content_length != end - start + 1:
                                discard_partial()
                                raise IOError("服务器返回的分段长度无效")
                            if expected_size and (
                                total_value == "*" or int(total_value) != expected_size
                            ):
                                discard_partial()
                                raise IOError("服务器返回的模型总大小与清单不一致")
                            if validator and validator != str(resume_metadata.get("validator") or ""):
                                discard_partial()
                                raise IOError("下载源文件已发生变化，正在重新下载")
                            total = int(total_value) if total_value != "*" else 0
                        else:
                            if expected_size and content_length and content_length != expected_size:
                                discard_partial()
                                raise IOError(
                                    f"模型文件大小不匹配（应为 {expected_size} 字节，服务器返回 {content_length} 字节）"
                                )
                            total = content_length
                            save_resume_metadata(validator)
                        done = resume_from
                        mode = "ab" if resume_from else "wb"
                        with open(part_path, mode) as f:
                            while True:
                                pause_event = control.get("pause_event")
                                if pause_event and pause_event.is_set():
                                    self.after(0, lambda: control["status_var"].set("已暂停，点击继续可断点续传"))
                                    return
                                chunk = resp.read(1024 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                                done += len(chunk)
                                if total:
                                    percent = min(100, done * 100 / total)
                                    self.after(0, lambda p=percent, d=done, t=total: self._update_model_download_progress(control, p, d, t))
                                else:
                                    self.after(0, lambda d=done: control["status_var"].set(f"已下载 {d / 1024 / 1024:.1f} MB"))
                        if total and done != total:
                            raise IOError(f"下载不完整（应接收 {total} 字节，实际 {done} 字节）")
                        if expected_size and done != expected_size:
                            if status_code == 200 or done > expected_size:
                                discard_partial()
                            raise IOError(
                                f"模型文件大小不匹配（应为 {expected_size} 字节，实际 {done} 字节）"
                            )
                    break
                except Exception:
                    attempt += 1
                    if attempt > max_retries:
                        raise
                    self.after(0, lambda n=attempt: control["status_var"].set(f"网络中断，正在重连 {n}/{max_retries}..."))
                    time.sleep(min(2 * attempt, 6))
            if not model_file_ready(part_path, expected_size or None):
                raise IOError("模型文件校验未通过，已保留断点文件")
            os.replace(part_path, target)
            meta_path.unlink(missing_ok=True)
            self.after(0, lambda: self._finish_model_download(control))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._fail_model_download(control, e))

    def _update_model_download_progress(self, control: dict, percent: float, done: int, total: int):
        control["progress_var"].set(percent)
        control["status_var"].set(f"{percent:.0f}%  {done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB")

    def _finish_model_download(self, control: dict):
        self._release_model_transfer(control)
        control["state"] = "done"
        control["progress_var"].set(100)
        control["status_var"].set("下载完成，已放入模型目录")
        button = control.get("button")
        pause_button = control.get("pause_button")
        if button:
            button.configure(state="disabled", text="已完成")
        if pause_button:
            pause_button.configure(state="disabled", text="暂停")
        self._footer_label.config(text="  模型下载完成，正在重新检测...")
        threading.Thread(target=self._recheck_models, daemon=True).start()

    def _fail_model_download(self, control: dict, error: str):
        self._release_model_transfer(control)
        control["state"] = "failed"
        control["status_var"].set(f"下载失败，已保留断点：{error}")
        button = control.get("button")
        pause_button = control.get("pause_button")
        if button:
            button.configure(state="normal", text="重试")
        if pause_button:
            pause_button.configure(state="disabled", text="暂停")
        self._footer_label.config(text="  模型下载失败，请检查网络或稍后重试")

    def _open_workflows_dir(self):
        workflows_dir = BASE_DIR / "workflows"
        workflows_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(workflows_dir))

    def _workflow_slug(self, value: str) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_-")
        if not text:
            text = f"workflow_{int(time.time())}"
        return text[:64]

    def _unique_workflow_id(self, base_id: str) -> str:
        workflows_dir = BASE_DIR / "workflows"
        base_id = self._workflow_slug(base_id)
        candidate = base_id
        index = 2
        while (workflows_dir / candidate).exists():
            candidate = f"{base_id}_{index}"
            index += 1
        return candidate

    def _workflow_manifest_type(self, output_type: str) -> str:
        output_type = str(output_type or "").lower()
        if output_type == "video":
            return "video.first_last_to_video"
        if output_type == "text":
            return "text.chat"
        return "image.text_to_image"

    def _infer_workflow_output_type(self, name: str, data: dict, manifest: dict = None) -> str:
        manifest = manifest or {}
        raw_type = str(manifest.get("type") or manifest.get("output_type") or "").lower()
        if "video" in raw_type or "flf2v" in raw_type or "i2v" in raw_type:
            return "video"
        if "text" in raw_type or "chat" in raw_type:
            return "text"
        if "image" in raw_type or "t2i" in raw_type:
            return "image"

        text = str(name or "").lower()
        if any(token in text for token in ("wan", "flf2v", "i2v", "video", "首尾帧", "视频")):
            return "video"
        if any(token in text for token in ("text", "chat", "qwen", "deepseek", "llm", "文字")):
            return "text"

        class_text = " ".join(
            str(node.get("class_type", "")).lower()
            for node in data.values()
            if isinstance(node, dict)
        )
        if any(token in class_text for token in ("wan", "videocombine", "vhs_", "saveanimated")):
            return "video"
        if "saveimage" in class_text or "ksampler" in class_text or "flux" in class_text:
            return "image"
        return "image"

    def _workflow_description_for_type(self, output_type: str) -> str:
        if output_type == "video":
            return "首帧+尾帧转视频工作流，输入 prompt、start_image、end_image 后输出分镜视频"
        if output_type == "text":
            return "文字生成工作流，输入 prompt 后输出脚本、分镜或结构化文本"
        return "文生图工作流，输入 prompt 后输出分镜图片"

    def _workflow_input_schema_for_type(self, output_type: str) -> dict:
        output_type = str(output_type or "").lower()
        if output_type == "video":
            return {
                "summary": "输入文字动作提示词、首帧图片、尾帧图片，生成分镜视频。",
                "required": ["prompt", "start_image", "end_image"],
                "optional": ["duration", "fps", "seed"],
                "response": {"type": "video", "format": "url"},
                "inputs": [
                    {"name": "prompt", "type": "text", "label": "动作/镜头提示词", "required": True},
                    {"name": "start_image", "type": "image", "label": "首帧图片", "required": True},
                    {"name": "end_image", "type": "image", "label": "尾帧图片", "required": True},
                    {"name": "duration", "type": "number", "label": "时长秒", "required": False},
                    {"name": "seed", "type": "integer", "label": "随机种子", "required": False},
                ],
            }
        if output_type == "text":
            return {
                "summary": "输入文字需求，生成脚本、角色或分镜结构化 JSON。",
                "required": ["prompt"],
                "optional": ["messages", "response_format"],
                "response": {"type": "json", "format": "json_object"},
                "inputs": [
                    {"name": "prompt", "type": "text", "label": "文字需求", "required": True},
                    {"name": "messages", "type": "messages", "label": "聊天消息", "required": False},
                    {"name": "response_format", "type": "object", "label": "响应格式", "required": False, "default": {"type": "json_object"}},
                ],
            }
        return {
            "summary": "输入文字提示词，生成分镜图片。",
            "required": ["prompt"],
            "optional": ["negative_prompt", "size", "width", "height", "steps", "seed"],
            "response": {"type": "image", "format": "url"},
            "inputs": [
                {"name": "prompt", "type": "text", "label": "提示词", "required": True},
                {"name": "negative_prompt", "type": "text", "label": "反向提示词", "required": False},
                {"name": "size", "type": "string", "label": "尺寸", "required": False},
                {"name": "seed", "type": "integer", "label": "随机种子", "required": False},
            ],
        }

    def _workflow_schema_for_display(self, workflow: dict) -> dict:
        output_type = str(workflow.get("output_type") or workflow.get("type") or "image").lower()
        schema = workflow.get("input_schema") if isinstance(workflow.get("input_schema"), dict) else {}
        if not schema:
            schema = self._workflow_input_schema_for_type(output_type)
        inputs = schema.get("inputs") if isinstance(schema.get("inputs"), list) else workflow.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            inputs = self._workflow_input_schema_for_type(output_type).get("inputs", [])
        merged = dict(schema)
        merged["inputs"] = inputs
        if "summary" not in merged:
            merged["summary"] = self._workflow_description_for_type(output_type)
        if "response" not in merged:
            merged["response"] = self._workflow_input_schema_for_type(output_type).get("response", {})
        return merged

    def _workflow_output_contract(self, workflow: dict, schema: dict) -> dict:
        output_type = str(workflow.get("output_type") or workflow.get("type") or "image").lower()
        if output_type == "text":
            return {
                "status": "completed",
                "text": "{...可解析的结构化 JSON 字符串...}",
                "output": {"text": "{...同上...}"},
                "notes": "服务端会把 text/output.text 解析为脚本、角色或分镜 JSON；纯文字工作流必须返回 JSON 对象或 JSON 字符串。",
            }
        if output_type == "video":
            return {
                "status": "completed",
                "outputs": [
                    {
                        "filename": "task_xxx.mp4",
                        "type": "video",
                        "url": "https://客户端公网URL/v1/files/{task_id}/task_xxx.mp4",
                    }
                ],
                "notes": "服务端拿 outputs[].url 下载并填充到对应分镜视频位置。",
            }
        return {
            "created": int(time.time()),
            "data": [
                {
                    "url": "https://客户端公网URL/v1/files/{task_id}/task_xxx.png"
                }
            ],
            "notes": "图片生成优先兼容火山方舟 Images API；旧任务接口也会归一化为 outputs[].url。",
        }

    def _format_workflow_schema_text(self, workflow: dict) -> str:
        schema = self._workflow_schema_for_display(workflow)
        output_type = str(workflow.get("output_type") or workflow.get("type") or "image").lower()
        lines = [
            f"工作流：{self._workflow_display_name(workflow)}",
            f"ID：{workflow.get('id') or '-'}",
            f"类型：{output_type}",
            f"说明：{workflow.get('description') or schema.get('summary') or '-'}",
            "",
            "入参结构：",
        ]
        inputs = schema.get("inputs") or []
        if not inputs:
            lines.append("  - prompt (text, 必填)：提示词/文字需求")
        else:
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("key") or "-"
                typ = item.get("type") or "string"
                label = item.get("label") or item.get("description") or ""
                required = "必填" if item.get("required") else "可选"
                default = item.get("default", None)
                suffix = f"，默认 {json.dumps(default, ensure_ascii=False)}" if default not in (None, "") else ""
                lines.append(f"  - {name} ({typ}, {required})：{label or name}{suffix}")
        lines.extend([
            "",
            "通用请求字段：",
            "  - model：工作流 ID 或名称，服务端会按客户端同步的工作流列表选择。",
            "  - prompt：主要文本输入；图片/视频/文字工作流都应支持。",
            "  - task_id：任务轮询 ID，由客户端生成任务后返回。",
            "",
            "出参结构：",
            json.dumps(self._workflow_output_contract(workflow, schema), ensure_ascii=False, indent=2),
        ])
        return "\n".join(lines)

    def _show_workflow_schema(self, workflow: dict):
        text = self._format_workflow_schema_text(workflow)
        title = self._workflow_display_name(workflow)
        popup = tk.Toplevel(self)
        popup.title(f"工作流参数 - {title}")
        popup.geometry("760x560")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        self._center_popup(popup, 760, 560)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        tk.Label(panel, text=f"{title}：入参 / 出参", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=20, pady=(18, 6))
        tk.Label(panel, text="这里展示客户端对服务端暴露的调用协议；优先使用工作流自带 schema，缺失时使用通用结构。",
                 font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=20, pady=(0, 12))

        box = tk.Text(
            panel,
            font=F["body"],
            bg=C["entry"],
            fg=C["text"],
            relief="flat",
            wrap="word",
            height=20,
            highlightthickness=1,
            highlightbackground=C["border2"],
            padx=10,
            pady=10,
            spacing1=2,
            spacing3=4,
        )
        box.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        box.insert("1.0", text)
        box.config(state="disabled")

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=20, pady=(0, 16))

        def copy_text():
            self.clipboard_clear()
            self.clipboard_append(text)
            self._footer_label.config(text="  已复制工作流参数")

        self._button(actions, "复制参数", copy_text, "primary").pack(side="left", ipadx=12, ipady=5)
        self._button(actions, "关闭", popup.destroy, "plain").pack(side="right", ipadx=12, ipady=5)

    def _load_json_file(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象")
        return data

    def _is_comfy_api_workflow(self, data: dict) -> bool:
        return isinstance(data, dict) and any(
            isinstance(node, dict) and "class_type" in node
            for node in data.values()
        )

    def _workflow_link_map(self, data: dict) -> dict:
        links = data.get("links") or []
        mapping = {}
        for link in links:
            try:
                if isinstance(link, (list, tuple)) and len(link) >= 6:
                    link_id, origin_id, origin_slot, target_id, target_slot = link[:5]
                    mapping[link_id] = [str(origin_id), int(origin_slot or 0)]
                elif isinstance(link, dict):
                    link_id = link.get("id")
                    origin_id = link.get("origin_id", link.get("originId", link.get("from_node_id", link.get("fromNode"))))
                    origin_slot = link.get("origin_slot", link.get("originSlot", link.get("from_socket", link.get("fromSlot", 0))))
                    if link_id is not None and origin_id is not None:
                        mapping[link_id] = [str(origin_id), int(origin_slot or 0)]
            except Exception:
                continue
        if mapping:
            return mapping

        for node in data.get("nodes") or []:
            node_id = node.get("id")
            if node_id is None:
                continue
            for index, output in enumerate(node.get("outputs") or []):
                for link_id in output.get("links") or []:
                    mapping[link_id] = [str(node_id), int(output.get("slot_index", index) or 0)]
        return mapping

    def _front_workflow_widget_inputs(self, node: dict) -> list:
        inputs = node.get("inputs") or []
        widget_inputs = []
        for item in inputs:
            if not isinstance(item, dict) or item.get("link") is not None:
                continue
            widget = item.get("widget") if isinstance(item.get("widget"), dict) else {}
            name = widget.get("name") or item.get("name")
            if name:
                widget_inputs.append(str(name))
        return widget_inputs

    def _convert_front_workflow_to_api(self, data: dict) -> dict:
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("普通 ComfyUI 工作流缺少 nodes，无法自动转换为 API 模式。")
        if data.get("definitions") and any(not isinstance(node.get("type"), str) or len(str(node.get("type"))) > 40 for node in nodes):
            raise ValueError("该工作流包含子图/模板节点，客户端暂不能安全展开。请在 ComfyUI 中打开后另存为 API Format。")

        link_map = self._workflow_link_map(data)
        prompt = {}
        converted_count = 0
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("id")
            class_type = str(node.get("type") or "").strip()
            if node_id is None or not class_type:
                continue
            node_inputs = {}
            for item in node.get("inputs") or []:
                if not isinstance(item, dict):
                    continue
                link_id = item.get("link")
                if link_id is None:
                    continue
                source = link_map.get(link_id)
                if source:
                    name = str(item.get("name") or "").strip()
                    if name:
                        node_inputs[name] = source

            widget_names = self._front_workflow_widget_inputs(node)
            widget_values = node.get("widgets_values") or []
            if isinstance(widget_values, dict):
                for name, value in widget_values.items():
                    node_inputs[str(name)] = value
            elif isinstance(widget_values, list):
                for index, value in enumerate(widget_values):
                    if index < len(widget_names):
                        node_inputs[widget_names[index]] = value

            prompt[str(node_id)] = {
                "class_type": class_type,
                "inputs": node_inputs,
                "_meta": {
                    "title": str(node.get("title") or node.get("properties", {}).get("Node name for S&R") or class_type),
                },
            }
            converted_count += 1

        if not converted_count:
            raise ValueError("没有找到可转换的 ComfyUI 节点。")
        return prompt

    def _find_workflow_json_in_dir(self, folder: Path) -> Path:
        preferred = [
            folder / "workflow.json",
            *sorted(folder.glob("*_api.json")),
            *sorted(folder.glob("*api*.json")),
            *sorted(folder.glob("*.json")),
        ]
        for path in preferred:
            if not path.exists() or path.name.lower() == "manifest.json":
                continue
            try:
                data = self._load_json_file(path)
            except Exception:
                continue
            if self._is_comfy_api_workflow(data) or ("nodes" in data and "links" in data):
                return path
        raise ValueError("没有找到 ComfyUI API 格式的 workflow JSON。请在 ComfyUI 里使用“Save (API Format)”导出。")

    def _prepare_workflow_source(self, source_path: Path) -> tuple[Path, dict, dict]:
        source_path = Path(source_path)
        temp_dir = None
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            temp_dir = BASE_DIR / "runtime" / "workflow_import_tmp" / f"import_{int(time.time())}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(source_path, "r") as zf:
                zf.extractall(temp_dir)
            candidates = [item for item in temp_dir.iterdir()]
            source_path = candidates[0] if len(candidates) == 1 else temp_dir

        manifest = {}
        if source_path.is_dir():
            manifest_path = source_path / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = self._load_json_file(manifest_path)
                except Exception:
                    manifest = {}
            workflow_json = self._find_workflow_json_in_dir(source_path)
        else:
            workflow_json = source_path

        data = self._load_json_file(workflow_json)
        if not self._is_comfy_api_workflow(data):
            if "nodes" in data and "links" in data:
                data = self._convert_front_workflow_to_api(data)
                converted_dir = BASE_DIR / "runtime" / "workflow_import_tmp" / "converted"
                converted_dir.mkdir(parents=True, exist_ok=True)
                converted_path = converted_dir / f"{workflow_json.stem}_{int(time.time())}_api.json"
                with open(converted_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                workflow_json = converted_path
                print(f"[Workflow] Converted frontend workflow to API format: {workflow_json}")
            else:
                raise ValueError("这个 JSON 看起来不是 ComfyUI API 工作流。")
        if not self._is_comfy_api_workflow(data):
            raise ValueError("工作流自动转换失败：转换结果不是 API 模式。")
        return workflow_json, data, manifest

    def _write_workflow_config_from_manifests(self, default_workflow_id: str = ""):
        config_path = BASE_DIR / "runtime" / "workflow_config.json"
        workflows_dir = BASE_DIR / "workflows"
        old_default = ""
        if config_path.exists():
            try:
                old_default = self._load_json_file(config_path).get("default_workflow_id", "")
            except Exception:
                old_default = ""

        type_map = {
            "video.first_last_to_video": "video",
            "video.image_to_video": "video",
            "image.text_to_image": "image",
            "text.chat": "text",
        }
        workflows = []
        for folder in sorted(workflows_dir.iterdir() if workflows_dir.exists() else []):
            if not folder.is_dir():
                continue
            manifest_path = folder / "manifest.json"
            workflow_path = folder / "workflow.json"
            if not manifest_path.exists() or not workflow_path.exists():
                continue
            try:
                manifest = self._load_json_file(manifest_path)
            except Exception:
                continue
            wf_id = str(manifest.get("id") or folder.name).strip()
            raw_type = str(manifest.get("type") or "").strip()
            output_type = type_map.get(raw_type, raw_type.split(".")[-1] if "." in raw_type else raw_type)
            if output_type == "text_to_image":
                output_type = "image"
            workflows.append({
                "id": wf_id,
                "name": str(manifest.get("name") or wf_id).strip(),
                "enabled": bool(manifest.get("enabled", True)),
                "description": str(manifest.get("description") or manifest.get("name") or wf_id).strip(),
                "workflow_json": f"{folder.name}/workflow.json",
                "output_type": output_type or "image",
                "folder_name": folder.name,
                "input_schema": manifest.get("input_schema") or self._workflow_input_schema_for_type(output_type or "image"),
                "inputs": (manifest.get("input_schema") or self._workflow_input_schema_for_type(output_type or "image")).get("inputs") or manifest.get("inputs") or [],
            })

        valid_ids = {item["id"] for item in workflows}
        chosen_default = default_workflow_id or old_default
        if chosen_default not in valid_ids:
            image_default = next((item["id"] for item in workflows if item.get("output_type") == "image"), "")
            chosen_default = image_default or (workflows[0]["id"] if workflows else "")

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({
                "workflows": workflows,
                "default_workflow_id": chosen_default,
            }, f, ensure_ascii=False, indent=2)
        return workflows

    def _install_workflow_from_path(self, source_path: Path) -> dict:
        workflow_json, data, manifest = self._prepare_workflow_source(source_path)
        source_path = Path(source_path)
        source_stem = workflow_json.stem
        if source_stem.lower() in ("workflow", "api", "workflow_api") and workflow_json.parent.name:
            source_stem = workflow_json.parent.name
        base_name = str(manifest.get("id") or source_stem or source_path.stem)
        workflow_id = self._unique_workflow_id(base_name)
        output_type = self._infer_workflow_output_type(workflow_json.stem, data, manifest)
        workflow_name = str(manifest.get("name") or source_stem or workflow_id).strip()
        target_dir = BASE_DIR / "workflows" / workflow_id
        target_dir.mkdir(parents=True, exist_ok=False)

        source_root = workflow_json.parent if source_path.is_dir() else None
        if source_root and source_root.exists():
            for item in source_root.iterdir():
                if item.name.lower() == "manifest.json":
                    continue
                target = target_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
        shutil.copy2(workflow_json, target_dir / "workflow.json")
        try:
            temp_root = (BASE_DIR / "runtime" / "workflow_import_tmp").resolve()
            if str(workflow_json.resolve()).startswith(str(temp_root)):
                workflow_json.unlink(missing_ok=True)
        except Exception:
            pass

        manifest_data = {
            **manifest,
            "id": workflow_id,
            "name": workflow_name,
            "type": self._workflow_manifest_type(output_type),
            "engine": manifest.get("engine", "comfyui"),
            "version": manifest.get("version", "1.0.0"),
            "description": manifest.get("description") or self._workflow_description_for_type(output_type),
            "input_schema": manifest.get("input_schema") or self._workflow_input_schema_for_type(output_type),
        }
        with open(target_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, ensure_ascii=False, indent=2)

        workflows = self._write_workflow_config_from_manifests(
            workflow_id if output_type == "image" else ""
        )
        return {
            "id": workflow_id,
            "name": workflow_name,
            "output_type": output_type,
            "target": str(target_dir),
            "workflows": workflows,
        }

    def _reload_workflows_and_sync(self):
        import urllib.request as ur
        try:
            req = ur.Request(
                f"{API_BASE}/v1/workflows/reload",
                data=b"{}",
                method="POST",
                headers=self._local_api_headers(),
            )
            ur.urlopen(req, timeout=8).read()
        except Exception as reload_error:
            print(f"[Workflow] reload skipped/failed: {reload_error}")

        try:
            resp = ur.urlopen(f"{API_BASE}/health", timeout=8)
            data = json.loads(resp.read().decode("utf-8"))
            self.after(0, lambda d=data: self._on_health_update(d))
        except Exception as health_error:
            print(f"[Workflow] health refresh failed: {health_error}")

        if self._server_session_token:
            try:
                self._sync_to_server_with_retry(max_retries=SERVER_SYNC_MAX_RETRIES)
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as sync_error:
                print(f"[Workflow] sync after import failed: {sync_error}")
                self.after(0, lambda: self._set_light("server", "offline", "同步失败"))

    def _show_workflow_import_result(self, result: dict):
        messagebox.showinfo(
            "工作流已导入",
            "工作流已自动放入客户端目录并完成注册。\n\n"
            f"名称：{result.get('name')}\n"
            f"ID：{result.get('id')}\n"
            f"类型：{result.get('output_type')}\n\n"
            "客户端会自动刷新本地 API，并在已登录时同步给服务端。",
            parent=self,
        )
        self._footer_label.config(text=f"  工作流已导入：{result.get('name')}")

    def _select_and_install_workflow_file(self, popup=None):
        path = filedialog.askopenfilename(
            title="选择 ComfyUI API 工作流 JSON",
            filetypes=[
                ("ComfyUI API 工作流", "*.json"),
                ("ZIP 工作流包", "*.zip"),
                ("所有文件", "*.*"),
            ],
            parent=popup or self,
        )
        if not path:
            return
        self._run_workflow_import(Path(path), popup)

    def _select_and_install_workflow_folder(self, popup=None):
        path = filedialog.askdirectory(
            title="选择工作流文件夹",
            parent=popup or self,
        )
        if not path:
            return
        self._run_workflow_import(Path(path), popup)

    def _run_workflow_import(self, source_path: Path, popup=None):
        try:
            result = self._install_workflow_from_path(source_path)
        except Exception as error:
            messagebox.showerror("工作流导入失败", str(error), parent=popup or self)
            return
        if popup:
            popup.destroy()
        self._update_model_display()
        threading.Thread(target=self._reload_workflows_and_sync, daemon=True).start()
        self._show_workflow_import_result(result)

    def _show_workflow_upload_dialog(self):
        text = (
            "当前客户端暂时只支持三类工作流：\n\n"
            "1. 文字模型：输入文字需求，用于服务端生成脚本、角色和分镜。\n"
            "   当前主要由服务端设置里的文字模型承担，客户端只同步说明。\n\n"
            "2. 文生图片模型：输入 prompt 文字，输出分镜图片。\n"
            "   目录示例：workflows/flux_t2i_v1/workflow.json\n"
            "   必填输入：prompt\n\n"
            "3. 首尾帧视频模型：输入 prompt 文字、首帧图片、尾帧图片，输出分镜视频。\n"
            "   目录示例：workflows/wan_flf2v_v1/workflow.json\n"
            "   必填输入：prompt、start_image、end_image\n\n"
            "推荐方式：\n"
            "- 点击“选择工作流文件”，选择 ComfyUI 导出的 API Format JSON。\n"
            "- 客户端会自动复制到 workflows 目录、生成 manifest、重建 workflow_config.json。\n"
            "- 如果本地 API 已启动，会自动重载；如果已登录服务端，会自动同步 URL / Key / 工作流列表。\n\n"
            "注意：如果工作流依赖新模型，模型下载地址和存放目录仍需要补充到 config/model_manifest.yaml，后续模型缺失提示才会完整。"
        )
        popup = tk.Toplevel(self)
        popup.title("上传工作流")
        popup.geometry("700x500")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        self._center_popup(popup, 700, 500)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        tk.Label(panel, text="上传工作流", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=20, pady=(18, 6))
        tk.Label(panel, text="服务端会通过客户端接口读取工作流名称、类型和输入要求。",
                 font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=20, pady=(0, 12))
        box = tk.Text(panel, font=F["small"], bg=C["entry"], fg=C["text"], relief="flat", wrap="word", height=18,
                      highlightthickness=1, highlightbackground=C["border2"])
        box.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        box.insert("1.0", text)
        box.config(state="disabled")
        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=20, pady=(0, 16))
        self._button(actions, "选择工作流文件", lambda: self._select_and_install_workflow_file(popup), "primary").pack(side="left", ipadx=12, ipady=6)
        self._button(actions, "选择工作流文件夹", lambda: self._select_and_install_workflow_folder(popup), "plain").pack(side="left", ipadx=12, ipady=6, padx=(10, 0))
        self._button(actions, "打开目录", self._open_workflows_dir, "plain").pack(side="left", ipadx=12, ipady=6, padx=(10, 0))
        self._button(actions, "关闭", popup.destroy, "plain").pack(side="right", ipadx=12, ipady=6)

    # ══════════════════════════════════════════════════════
    # 操作按钮区
    # ══════════════════════════════════════════════════════
    def _load_icon_image(self, name: str, size: int = 24):
        path = GUI_ASSET_DIR / f"{name}_icon.png"
        if not path.exists():
            return None
        try:
            from PIL import Image, ImageTk
            image = Image.open(path).convert("RGBA")
            image.thumbnail((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self._image_refs.append(photo)
            return photo
        except Exception as exc:
            print(f"[GUI] icon load failed: {path} {exc}")
            return None

    def _action_icon(self, parent, kind: str):
        icon_name = {
            "folder": "folder",
            "cube": "model",
            "gear": "ops",
            "refresh": "restart",
        }.get(kind, kind)
        image = self._load_icon_image(icon_name, 24)
        if image is not None:
            label = tk.Label(parent, image=image, bg=C["card"], width=28, height=28)
            label.image = image
            return label

        canvas = tk.Canvas(parent, width=26, height=26, bg=C["card"], highlightthickness=0, bd=0)
        color = "#58708f"
        w = 1.8
        if kind == "folder":
            canvas.create_line(5, 9, 10, 9, 12, 12, 21, 12, 21, 20, 5, 20, 5, 9, fill=color, width=w)
        elif kind == "cube":
            canvas.create_polygon(13, 4, 21, 8, 13, 12, 5, 8, outline=color, fill="", width=w)
            canvas.create_line(5, 8, 5, 17, 13, 22, 21, 17, 21, 8, fill=color, width=w)
            canvas.create_line(13, 12, 13, 22, fill=color, width=w)
        elif kind == "gear":
            canvas.create_oval(8, 8, 18, 18, outline=color, width=w)
            for x1, y1, x2, y2 in [(13, 3, 13, 7), (13, 19, 13, 23), (3, 13, 7, 13), (19, 13, 23, 13),
                                   (6, 6, 8, 8), (18, 18, 20, 20), (18, 8, 20, 6), (6, 20, 8, 18)]:
                canvas.create_line(x1, y1, x2, y2, fill=color, width=w)
        elif kind == "refresh":
            canvas.create_arc(6, 6, 21, 21, start=35, extent=270, style="arc", outline=color, width=w)
            canvas.create_line(19, 7, 21, 12, 16, 11, fill=color, width=w)
        else:
            canvas.create_rectangle(8, 8, 18, 18, outline=color, width=w)
        return canvas

    def _build_action_buttons(self):
        btn_frame = tk.Frame(self._overview_page, bg=C["bg"])
        self._btn_frame = btn_frame
        btn_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(0, 12))

        row = tk.Frame(btn_frame, bg=C["bg"])
        row.pack(fill="x")
        for col in range(4):
            row.columnconfigure(col, weight=1, uniform="action_buttons")

        def action(col, text, icon, command, warn=False):
            card = self._card(row)
            card.configure(height=LAYOUT["actions_h"])
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0 if col == 3 else 6))
            card.pack_propagate(False)
            self._action_icon(card, icon).pack(side="left", padx=(14, 8), pady=8)
            btn = tk.Button(
                card,
                text=text,
                font=F["bold"],
                bg=C["card"],
                fg=C["warn"] if warn else C["text"],
                activebackground=C["hover"],
                activeforeground=C["warn"] if warn else C["text"],
                relief="flat",
                bd=0,
                cursor="hand2",
                command=command,
            )
            btn.pack(side="left", fill="x", expand=True, pady=8)
            return card

        action(0, "打开输出目录", "folder", self._open_outputs)
        action(1, "打开模型目录", "cube", self._open_models)
        action(2, "安装运行环境", "gear", self._open_runtime_maintenance, warn=True)
        action(3, "重启后台", "refresh", self._restart_backend, warn=True)

    # ══════════════════════════════════════════════════════
    # runtime 缺失面板
    # ══════════════════════════════════════════════════════
    def _show_runtime_missing(self, allow_back: bool = False):
        """显示运行环境未安装面板（替换主内容区）"""
        # 隐藏正常内容
        for w in [self._info_frame, getattr(self, "_main_area", None), self._btn_frame]:
            if not w:
                continue
            if w.winfo_ismapped():
                w.pack_forget()
        if self._prog_frame.winfo_ismapped():
            pass

        if hasattr(self, '_runtime_missing_frame') and self._runtime_missing_frame:
            try:
                self._runtime_missing_frame.destroy()
            except Exception:
                pass
            self._runtime_missing_frame = None

        frame = tk.Frame(self._overview_page, bg=C["bg"])
        frame.pack(fill="both", expand=True, padx=LAYOUT["outer"], pady=(8, 12))

        content = self._card(frame, fill="both", expand=True)

        tk.Label(content, text="◇", font=("Microsoft YaHei UI", 26),
                 fg=C["text2"], bg=C["card"]).pack(pady=(46, 8))
        tk.Label(content, text="运行环境未安装", font=("Microsoft YaHei UI", 18, "bold"),
                 fg=C["text"], bg=C["card"]).pack()

        tk.Label(
            content,
            text=f"需要安装 {RUNTIME_PACKAGE_NAME}，可以选择本地 7z 包，也可以从官方 Release 或配置的镜像自动下载安装。",
            font=F["normal"],
            fg=C["text2"],
            bg=C["card"],
            justify="center",
            wraplength=620,
        ).pack(pady=(8, 30))

        primary_row = tk.Frame(content, bg=C["card"])
        primary_row.pack(fill="x", padx=28, pady=(0, 24))

        def install_card(index, icon, title, desc, button, command):
            card = self._card(primary_row)
            card.pack(side="left", fill="both", expand=True, padx=8)
            tk.Label(card, text=icon, font=("Microsoft YaHei UI", 34), fg=C["text2"], bg=C["card"]).pack(pady=(20, 8))
            tk.Label(card, text=f"{index}  {title}", font=F["bold"], fg=C["primary"], bg=C["card"]).pack()
            tk.Label(card, text=desc, font=F["small"], fg=C["text2"], bg=C["card"],
                     justify="center", wraplength=190).pack(padx=14, pady=(8, 16))
            self._button(card, button, command, "plain").pack(pady=(0, 20), ipadx=22, ipady=5)

        install_card("1", "□", "选择 7z 环境包", "从本地选择已经下载好的 7z 运行环境包进行安装。", "选择文件", self._select_runtime)
        install_card("2", "☁", "在线一键安装", "从官方 Release 或配置的镜像下载并安装运行环境。", "一键安装", self._install_runtime_from_mirror)
        install_card("3", "▣", "安装同目录环境包", "自动查找客户端同目录下的 7z 环境包并安装。", "开始安装", self._install_local_runtime)

        secondary_row = tk.Frame(content, bg=C["card"])
        secondary_row.pack(fill="x", padx=26, pady=(0, 16))

        self._button(secondary_row, "打开安装说明", self._open_install_guide, "plain").pack(side="left", ipadx=12, ipady=5)

        self._button(secondary_row, "返回主界面", self._show_main_content, "plain").pack(side="right", ipadx=18, ipady=5)

        self._runtime_missing_frame = frame

    def _hide_runtime_missing(self):
        if hasattr(self, '_runtime_missing_frame') and self._runtime_missing_frame:
            self._runtime_missing_frame.pack_forget()

    def _show_main_content(self):
        self._hide_runtime_missing()
        if not self._info_frame.winfo_ismapped():
            self._info_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(8, 10))
        if hasattr(self, "_main_area") and not self._main_area.winfo_ismapped():
            self._main_area.pack(fill="both", expand=True, padx=LAYOUT["outer"], pady=(0, 12))
        if not self._btn_frame.winfo_ismapped():
            self._btn_frame.pack(fill="x", padx=LAYOUT["outer"], pady=(0, 12))

    def _install_local_runtime(self):
        """安装同目录下的 runtime 包"""
        # 查找同目录的 7z 包
        preferred = BASE_DIR / RUNTIME_PACKAGE_NAME
        candidates = [preferred] if preferred.exists() else list(BASE_DIR.glob("runtime-*.7z"))
        if not candidates:
            r = messagebox.askyesno(
                "未找到环境包",
                "当前目录未找到 runtime-*.7z 文件。\n\n是否打开文件选择对话框？")
            if r:
                self._select_runtime()
            return

        pkg = candidates[0]
        self._extract_runtime(pkg)

    def _runtime_mirror_url(self) -> str:
        cfg = _load_local_config()
        runtime_cfg = cfg.get("runtime", {}) if isinstance(cfg.get("runtime", {}), dict) else {}
        return str(
            runtime_cfg.get("mirror_url") or
            cfg.get("runtime_mirror_url") or
            os.environ.get("LINGJING_RUNTIME_MIRROR_URL") or
            RUNTIME_RELEASE_URL
        ).strip()

    def _install_runtime_from_mirror(self):
        repairing = _runtime_has_package_files()
        if repairing and not messagebox.askyesno(
            "安装或修复运行环境",
            "安装过程中会暂时停止 ComfyUI、API 和公网连接，完成后自动重新启动。\n\n"
            "模型、工作流和生成结果不会被删除。是否继续？",
            parent=self,
        ):
            return
        url = self._runtime_mirror_url()
        if url:
            self._download_runtime(url, repair_confirmed=repairing)
            return

        messagebox.showerror(
            "未配置下载地址",
            "当前版本没有配置运行环境包下载地址，无法自动安装。\n\n"
            "请先使用本地 7z 包安装，或配置 runtime.mirror_url 后再点一键安装。"
        )

    def _download_runtime(self, url: str, repair_confirmed: bool = False):
        popup = tk.Toplevel(self)
        popup.title("下载运行环境")
        popup.geometry("430x150")
        popup.configure(bg=C["surface"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)
        self._center_popup(popup, 430, 150)

        tk.Label(popup, text="正在下载运行环境...", font=F["bold"],
                 fg=C["text"], bg=C["surface"]).pack(pady=(22, 8))
        progress_lbl = tk.Label(popup, text="准备下载...", font=F["small"],
                                fg=C["warn"], bg=C["surface"])
        progress_lbl.pack()

        def keep_download_open():
            progress_lbl.config(text="下载正在进行，完成前请保持窗口开启", fg=C["warn"])

        def show_download_error(message: str):
            try:
                progress_lbl.config(text=f"下载失败：{message}", fg=C["error"])
                popup.grab_release()
                popup.protocol("WM_DELETE_WINDOW", popup.destroy)
            except Exception:
                pass

        popup.protocol("WM_DELETE_WINDOW", keep_download_open)

        def _do_download():
            try:
                import urllib.request as ur
                cache_dir = BASE_DIR / "cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                target = cache_dir / RUNTIME_PACKAGE_NAME
                partial = target.with_name(f"{target.name}.part")
                sidecar = Path(f"{target}.sha256")

                resume_at = partial.stat().st_size if partial.exists() else 0
                headers = {"User-Agent": "LingJing-Desktop/0.1.0"}
                if resume_at:
                    headers["Range"] = f"bytes={resume_at}-"
                request = ur.Request(url, headers=headers)
                with ur.urlopen(request, timeout=30) as response:
                    partial_response = getattr(response, "status", None) == 206
                    mode = "ab" if resume_at and partial_response else "wb"
                    if mode == "wb":
                        resume_at = 0
                    remaining = int(response.headers.get("Content-Length") or 0)
                    total_size = resume_at + remaining if remaining else 0
                    downloaded = resume_at
                    with partial.open(mode) as handle:
                        while True:
                            chunk = response.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if total_size:
                                pct = min(100, int(downloaded * 100 / total_size))
                                self.after(0, lambda p=pct: progress_lbl.config(text=f"下载中：{p}%"))
                            else:
                                size_mb = downloaded / (1024 * 1024)
                                self.after(0, lambda s=size_mb: progress_lbl.config(text=f"已下载：{s:.1f} MB"))
                partial.replace(target)

                self.after(0, lambda: progress_lbl.config(text="正在下载校验文件..."))
                try:
                    checksum_request = ur.Request(f"{url}.sha256", headers={"User-Agent": "LingJing-Desktop/0.1.0"})
                    with ur.urlopen(checksum_request, timeout=30) as response:
                        sidecar.write_bytes(response.read())
                except Exception:
                    if url.rstrip("/") == RUNTIME_RELEASE_URL.rstrip("/"):
                        raise RuntimeError("官方环境包 SHA256 校验文件下载失败")

                self.after(0, popup.destroy)
                self.after(100, lambda: self._extract_runtime(target, repair_confirmed=repair_confirmed))
            except Exception as ex:
                if not self._shutting_down:
                    self.after(0, lambda e=str(ex): show_download_error(e))

        threading.Thread(target=_do_download, daemon=True).start()

    def _select_runtime(self):
        """选择环境包"""
        path = filedialog.askopenfilename(
            title="选择运行环境包",
            filetypes=[("7z 压缩包", "*.7z"), ("所有文件", "*.*")])
        if path:
            self._extract_runtime(Path(path))

    def _extract_runtime(self, pkg_path: Path, repair_confirmed: bool = False):
        """解压 runtime 包"""
        if _runtime_has_package_files() and not repair_confirmed:
            if not messagebox.askyesno(
                "安装或修复运行环境",
                "安装过程中会暂时停止 ComfyUI、API 和公网连接，完成后自动重新启动。\n\n"
                "模型、工作流和生成结果不会被删除。是否继续？",
                parent=self,
            ):
                return
        # 后台线程解压
        popup = tk.Toplevel(self)
        popup.title("安装中")
        popup.geometry("360x120")
        popup.configure(bg=C["surface"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)
        self._center_popup(popup, 360, 120)

        tk.Label(popup, text="正在解压运行环境，请稍候...", font=F["bold"],
                 fg=C["text"], bg=C["surface"]).pack(pady=(20, 8))
        progress_lbl = tk.Label(popup, text="准备中...", font=F["small"],
                                fg=C["warn"], bg=C["surface"])
        progress_lbl.pack()

        def keep_install_open():
            progress_lbl.config(text="安装正在进行，完成前请保持窗口开启", fg=C["warn"])

        def show_install_error(message: str):
            try:
                progress_lbl.config(text=f"失败: {message}", fg=C["error"])
                popup.grab_release()
                popup.protocol("WM_DELETE_WINDOW", popup.destroy)
            except Exception:
                pass
            self._environment_status = {
                "package_ready": _check_runtime_exists(),
                "ready": False,
                "message": str(message),
            }
            if hasattr(self, "_dashboard_pages"):
                self._dashboard_pages.refresh(self._last_health)

        popup.protocol("WM_DELETE_WINDOW", keep_install_open)

        def _do_extract():
            maintenance_started = False
            running_before: set[str] = set()
            try:
                package = Path(pkg_path)
                if not package.is_file():
                    raise FileNotFoundError(f"环境包不存在: {package}")

                sidecar = Path(f"{package}.sha256")
                if sidecar.is_file():
                    self.after(0, lambda: progress_lbl.config(text="正在校验 SHA256..."))
                    valid, expected, actual = verify_sha256(package, sidecar)
                    if not valid:
                        raise RuntimeError(
                            f"SHA256 校验失败（期望 {expected[:12]}...，实际 {actual[:12]}...）"
                        )

                extractor = find_extractor(BASE_DIR)
                if not extractor:
                    raise RuntimeError("未找到可用的 7-Zip 或 Windows tar.exe 解压工具")

                self.after(0, lambda: progress_lbl.config(text="正在检查环境包结构..."))
                list_result = self._process_supervisor.run(
                    "runtime-install",
                    archive_list_command(extractor, package),
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if list_result.returncode != 0:
                    raise RuntimeError(list_result.stderr.strip() or "无法读取环境包目录")
                members = parse_archive_members(extractor, list_result.stdout)
                missing = missing_archive_entries(members)
                invalid = invalid_archive_entries(members)
                if missing:
                    raise RuntimeError(f"环境包目录结构不完整，缺少: {', '.join(missing)}")
                if invalid:
                    raise RuntimeError(f"环境包包含不允许的路径: {invalid[0]}")
                if self._shutting_down:
                    return

                self.after(0, lambda: progress_lbl.config(text="正在暂停后台服务..."))
                maintenance_started, running_before, stop_error = self._begin_runtime_maintenance()
                if not maintenance_started:
                    raise RuntimeError(stop_error or "无法开始运行环境维护")
                if stop_error:
                    raise RuntimeError(f"后台服务未能安全停止：{stop_error}")

                self.after(0, lambda: progress_lbl.config(text="正在解压..."))
                result = self._process_supervisor.run(
                    "runtime-install",
                    archive_extract_command(extractor, package, BASE_DIR),
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=3600,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "环境包解压失败")

                missing_after_install = missing_runtime_paths(BASE_DIR)
                if missing_after_install:
                    raise RuntimeError(f"安装后环境仍不完整，缺少: {', '.join(missing_after_install)}")

                if not self._shutting_down:
                    self._end_runtime_maintenance(restart=True)
                    maintenance_started = False

                    def finish_install():
                        try:
                            popup.destroy()
                        except Exception:
                            pass
                        self._environment_status = {}
                        self._show_main_content()
                        if hasattr(self, "_dashboard_pages"):
                            self._dashboard_pages.refresh(self._last_health)
                            if self._current_page_id == "resources":
                                self.after_idle(self._dashboard_pages.focus_runtime_maintenance)

                    self.after(0, finish_install)
            except Exception as e:
                if maintenance_started:
                    can_restore = bool(running_before) and _check_runtime_exists()
                    self._end_runtime_maintenance(restart=can_restore)
                    maintenance_started = False
                if not self._shutting_down:
                    self.after(0, lambda error=str(e): show_install_error(error))
            finally:
                if maintenance_started:
                    can_restore = bool(running_before) and _check_runtime_exists()
                    self._end_runtime_maintenance(restart=can_restore)

        threading.Thread(target=_do_extract, daemon=True).start()

    def _runtime_maintenance_active(self) -> bool:
        lock = self.__dict__.get("_runtime_maintenance_lock")
        if lock is None:
            return bool(self.__dict__.get("_runtime_maintenance_in_progress", False))
        if not lock.acquire(blocking=False):
            return True
        try:
            return bool(self._runtime_maintenance_in_progress)
        finally:
            lock.release()

    def _begin_runtime_maintenance(self) -> tuple[bool, set[str], str]:
        """Reserve runtime replacement and stop services without a launch race."""
        with self._runtime_maintenance_lock:
            if self._shutting_down:
                return False, set(), "客户端正在退出"
            if self._runtime_maintenance_in_progress:
                return False, set(), "已有运行环境维护任务正在进行"
            self._runtime_maintenance_in_progress = True
            running_before = {
                role
                for role in ("comfyui", "api")
                if self._process_supervisor.is_running(role)
            }
        try:
            error = self._stop_runtime_for_maintenance()
        except Exception as exc:
            with self._runtime_maintenance_lock:
                self._runtime_maintenance_in_progress = False
            return False, running_before, f"停止后台服务失败：{exc}"
        return True, running_before, error

    def _end_runtime_maintenance(self, restart: bool):
        """Release the maintenance guard and optionally restore backend services."""
        with self._runtime_maintenance_lock:
            self._runtime_maintenance_in_progress = False
        if restart and not self._shutting_down:
            self.after(
                500,
                lambda: self._request_backend_start("恢复后台"),
            )

    def _stop_runtime_for_maintenance(self) -> str:
        """Stop only processes that may lock runtime files before replacement."""
        self._poll_run = False
        errors = []
        for role in ("comfyui", "api", "torch-check", "environment-check"):
            error = self._process_supervisor.terminate(role, timeout=8)
            if error:
                errors.append(f"{role}: {error}")
        if not self._process_supervisor.is_running("comfyui"):
            self._comfy_proc = None
        if not self._process_supervisor.is_running("api"):
            self._api_proc = None
        for name, port in (("ComfyUI", COMFY_PORT), ("API", API_PORT)):
            port_ready, port_error = self._process_supervisor.prepare_port(port)
            if not port_ready:
                errors.append(
                    f"{name} 仍被其他程序占用：{port_error}。请先手动关闭该程序"
                )
        return "；".join(errors)

    def _open_install_guide(self):
        """打开安装说明"""
        import webbrowser
        guide_path = BASE_DIR / "README.md"
        if guide_path.exists():
            os.startfile(str(guide_path))
        else:
            webbrowser.open("https://github.com")

    # ══════════════════════════════════════════════════════
    # 底部状态栏
    # ══════════════════════════════════════════════════════
    def _build_footer(self):
        bar = tk.Frame(self._content_root, bg=C["surface"], height=LAYOUT["footer_h"])
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._loading_bar = tk.Frame(bar, bg=C["surface"])
        self._loading_bar.pack(side="left", padx=(12, 8))
        self._loading_text = tk.Label(
            self._loading_bar, text="", font=F["small"], fg=C["warn"], bg=C["surface"])
        self._loading_text.pack(side="left")
        self._loading_dots = tk.Label(
            self._loading_bar, text="", font=F["small"], fg=C["warn"], bg=C["surface"], width=3, anchor="w")
        self._loading_dots.pack(side="left")
        self._footer_label = tk.Label(
            bar, text="", font=("Consolas", 8), fg=C["text2"], bg=C["surface"], anchor="w")
        self._footer_label.pack(side="left")
        tk.Button(bar, text="退出", font=F["small"], bg=C["surface"], fg=C["error"],
                  activebackground=C["hover"], relief="flat", bd=0, cursor="hand2",
                  command=self._on_close).pack(side="right", padx=(0, 12))
        self._animate_loading()

    # ══════════════════════════════════════════════════════
    # 辅助动作
    # ══════════════════════════════════════════════════════
    def _short_middle(self, text: str, left: int = 30, right: int = 16) -> str:
        text = str(text or "").strip()
        if len(text) <= left + right + 3:
            return text
        return f"{text[:left]}...{text[-right:]}"

    def _set_public_url(self, url: str):
        self._url_label.config(text=self._short_middle(url, 16, 10) if url else "等待隧道...")

    def _clear_public_url(self):
        self._tunnel_url = ""
        self._set_public_url("")
        self._initial_session_sync_done = False

    def _set_api_key(self, api_key: str):
        self._key_label.config(text=self._short_middle(api_key, 18, 10) if api_key else "—")
        settings_label = getattr(self, "_settings_key_label", None)
        if settings_label is not None:
            masked = f"{api_key[:12]}{'•' * 12}{api_key[-6:]}" if len(api_key) > 20 else "服务启动后自动生成"
            try:
                settings_label.config(text=masked)
            except Exception:
                pass

    def _copy_public_url(self):
        self._copy(self._tunnel_url)

    def _copy_api_key(self):
        self._copy(self._api_key)

    def _edit_api_key(self):
        """Let the user change the local gateway key and apply it safely."""
        popup = tk.Toplevel(self)
        popup.title("修改访问密钥")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        popup.grab_set()
        self._center_popup(popup, 540, 255)

        panel = self._card(popup, fill="both", expand=True, padx=16, pady=16)
        tk.Label(panel, text="修改访问密钥", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=22, pady=(20, 5))
        tk.Label(
            panel,
            text="其他软件使用这个 Key 调用客户端。保存后，正在运行的 API 会自动重启。",
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
        ).pack(anchor="w", padx=22)

        entry = self._entry_widget(panel, width=430 if CTK_AVAILABLE else 52)
        entry.pack(fill="x", padx=22, pady=(14, 5), ipady=7 if not CTK_AVAILABLE else 0)
        entry.insert(0, self._api_key or f"sk-local-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}")
        status = tk.Label(panel, text="16–128 位，仅支持字母、数字、点、下划线和短横线。", font=F["tiny"], fg=C["muted"], bg=C["card"])
        status.pack(anchor="w", padx=22)

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=22, pady=(13, 0))

        def regenerate():
            value = f"sk-local-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
            entry.delete(0, "end")
            entry.insert(0, value)
            status.config(text="已生成新密钥，点击保存后生效。", fg=C["warn"])

        def save():
            value = entry.get().strip()
            if not 16 <= len(value) <= 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
                status.config(text="密钥格式不正确，请按提示修改。", fg=C["error"])
                return
            status.config(text="正在保存并应用...", fg=C["warn"])
            regenerate_button.configure(state="disabled")
            save_button.configure(state="disabled")

            def apply_key():
                if not self._begin_backend_action("应用访问密钥"):
                    self.after(0, lambda: show_error("其他后台操作正在进行，请稍候重试。"))
                    return
                was_running = False
                saved = False
                try:
                    was_running = self._process_supervisor.is_running("api")
                    if was_running:
                        self._invalidate_health_polling()
                        error = self._process_supervisor.terminate("api", timeout=8)
                        if error:
                            self._ensure_health_polling()
                            self.after(0, lambda e=error: show_error(f"API 无法安全重启：{e}"))
                            return
                        self._api_proc = None
                    # Stop the old API before writing.  Its in-memory state may
                    # contain the previous key and must not overwrite this save.
                    RuntimeState(BASE_DIR / "runtime").set_api_key(value)
                    saved = True
                    if not self._run_ui_backend_step(
                        lambda: finish_save(value, was_running)
                    ):
                        return
                    if (
                        was_running
                        and not self._shutting_down
                        and not self._runtime_maintenance_active()
                    ):
                        if self._run_ui_backend_step(self._start_api_service):
                            self._ensure_health_polling()
                except Exception as exc:
                    if was_running and not self._shutting_down:
                        try:
                            if self._run_ui_backend_step(self._start_api_service):
                                self._ensure_health_polling()
                        except Exception as restore_error:
                            print(f"[Settings] API restore failed: {restore_error}")
                    if saved:
                        self.after(
                            0,
                            lambda e=exc: self._report_backend_failure(
                                f"访问密钥已保存，但 API 重启失败：{e}"
                            ),
                        )
                    else:
                        self.after(0, lambda e=exc: show_error(f"保存失败：{e}"))
                finally:
                    self._end_backend_action()

            threading.Thread(target=apply_key, daemon=True).start()

        def show_error(message: str):
            try:
                if popup.winfo_exists():
                    status.config(text=message, fg=C["error"])
                    regenerate_button.configure(state="normal")
                    save_button.configure(state="normal")
            except Exception:
                pass

        def finish_save(value: str, restart_api: bool):
            self._api_key = value
            self._set_api_key(value)
            try:
                popup.grab_release()
                popup.destroy()
            except Exception:
                pass
            self._footer_label.config(text="  访问密钥已保存")
            if restart_api and not self._shutting_down:
                self._clear_public_url()
                self._footer_label.config(text="  访问密钥已保存，正在重启 API...")
                self._set_light("api", "loading", "重启中")
                self._set_light("tunnel", "loading", "重连中")

        regenerate_button = self._button(actions, "重新生成", regenerate, "plain", width=88)
        regenerate_button.pack(side="left")
        save_button = self._button(actions, "保存并应用", save, "primary", width=104)
        save_button.pack(side="right")

    def _json_headers(self, server_url: str = "", token: str = "") -> dict:
        server_url = (server_url or "").rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36 LingjingClient/0.2"
            ),
        }
        if server_url.startswith(("http://", "https://")):
            headers["Origin"] = server_url
            headers["Referer"] = f"{server_url}/"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _current_local_api_key(self) -> str:
        """Read the key used by the running local API, with memory as fallback."""
        persisted = ""
        try:
            persisted = str(
                RuntimeState(BASE_DIR / "runtime").api_key or ""
            ).strip()
        except Exception as exc:
            print(f"[Runtime] failed to reload local API key: {exc}")
        api_key = persisted or str(self._api_key or "").strip()
        if api_key and api_key != self._api_key:
            self._api_key = api_key
        return api_key

    def _local_api_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        api_key = self._current_local_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _set_account_status(self, text: str, status: str = "warn"):
        color = C["success"] if status == "success" else C["error"] if status == "error" else C["warn"]
        self._account_status_text = text
        if hasattr(self, "_account_status_label"):
            self._account_status_label.config(text=text, fg=color)
        self._render_account_badge()

    def _fetch_account_profile(self, server_url: str, token: str) -> dict:
        if not server_url or not token:
            return {}
        import urllib.request as ur
        req = ur.Request(
            f"{server_url.rstrip('/')}/api/auth/me",
            method="GET",
            headers=self._json_headers(server_url, token),
        )
        with ur.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _format_http_error(self, error: Exception) -> str:
        try:
            import urllib.error as ue
            if isinstance(error, ue.HTTPError):
                raw = error.read().decode("utf-8", errors="ignore")
                lowered = raw.lower()
                if "user-agent has been banned" in lowered or "cloudflare" in lowered:
                    return "Cloudflare 拦截了桌面客户端请求，请在服务端安全规则放行客户端登录接口"
                try:
                    data = json.loads(raw)
                    err = data.get("error") if isinstance(data, dict) else None
                    if isinstance(err, dict):
                        code = err.get("code", "")
                        msg = err.get("message", "")
                        return f"{code}: {msg}" if code and msg else msg or code or f"HTTP {error.code}"
                    if isinstance(data, dict) and data.get("message"):
                        return str(data.get("message"))
                except Exception:
                    pass
                raw = raw.strip()
                if raw.startswith("<"):
                    return f"HTTP {error.code} {error.reason}"
                return raw[:240] or f"HTTP {error.code} {error.reason}"
        except Exception:
            pass
        return str(error)

    def _use_guest_mode(self):
        self._server_session_token = ""
        self._server_user_email = ""
        self._server_mode = "guest"
        self._server_account_profile = {}
        self._server_password_var.set("")
        self._save_account_session()
        self._render_account_badge()
        self._apply_account_visibility()
        self._set_account_status("本地模式：不会连接平台服务端", "success")
        self._set_light("server", "online", "本地模式")

    def _show_login_prompt(self, force: bool = False):
        if self._shutting_down:
            return
        try:
            if self._login_popup is not None and self._login_popup.winfo_exists():
                self._login_popup.lift()
                self._login_popup.focus_force()
                return
        except Exception:
            self._login_popup = None
        if self._login_prompt_shown and not force:
            return
        self._login_prompt_shown = True

        popup = tk.Toplevel(self)
        self._login_popup = popup
        popup.title("登录平台账号")
        popup.geometry("600x470")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(True, True)
        popup.minsize(580, 450)
        self._center_popup(popup, 600, 470)

        def close_popup():
            self._login_popup = None
            try:
                popup.grab_release()
            except Exception:
                pass
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", close_popup)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)

        title_row = tk.Frame(panel, bg=C["card"])
        title_row.pack(fill="x", padx=28, pady=(22, 8))
        tk.Label(title_row, text="▷", font=("Microsoft YaHei UI", 18, "bold"),
                 fg="#ffffff", bg=C["primary"], width=3).pack(side="left", padx=(0, 10), ipady=4)
        tk.Label(title_row, text="登录灵镜造片厂账号", font=F["title"],
                 fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Label(panel, text="登录后可同步会员与平台信息；不登录也能完整使用本地工作流、API 和 Tunnel。",
                 font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", padx=28, pady=(0, 18))

        form = tk.Frame(panel, bg=C["card"])
        form.pack(fill="x", padx=28)

        def popup_field(label, text="", show=None):
            row = tk.Frame(form, bg=C["card"])
            row.pack(fill="x", pady=(0, 10))
            tk.Label(row, text=label, font=F["small"], fg=C["text"], bg=C["card"], width=10, anchor="w").pack(side="left")
            entry = self._entry_widget(row, show=show, width=320)
            entry.pack(side="left", fill="x", expand=True, ipady=6 if not CTK_AVAILABLE else 0)
            if text:
                entry.insert(0, text)
            return entry

        server_entry = popup_field("服务端地址", self._get_server_url() or "https://ai.lol-lu.site")
        email_entry = popup_field("账号邮箱", self._get_server_email())
        password_entry = popup_field("密码", self._get_server_password(), show="*")
        status_lbl = tk.Label(panel, text="", font=F["small"], fg=C["error"], bg=C["card"], anchor="w")
        status_lbl.pack(fill="x", padx=28)

        actions = tk.Frame(panel, bg=C["card"])
        actions.pack(fill="x", padx=28, pady=(14, 0))

        def guest():
            self._set_account_form_values(
                server_url=server_entry.get().strip(),
                email=email_entry.get().strip(),
                password="",
            )
            self._use_guest_mode()
            close_popup()

        def login():
            self._set_account_form_values(
                server_url=server_entry.get().strip(),
                email=email_entry.get().strip(),
                password=password_entry.get(),
            )
            def on_result(success: bool, message: str):
                try:
                    if not popup.winfo_exists():
                        return
                    status_lbl.config(text=message, fg=C["success"] if success else C["error"])
                    login_button.configure(state="normal", text="登录并同步")
                    if success:
                        popup.after(500, close_popup)
                    else:
                        password_entry.focus_set()
                except Exception:
                    pass

            status_lbl.config(text="正在验证账号...", fg=C["warn"])
            login_button.configure(state="disabled", text="正在登录...")
            self._login_and_sync(on_result=on_result)

        login_button = self._button(actions, "登录并同步", login, "primary")
        login_button.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        self._button(actions, "继续使用本地模式", guest, "plain").pack(side="left", fill="x", expand=True, ipady=7, padx=(8, 0))

        links = tk.Frame(panel, bg=C["card"])
        links.pack(fill="x", padx=28, pady=(16, 0))
        tk.Button(links, text="注册账号", font=F["small"], bg=C["card"], fg=C["primary"],
                  activebackground=C["card"], relief="flat", bd=0, cursor="hand2",
                  command=self._open_register).pack(side="left")

    def _login_and_sync(self, on_result=None):
        if self._server_sync_running:
            if callable(on_result):
                on_result(False, "已有登录请求正在处理中，请稍候。")
            return
        server_url = self._get_server_url()
        email = self._get_server_email()
        password = self._get_server_password()
        if not server_url:
            self._set_account_status("请填写服务端地址", "error")
            if callable(on_result):
                on_result(False, "请填写服务端地址。")
            return
        if not email or not password:
            self._set_account_status("请填写账号邮箱和密码", "error")
            if callable(on_result):
                on_result(False, "请填写账号邮箱和密码。")
            return
        threading.Thread(
            target=self._login_and_sync_worker,
            args=(server_url, email, password, on_result),
            daemon=True,
        ).start()

    def _login_and_sync_worker(self, server_url: str, email: str, password: str, on_result=None):
        self._server_sync_running = True
        self.after(0, lambda: self._set_account_status("正在登录服务端...", "warn"))
        try:
            import urllib.request as ur

            login_payload = json.dumps({"email": email, "password": password}).encode("utf-8")
            req = ur.Request(
                f"{server_url}/api/auth/login",
                data=login_payload,
                method="POST",
                headers=self._json_headers(server_url),
            )
            with ur.urlopen(req, timeout=20) as resp:
                login_data = json.loads(resp.read().decode("utf-8"))
            token = login_data.get("sessionToken", "")
            if not token:
                raise RuntimeError("服务端未返回 sessionToken")
            self._server_session_token = token
            self._offline_notice_sent = False
            self._server_user_email = email
            self._server_url_value = server_url
            self._server_mode = "logged_in"
            self._server_account_profile = {"user": login_data.get("user", {})}
            try:
                profile = self._fetch_account_profile(server_url, token)
                if profile:
                    self._server_account_profile = profile
            except Exception as profile_error:
                print(f"[Account] profile refresh failed: {profile_error}")
            self._save_account_session()
            self.after(0, lambda: self._set_account_form_values(server_url=server_url, email=email, password=""))
            self.after(0, self._render_account_badge)
            self.after(0, self._apply_account_visibility)
            self.after(0, lambda: self._set_account_status("登录成功，正在同步客户端...", "warn"))
            if callable(on_result):
                self.after(0, lambda: on_result(True, "登录成功，正在同步客户端信息..."))
            try:
                self._sync_to_server_with_retry(server_url, max_retries=SERVER_SYNC_MAX_RETRIES, takeover=True)
                self._save_account_session()
                self.after(0, lambda: self._set_account_status(f"已同步：{email}", "success"))
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as sync_error:
                friendly = self._format_http_error(sync_error)
                self.after(0, lambda e=friendly: self._set_account_status(f"已登录，同步失败：{e}", "error"))
                self.after(0, lambda: self._set_light("server", "offline", "同步失败"))
        except Exception as ex:
            self._server_session_token = ""
            self._server_mode = "guest"
            self.after(0, lambda: self._set_account_form_values(password=""))
            friendly = self._format_http_error(ex)
            self.after(0, lambda e=friendly: self._set_account_status(f"同步失败：{e}", "error"))
            if callable(on_result):
                self.after(0, lambda e=friendly: on_result(False, f"登录失败：{e}"))
            self.after(0, lambda: self._set_light("server", "online", "本地模式"))
            self.after(0, self._render_account_badge)
            self.after(0, self._apply_account_visibility)
        finally:
            self._server_sync_running = False

    def _sync_payload(self, status: str = "", takeover: bool = False) -> dict:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        workflows = self._last_health.get("workflows") or []
        if isinstance(workflows, int):
            workflows = []
        models = []
        model_groups = {"image": [], "video": [], "text": []}
        for wf in workflows:
            if not isinstance(wf, dict):
                continue
            model = {
                "id": wf.get("id", ""),
                "name": wf.get("name", wf.get("id", "")),
                "label": wf.get("name", wf.get("id", "")),
                "type": wf.get("type", ""),
                "available": wf.get("enabled", True) and self._workflow_model_available(wf),
                "workflowId": wf.get("id", ""),
                "input_schema": wf.get("input_schema") or {},
                "inputs": wf.get("inputs") or [],
            }
            models.append({
                **model,
            })
            group = self._model_group_for_type(model["type"])
            model_groups.setdefault(group, []).append(model)
        return {
            "baseUrl": self._tunnel_url,
            "apiKey": self._api_key,
            "base_url": self._tunnel_url,
            "api_key": self._api_key,
            "clientId": self._last_health.get("session_id", "") or f"local-{os.environ.get('COMPUTERNAME', 'windows')}",
            "instanceId": self._client_instance_id,
            "clientName": os.environ.get("COMPUTERNAME", "Windows 客户端"),
            "localApi": API_BASE,
            "client_id": self._last_health.get("session_id", "") or f"local-{os.environ.get('COMPUTERNAME', 'windows')}",
            "instance_id": self._client_instance_id,
            "client_name": os.environ.get("COMPUTERNAME", "Windows 客户端"),
            "version": self._last_health.get("version", "0.2.0"),
            "local_api": API_BASE,
            "status": status or ("online" if self._tunnel_url else "starting"),
            "heartbeatAt": now_iso,
            "heartbeat_at": now_iso,
            "lastSeenAt": now_iso,
            "last_seen_at": now_iso,
            "workflows": workflows,
            "models": models,
            "modelGroups": model_groups,
            "model_groups": model_groups,
            "current_task": self._last_health.get("current_task"),
            "source": "desktop-gui",
            "takeover": bool(takeover),
        }

    def _model_group_for_type(self, workflow_type: str) -> str:
        text = str(workflow_type or "").lower()
        if "video" in text or "flf2v" in text:
            return "video"
        if "text" in text or "chat" in text:
            return "text"
        return "image"

    def _workflow_model_available(self, workflow: dict) -> bool:
        if "available" in workflow:
            return bool(workflow.get("available"))
        for key in ("missing_models", "missingModels", "missing"):
            value = workflow.get(key)
            if isinstance(value, (list, tuple, set)) and value:
                return False
            if isinstance(value, str) and value.strip():
                return False

        workflow_id = str(workflow.get("id") or "").lower()
        workflow_name = str(workflow.get("name") or workflow.get("label") or "").lower()
        text = f"{workflow_id} {workflow_name}"
        if "wan" in text or "wan2.1" in text:
            return self._model_status.get("Wan2.1") == "完整"
        if "flux" in text:
            return self._model_status.get("Flux2") == "完整"
        if "qwen" in text or "千问" in text:
            return self._model_status.get("Qwen3.5") == "完整"
        return bool(workflow.get("enabled", True))

    def _sync_to_server(self, server_url: str = "", status: str = "", takeover: bool = False):
        if not self._server_session_token:
            return
        server_url = (server_url or self._server_url_value).rstrip("/")
        if not server_url:
            return
        payload = self._sync_payload(status=status, takeover=takeover)
        if not payload["base_url"] or not payload["api_key"]:
            raise RuntimeError("公网 URL 或 API Key 尚未就绪")

        import urllib.request as ur
        req = ur.Request(
            f"{server_url}/api/client/local-session/sync",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._json_headers(server_url, self._server_session_token),
        )
        with ur.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _sync_to_server_with_retry(self, server_url: str = "", status: str = "", max_retries: int = SERVER_SYNC_MAX_RETRIES, takeover: bool = False):
        last_error = None
        attempts = max(1, int(max_retries or 1))
        for attempt in range(1, attempts + 1):
            try:
                result = self._sync_to_server(server_url, status, takeover=takeover)
                self._server_sync_fail_count = 0
                return result
            except Exception as ex:
                if self._is_session_replaced_error(ex):
                    self.after(0, self._handle_remote_session_replaced)
                    raise ex
                last_error = ex
                self._server_sync_fail_count = attempt
                if attempt < attempts:
                    self.after(0, lambda a=attempt: self._set_light("server", "loading", f"重试 {a}/{attempts}"))
                    time.sleep(min(2 * attempt, 6))
        raise last_error

    def _is_session_replaced_error(self, error: Exception) -> bool:
        code = getattr(error, "code", None)
        if code != 409:
            return False
        friendly = self._format_http_error(error)
        return "LOCAL_CLIENT_SESSION_REPLACED" in friendly or "其他客户端" in friendly

    def _handle_remote_session_replaced(self):
        self._server_session_token = ""
        self._server_mode = "guest"
        self._server_account_profile = {}
        self._server_password_var.set("")
        self._save_account_session()
        self._render_account_badge()
        self._apply_account_visibility()
        self._set_account_status("账号已在其他客户端登录，本客户端已停止同步。", "error")
        self._set_light("server", "online", "本地模式")

    def _notify_server_offline(self, server_url: str = "", token: str = ""):
        session_token = token or self._server_session_token
        if self._offline_notice_sent or not session_token:
            return
        self._offline_notice_sent = True
        server_url = (server_url or self._server_url_value or "https://ai.lol-lu.site").rstrip("/")
        if not server_url:
            return
        try:
            import urllib.request as ur
            payload = self._sync_payload(status="offline")
            req = ur.Request(
                f"{server_url}/api/client/local-session/offline",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers=self._json_headers(server_url, session_token),
            )
            with ur.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as ex:
            print(f"[Account] offline notify failed: {ex}")

    def _heartbeat_loop(self):
        while self._heartbeat_run:
            time.sleep(30)
            if self._shutting_down or not self._server_session_token:
                continue
            try:
                self._sync_to_server_with_retry(max_retries=SERVER_SYNC_MAX_RETRIES)
                if self._server_user_email:
                    self.after(0, lambda: self._set_account_status(f"已同步：{self._server_user_email}", "success"))
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as ex:
                friendly = self._format_http_error(ex)
                self.after(0, lambda e=friendly: self._set_account_status(f"同步异常：{e}", "error"))
                self.after(0, lambda: self._set_light("server", "offline", "同步失败"))

    def _refresh_saved_login_and_sync(self):
        if self._initial_session_sync_done or self._server_mode != "logged_in" or not self._server_session_token:
            return
        if not self._tunnel_url or not self._api_key:
            return
        self._initial_session_sync_done = True

        def _worker():
            server_url = (self._server_url_value or "https://ai.lol-lu.site").rstrip("/")
            try:
                profile = self._fetch_account_profile(server_url, self._server_session_token)
                if profile:
                    self._server_account_profile = profile
                    user = profile.get("user") if isinstance(profile.get("user"), dict) else {}
                    self._server_user_email = user.get("email") or self._server_user_email
                self._sync_to_server_with_retry(server_url, max_retries=SERVER_SYNC_MAX_RETRIES)
                self._save_account_session()
                email = self._server_user_email or "账号"
                self.after(0, self._render_account_badge)
                self.after(0, lambda: self._set_account_status(f"已自动同步：{email}", "success"))
                self.after(0, lambda: self._set_light("server", "online", "已同步"))
            except Exception as ex:
                friendly = self._format_http_error(ex)
                if "UNAUTHENTICATED" in friendly or "Please log in" in friendly or "HTTP 401" in friendly:
                    self._clear_account_session()
                    self._server_mode = "guest"
                    self._save_account_session()
                    self.after(0, self._render_account_badge)
                    self.after(0, self._apply_account_visibility)
                    self.after(0, lambda: self._set_account_status("平台登录已过期，本地功能不受影响", "warn"))
                    self.after(0, lambda: self._set_light("server", "online", "本地模式"))
                else:
                    self.after(0, lambda e=friendly: self._set_account_status(f"自动同步失败：{e}", "error"))
                    self.after(0, lambda: self._set_light("server", "offline", "同步失败"))

        threading.Thread(target=_worker, daemon=True).start()

    def _selected_workflow_path(self) -> str:
        workflow_id = self._workflow_mode.get()
        if not workflow_id or workflow_id == "default":
            return "/v1/workflows/run"
        return f"/v1/workflows/run/{workflow_id}"

    def _selected_image_model(self) -> str:
        return "flux_t2i_v1"

    def _open_server_url(self, path: str):
        base = (self._get_server_url() or self._server_url_value or "https://ai.lol-lu.site").rstrip("/")
        try:
            webbrowser.open(f"{base}{path}")
        except Exception as ex:
            messagebox.showinfo("提示", f"请在浏览器打开：{base}{path}\n\n{ex}")

    def _open_register(self):
        self._open_server_url("/?auth=register")

    def _copy_ark_image_example(self):
        url = self._tunnel_url or API_BASE
        key = self._api_key or "YOUR_API_KEY"
        model = self._selected_image_model()
        example = (
            f'curl -X POST {url}/api/v3/images/generations \\\n'
            f'  -H "Authorization: Bearer {key}" \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"model":"{model}","prompt":"一只可爱的猫","response_format":"url","size":"2K","stream":false,"watermark":false}}\''
        )
        self.clipboard_clear()
        self.clipboard_append(example)
        self._footer_label.config(text="  已复制火山兼容图片示例")
        self.after(3000, lambda: self._footer_label.config(text=""))

    def _copy_example(self):
        url = self._tunnel_url or API_BASE
        key = self._api_key or "YOUR_API_KEY"
        path = self._selected_workflow_path()
        example = (
            f'curl -X POST {url}{path} \\\n'
            f'  -H "Authorization: Bearer {key}" \\\n'
            f'  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"prompt": "一只可爱的猫", "steps": 20}}\''
        )
        self.clipboard_clear()
        self.clipboard_append(example)
        self._footer_label.config(text="  已复制调用示例到剪贴板")
        self.after(3000, lambda: self._footer_label.config(text=""))

    def _open_outputs(self):
        outputs_dir = BASE_DIR / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(outputs_dir))

    def _open_last_output(self):
        self._open_outputs_for_items(self._last_completed_outputs or [], self._last_completed_task_id)

    def _open_outputs_for_items(self, outputs: list, task_id: str = ""):
        outputs_dir = BASE_DIR / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        task_id = str(task_id or "").strip()
        for item in outputs:
            if not isinstance(item, dict):
                continue
            filename = Path(str(item.get("filename") or item.get("file") or "")).name
            if not filename:
                continue
            subfolder = str(item.get("subfolder") or "").strip().replace("\\", "/")
            candidates = []
            if subfolder and ".." not in subfolder.split("/"):
                candidates.append(outputs_dir / subfolder / filename)
            item_task_id = str(item.get("task_id") or item.get("taskId") or task_id)
            if item_task_id:
                candidates.append(outputs_dir / item_task_id / filename)
            candidates.append(outputs_dir / filename)
            for candidate in candidates:
                try:
                    if candidate.exists() and candidate.is_file():
                        os.startfile(str(candidate))
                        return
                except Exception:
                    continue
        os.startfile(str(outputs_dir))

    def _open_models(self):
        models_dir = BASE_DIR / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(models_dir))

    def _open_runtime_dir(self):
        runtime_dir = BASE_DIR / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(runtime_dir))

    def _open_logs_dir(self):
        log_dir = self._backend_log_dir()
        os.startfile(str(log_dir))

    def _open_runtime_config(self):
        config_path = BASE_DIR / "runtime" / "config.local.json"
        if config_path.exists():
            os.startfile(str(config_path))
        else:
            self._open_runtime_dir()

    def _start_background_model_recheck(self):
        threading.Thread(target=self._recheck_models, daemon=True).start()

    def _start_background_runtime_recheck(self):
        """Check package, GPU and PyTorch in the background, then refresh the card."""
        if not self._begin_backend_action("检查环境"):
            return
        self._set_light("env", "loading", "检查中")
        self._footer_label.config(text="  正在检查运行环境...")
        try:
            threading.Thread(
                target=self._environment_recheck_worker,
                daemon=True,
            ).start()
        except Exception:
            self._end_backend_action()
            raise

    def _environment_recheck_worker(self):
        try:
            self._retry_env_check()
        except Exception as exc:
            self.after(
                0,
                lambda message=str(exc): self._finish_runtime_recheck(
                    {
                        "package_ready": not missing_runtime_paths(BASE_DIR),
                        "ready": False,
                        "missing": missing_runtime_paths(BASE_DIR),
                        "gpu_name": "",
                        "message": f"环境检查失败：{message}",
                    }
                ),
            )
        finally:
            self._end_backend_action()

    def _show_workflow_tutorial(self):
        popup = tk.Toplevel(self)
        popup.title("添加工作流教程")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(True, True)
        popup.minsize(620, 440)
        self._center_popup(popup, 660, 480)

        panel = self._card(popup, fill="both", expand=True, padx=18, pady=18)
        tk.Label(panel, text="三步添加自己的 ComfyUI 工作流", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w", padx=24, pady=(22, 14))
        steps = [
            ("1", "导出 API 工作流", "在 ComfyUI 中选择“保存（API 格式）”，得到可供程序调用的 JSON 文件。"),
            ("2", "添加到客户端", "点击“添加工作流”，选择 JSON 文件或包含工作流文件的文件夹。"),
            ("3", "确认输入与输出", "客户端会自动识别提示词、图片、输出节点和模型；只有识别不明确时才需要手动确认。"),
        ]
        for number, title, detail in steps:
            row = tk.Frame(panel, bg=C["card"])
            row.pack(fill="x", padx=24, pady=7)
            tk.Label(row, text=number, font=F["bold"], fg="#fff", bg=C["primary"], width=3, pady=6).pack(side="left", padx=(0, 12))
            text_box = tk.Frame(row, bg=C["card"])
            text_box.pack(side="left", fill="x", expand=True)
            tk.Label(text_box, text=title, font=F["h2"], fg=C["text"], bg=C["card"]).pack(anchor="w")
            tk.Label(text_box, text=detail, wraplength=520, justify="left", font=F["small"], fg=C["text2"], bg=C["card"]).pack(anchor="w", pady=(4, 0))

        note = tk.Frame(panel, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
        note.pack(fill="x", padx=24, pady=(12, 14))
        tk.Label(note, text="专业用户", font=F["bold"], fg=C["primary"], bg=C["hover"]).pack(anchor="w", padx=12, pady=(9, 2))
        tk.Label(note, text="导入后可在“查看参数”中检查英文调用名称、公开输入、输出节点和工作流结构。", font=F["small"], fg=C["text2"], bg=C["hover"]).pack(anchor="w", padx=12, pady=(0, 9))
        self._button(panel, "开始添加工作流", lambda: (popup.destroy(), self._show_workflow_upload_dialog()), "primary", width=132).pack(anchor="e", padx=24, pady=(0, 18))

    def _install_runtime(self):
        """Compatibility entry: installation now lives in the maintenance center."""
        self._open_runtime_maintenance()

    def _show_runtime_maintenance(self):
        """Show advanced runtime maintenance without leaving the resources page."""
        existing = self._runtime_maintenance_popup
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except Exception:
            self._runtime_maintenance_popup = None

        missing = missing_runtime_paths(BASE_DIR)
        ready = not missing
        popup = tk.Toplevel(self)
        self._runtime_maintenance_popup = popup
        popup.title("运行环境维护")
        popup.configure(bg=C["bg"])
        popup.transient(self)
        popup.resizable(False, False)
        self._center_popup(popup, 650, 370)

        def close_popup():
            self._runtime_maintenance_popup = None
            try:
                popup.destroy()
            except Exception:
                pass

        popup.protocol("WM_DELETE_WINDOW", close_popup)
        panel = self._card(popup, fill="both", expand=True, padx=16, pady=16)

        header = tk.Frame(panel, bg=C["card"])
        header.pack(fill="x", padx=22, pady=(20, 12))
        header_text = tk.Frame(header, bg=C["card"])
        header_text.pack(side="left", fill="x", expand=True)
        tk.Label(header_text, text="运行环境维护", font=F["title"], fg=C["text"], bg=C["card"]).pack(anchor="w")
        tk.Label(
            header_text,
            text="环境与模型分开保存；修复环境不会删除模型、工作流和生成结果。",
            font=F["small"],
            fg=C["text2"],
            bg=C["card"],
        ).pack(anchor="w", pady=(5, 0))
        status_text = "环境文件完整" if ready else f"需要补齐 {len(missing)} 项"
        status_fg = C["success"] if ready else C["warn"]
        tk.Label(header, text=f"  {status_text}  ", font=F["small"], fg=status_fg,
                 bg=C["soft_success"] if ready else C["soft_warn"], padx=5, pady=4).pack(side="right")

        def maintenance_row(title, detail, actions):
            row = tk.Frame(panel, bg=C["hover"], highlightthickness=1, highlightbackground=C["border2"])
            row.pack(fill="x", padx=22, pady=5)
            text_box = tk.Frame(row, bg=C["hover"])
            text_box.pack(side="left", fill="both", expand=True, padx=14, pady=11)
            tk.Label(text_box, text=title, font=F["bold"], fg=C["text"], bg=C["hover"]).pack(anchor="w")
            tk.Label(text_box, text=detail, font=F["small"], fg=C["text2"], bg=C["hover"]).pack(anchor="w", pady=(4, 0))
            action_box = tk.Frame(row, bg=C["hover"])
            action_box.pack(side="right", padx=12)
            for index, (label, command, variant, width) in enumerate(actions):
                self._button(action_box, label, command, variant, width=width).pack(
                    side="left", padx=(0 if index == 0 else 7, 0)
                )

        maintenance_row(
            "在线安装或修复",
            "自动下载已配置的环境包，校验后安装；完成后自动恢复后台服务。",
            [("开始", lambda: (close_popup(), self._install_runtime_from_mirror()), "primary", 76)],
        )
        maintenance_row(
            "使用本地环境包",
            "适合已经下载好 7z 环境包，或将环境包放在客户端同目录的情况。",
            [
                ("选择文件", lambda: (close_popup(), self._select_runtime()), "plain", 82),
                ("同目录查找", lambda: (close_popup(), self._install_local_runtime()), "plain", 92),
            ],
        )

        footer = tk.Frame(panel, bg=C["card"])
        footer.pack(fill="x", padx=22, pady=(14, 18))
        self._button(footer, "检查环境", lambda: (close_popup(), self._start_background_runtime_recheck()), "primary", width=88).pack(side="left")
        self._button(footer, "打开运行目录", self._open_runtime_dir, "plain", width=102).pack(side="left", padx=(8, 0))
        self._button(footer, "查看运行日志", self._open_logs_dir, "plain", width=102).pack(side="left", padx=(8, 0))
        self._button(footer, "关闭", close_popup, "plain", width=72).pack(side="right")

    def _import_models(self):
        if self._model_import_in_progress:
            self._footer_label.config(text="  已有模型导入任务正在运行，请稍候")
            return
        if self._has_active_model_transfers():
            self._footer_label.config(text="  模型正在下载，完成后再导入已有模型")
            messagebox.showinfo(
                "模型下载进行中",
                "请等待当前模型下载完成后再导入，避免两个任务同时写入同一个模型文件。",
                parent=self,
            )
            return
        path = filedialog.askdirectory(title="选择模型目录")
        if not path:
            return
        src = Path(path)
        models_dir = BASE_DIR / "models"
        transfer_lock, transfers = self._model_transfer_state()
        with transfer_lock:
            if transfers or self._model_import_in_progress:
                self._footer_label.config(text="  另一个模型维护任务已开始，请稍候")
                return
            self._model_import_in_progress = True

        def report_progress(index: int, total: int, filename: str):
            if not self._shutting_down:
                self.after(
                    0,
                    lambda: self._footer_label.config(
                        text=f"  正在导入模型 {index}/{total}：{filename}"
                    ),
                )

        def finish(result: dict):
            with self._model_transfer_state()[0]:
                self._model_import_in_progress = False
            if self._shutting_down:
                return
            self._model_status = _check_models_status()
            self._set_light(
                "models",
                "online" if self._model_status.get("all_ok") else "offline",
                "完整" if self._model_status.get("all_ok") else "缺失",
            )
            self._update_model_display()
            if hasattr(self, "_dashboard_pages"):
                self._dashboard_pages.refresh(self._last_health)

            summary = (
                f"已导入 {result.get('imported', 0)} 个，"
                f"跳过 {result.get('skipped', 0)} 个，失败 {result.get('failed', 0)} 个"
            )
            if not result.get("found"):
                self._footer_label.config(text="  未找到支持的模型文件")
                messagebox.showwarning(
                    "未找到模型",
                    "所选目录中没有找到 safetensors、GGUF、PT、PTH、BIN 或 CKPT 模型文件。",
                    parent=self,
                )
            elif result.get("failed"):
                self._footer_label.config(text=f"  模型导入完成：{summary}")
                detail = "\n".join((result.get("errors") or [])[:5])
                messagebox.showwarning(
                    "部分模型导入失败",
                    f"{summary}\n\n{detail}",
                    parent=self,
                )
            else:
                self._footer_label.config(text=f"  模型导入完成：{summary}")

        def fail(message: str):
            with self._model_transfer_state()[0]:
                self._model_import_in_progress = False
            if self._shutting_down:
                return
            self._footer_label.config(text=f"  模型导入失败：{message}")
            messagebox.showerror("模型导入失败", message, parent=self)

        def _do_copy():
            try:
                result = import_model_directory(src, models_dir, on_progress=report_progress)
            except Exception as exc:
                if not self._shutting_down:
                    self.after(0, lambda error=str(exc): fail(error))
                return
            if not self._shutting_down:
                self.after(0, lambda data=result: finish(data))

        self._footer_label.config(text="  正在扫描并导入模型...")
        threading.Thread(target=_do_copy, daemon=True).start()

    def _recheck_models(self):
        self.after(0, lambda: self._set_light("models", "loading"))
        time.sleep(0.3)
        self._model_status = _check_models_status()
        if self._model_status["all_ok"]:
            self.after(0, lambda: self._set_light("models", "online", "完整"))
        else:
            self.after(0, lambda: self._set_light("models", "offline", "缺失"))
        self.after(0, self._update_model_display)
        if hasattr(self, "_dashboard_pages"):
            self.after(0, lambda: self._dashboard_pages.refresh(self._last_health))

    def _finish_runtime_recheck(self, result: dict):
        if self._shutting_down:
            return
        self._environment_status = dict(result)
        if result.get("ready"):
            self._set_light("env", "online", "可用")
            gpu_name = str(result.get("gpu_name") or "显卡环境正常")
            self._footer_label.config(text=f"  运行环境检查通过：{gpu_name}")
        elif result.get("package_ready"):
            self._set_light("env", "offline", "需处理")
            self._footer_label.config(text=f"  运行环境需要处理：{result.get('message') or '检查未通过'}")
        else:
            self._set_light("env", "offline", "未安装")
            self._footer_label.config(text=f"  {result.get('message') or '运行环境尚未安装'}")
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(self._last_health)

    def _restart_backend(self):
        """重启所有后台服务"""
        if not self._begin_backend_action("重启后台"):
            return
        self._footer_label.config(text="  正在重启后台服务...")
        self._clear_public_url()
        for key in ("comfyui", "api", "tunnel"):
            self._set_light(key, "loading", "重启中")
        try:
            threading.Thread(target=self._restart_backend_worker, daemon=True).start()
        except Exception:
            self._end_backend_action()
            raise

    def _restart_backend_worker(self):
        try:
            errors = self._shutdown_backend()
            if errors:
                self.after(
                    0,
                    lambda message=errors: self._report_backend_failure(
                        f"后台服务未能完全停止：{message}"
                    ),
                )
                return
            if self._shutting_down or self._runtime_maintenance_active():
                return
            self._startup_sequence()
        except Exception as exc:
            self.after(
                0,
                lambda message=str(exc): self._report_backend_failure(
                    f"后台重启失败：{message}"
                ),
            )
        finally:
            self._end_backend_action()

    def _report_backend_failure(self, message: str):
        if self._shutting_down:
            return
        for key in ("comfyui", "api", "tunnel"):
            self._set_light(key, "offline", "重试可用")
        self._footer_label.config(text=f"  {message}")
        self._ensure_health_polling()

    def _begin_backend_action(self, action_name: str) -> bool:
        """Reserve one service lifecycle action without blocking the UI."""
        if self._shutting_down:
            return False
        if self._runtime_maintenance_active():
            self.after(
                0,
                lambda: self._footer_label.config(
                    text="  正在维护运行环境，完成后会自动恢复后台服务"
                ),
            )
            return False
        lock = self._backend_action_lock
        if not lock.acquire(blocking=False):
            current = self._backend_action_name or "后台操作"
            self.after(
                0,
                lambda name=current: self._footer_label.config(
                    text=f"  {name}正在进行，请稍候"
                ),
            )
            return False
        if self._shutting_down or self._runtime_maintenance_active():
            lock.release()
            return False
        self._backend_action_name = str(action_name or "后台操作")
        return True

    def _end_backend_action(self):
        self._backend_action_name = ""
        try:
            self._backend_action_lock.release()
        except RuntimeError:
            pass

    # ══════════════════════════════════════════════════════
    # 后台启动 ComfyUI + API + Tunnel
    # ══════════════════════════════════════════════════════
    def _backend_env(self):
        python_exe = sys.executable  # 使用当前 venv Python（GUI 自己）
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return python_exe, env

    def _backend_log_dir(self) -> Path:
        log_dir = BASE_DIR / "runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def _start_comfyui_service(self):
        lock = self._runtime_maintenance_lock
        if not lock.acquire(blocking=False):
            return
        try:
            if self._shutting_down or self._runtime_maintenance_in_progress:
                return
            return self._start_comfyui_service_unlocked()
        finally:
            lock.release()

    def _start_comfyui_service_unlocked(self):
        """只启动或复检 ComfyUI，不重启 API/Tunnel。"""
        if self._shutting_down:
            return
        self._set_light("comfyui", "loading")
        self._comfy_starting_until = time.time() + 150
        python_exe, env = self._backend_env()
        log_dir = self._backend_log_dir()
        # 确保 outputs 目录存在
        outputs_dir = BASE_DIR / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)

        # ── ComfyUI 使用相对路径 output-directory ──
        # ComfyUI cwd = runtime/ComfyUI，相对路径 ../../outputs 指向根目录 outputs/
        port_ready, port_error = self._process_supervisor.prepare_port(COMFY_PORT)
        if not port_ready:
            self._comfy_proc = None
            self._set_light("comfyui", "offline", "端口占用")
            self._footer_label.config(text=f"  ComfyUI 无法启动：{port_error}")
            print(f"[GUI] ComfyUI start blocked: {port_error}")
        else:
            comfy_log = open(str(log_dir / "comfyui.log"), "w")
            comfy_command = [
                str(python_exe), "main.py",
                "--listen", "127.0.0.1", "--port", str(COMFY_PORT),
                "--disable-auto-launch",
                *_comfy_vram_args(),
                "--output-directory", "../../outputs",
                "--extra-model-paths-config", "extra_model_paths.yaml",
            ]
            try:
                self._comfy_proc = self._process_supervisor.launch(
                    "comfyui",
                    comfy_command,
                    cwd=str(BASE_DIR / "runtime" / "ComfyUI"),
                    env=env,
                    stdout=comfy_log, stderr=comfy_log,
                )
            finally:
                comfy_log.close()
            print("[GUI] ComfyUI starting (cwd=runtime/ComfyUI, output=../../outputs)...")

    def _start_api_service(self):
        lock = self._runtime_maintenance_lock
        if not lock.acquire(blocking=False):
            return
        try:
            if self._shutting_down or self._runtime_maintenance_in_progress:
                return
            return self._start_api_service_unlocked()
        finally:
            lock.release()

    def _start_api_service_unlocked(self):
        """只启动 API 服务，不重启 ComfyUI。API 内部会自行启动/恢复 Tunnel。"""
        if self._shutting_down:
            return
        self._clear_public_url()
        self._set_light("api", "loading")
        self._set_light("tunnel", "loading")
        python_exe, env = self._backend_env()
        log_dir = self._backend_log_dir()
        port_ready, port_error = self._process_supervisor.prepare_port(API_PORT)
        if not port_ready:
            self._api_proc = None
            self._set_light("api", "offline", "端口占用")
            self._set_light("tunnel", "offline", "未启动")
            self._footer_label.config(text=f"  API 无法启动：{port_error}")
            print(f"[GUI] API start blocked: {port_error}")
            return
        api_log = open(str(log_dir / "api.log"), "w")
        try:
            self._api_proc = self._process_supervisor.launch(
                "api",
                [str(python_exe), str(BASE_DIR / "app" / "server.py")],
                cwd=str(BASE_DIR), env=env,
                stdout=api_log, stderr=api_log,
            )
        finally:
            api_log.close()
        print("[GUI] API server starting...")

    def _start_backend(self):
        """启动 ComfyUI 引擎，然后启动 API 服务器"""
        if self._shutting_down or self._runtime_maintenance_active():
            return
        self._set_light("comfyui", "loading")
        self._set_light("api", "loading")
        self._set_light("tunnel", "loading")
        self._start_comfyui_service()
        self._start_api_service()
        self._ensure_health_polling()

    def _ensure_health_polling(self):
        """确保只有一条健康检查轮询线程在跑。"""
        if self._shutting_down:
            self._poll_run = False
            return
        with self._health_poll_lock:
            if (
                self._poll_run
                and self._health_poll_thread
                and self._health_poll_thread.is_alive()
            ):
                return
            self._health_poll_generation += 1
            generation = self._health_poll_generation
            self._poll_run = True
            worker = threading.Thread(
                target=self._poll_health,
                args=(generation,),
                daemon=True,
            )
            self._health_poll_thread = worker
            try:
                worker.start()
            except Exception:
                self._poll_run = False
                raise

    def _invalidate_health_polling(self):
        """Retire the current poll before an intentional API stop/restart."""
        with self._health_poll_lock:
            self._health_poll_generation += 1
            self._poll_run = False

    def _health_poll_active(self, generation: int) -> bool:
        return (
            not self._shutting_down
            and self._poll_run
            and generation == self._health_poll_generation
        )

    def _finish_health_poll(self, generation: int) -> bool:
        """Stop only the current poll generation, never a replacement."""
        with self._health_poll_lock:
            if generation != self._health_poll_generation:
                return False
            self._poll_run = False
            return True

    def _deliver_health_snapshot(
        self,
        generation: int,
        data: dict,
        first: bool = False,
    ):
        """Discard a queued response if a newer poll generation replaced it."""
        if not self._health_poll_active(generation):
            return
        if first:
            self._on_first_health(data)
        else:
            self._on_health_update(data)

    def _deliver_health_failure(self, generation: int):
        """Apply failure only when no newer poll has already taken over."""
        with self._health_poll_lock:
            current_failure = (
                generation == self._health_poll_generation
                and not self._poll_run
                and not self._shutting_down
            )
        if current_failure:
            self._on_server_unreachable()

    def _poll_health(self, generation: int | None = None):
        if generation is None:
            with self._health_poll_lock:
                if self._shutting_down:
                    self._poll_run = False
                    return
                self._health_poll_generation += 1
                generation = self._health_poll_generation
                self._poll_run = True
        if not self._health_poll_active(generation):
            return
        import urllib.request as ur

        connected = False
        for i in range(60):
            if not self._health_poll_active(generation):
                return
            try:
                resp = ur.urlopen(f"{API_BASE}/health", timeout=5)
                data = json.loads(resp.read().decode())
                if not self._health_poll_active(generation):
                    return
                self.after(
                    0,
                    lambda g=generation, d=data: self._deliver_health_snapshot(
                        g, d, first=True
                    ),
                )
                connected = True
                break
            except Exception:
                time.sleep(1)

        if not connected:
            if self._finish_health_poll(generation) and not self._shutting_down:
                self.after(
                    0,
                    lambda g=generation: self._deliver_health_failure(g),
                )
            return

        # 持续轮询（含进度）
        consecutive_failures = 0
        while self._health_poll_active(generation):
            try:
                resp = ur.urlopen(f"{API_BASE}/health", timeout=5)
                data = json.loads(resp.read().decode())
                if not self._health_poll_active(generation):
                    return
                consecutive_failures = 0
                self.after(
                    0,
                    lambda g=generation, d=data: self._deliver_health_snapshot(g, d),
                )
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    if self._finish_health_poll(generation) and not self._shutting_down:
                        self.after(
                            0,
                            lambda g=generation: self._deliver_health_failure(g),
                        )
                    return
            time.sleep(3)

    def _on_first_health(self, data: dict):
        """首次获取健康状态"""
        if self._shutting_down:
            return
        self._last_health = data
        self._update_status(data)

        sess = BASE_DIR / "runtime" / "session.json"
        if sess.exists():
            with open(sess, "r", encoding="utf-8") as f:
                session = json.load(f)
            self._api_key = session.get("api_key", "")
            self._set_api_key(self._api_key)

        self._update_workflow_display(data)
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(data)
        self._refresh_saved_login_and_sync()

    def _on_health_update(self, data: dict):
        if self._shutting_down:
            return
        self._last_health = data
        self._update_status(data)
        self._update_workflow_display(data)
        if hasattr(self, "_dashboard_pages"):
            self._dashboard_pages.refresh(data)

        # 进度更新
        task = data.get("current_task")
        self._update_task_display(task)

    def _update_status(self, data: dict):
        url = data.get("base_url", "")
        tunnel_data = data.get("tunnel", {})
        comfy_data = data.get("comfyui", {})

        self._set_light("api", "online")

        ts = tunnel_data.get("status", "offline")
        if ts == "online":
            self._set_light("tunnel", "online")
        elif ts == "unavailable":
            self._set_light("tunnel", "offline", self._tunnel_error_label(tunnel_data.get("error", "")))
        elif ts in ("starting", "retrying"):
            self._set_light("tunnel", "loading")
        else:
            self._set_light("tunnel", "offline", "未连接")

        if ts != "online" or not url:
            if self._tunnel_url:
                self._clear_public_url()

        cs = comfy_data.get("status", "offline")
        if cs == "online":
            self._set_light("comfyui", "online")
        else:
            comfy_starting = (
                self._comfy_proc
                and self._comfy_proc.poll() is None
                and time.time() < self._comfy_starting_until
            )
            if cs == "offline" and comfy_starting:
                self._set_light("comfyui", "loading", "启动中")
            else:
                self._set_light("comfyui", "offline" if cs == "offline" else "loading")

        if ts == "online" and url and url != self._tunnel_url:
            self._tunnel_url = url
            self._set_public_url(url)
            self._initial_session_sync_done = False
            if self._server_mode == "logged_in" and self._server_session_token and self._api_key:
                threading.Thread(target=self._refresh_saved_login_and_sync, daemon=True).start()

        if ts == "online":
            parts = ["公网连接已建立"]
        elif ts in ("starting", "retrying"):
            parts = ["正在准备公网连接"]
        elif ts == "unavailable":
            error = str(tunnel_data.get("error") or "").strip()
            parts = [f"Tunnel：{error or '连接失败'}"]
        else:
            parts = ["公网连接未建立"]
        self._footer_label.config(text="  " + "  ".join(parts))

    def _tunnel_error_label(self, error: str) -> str:
        text = str(error or "").strip()
        lowered = text.lower()
        if "cloudflared not found" in lowered or "not found:" in lowered:
            return "未安装"
        if "not reachable" in lowered:
            return "公网不可达"
        if "failed to obtain tunnel url" in lowered:
            return "获取 URL 失败"
        if text:
            return "连接失败"
        return "未连接"

    def _set_light(self, key: str, status: str, custom_text: str = ""):
        """设置状态灯
        status: "loading" | "online" | "offline"
        """
        group = self._light_groups.get(key)
        if not group:
            return
        dot, dot_id, lbl, status_lbl = group[:4]

        self._light_states[key] = status

        if status == "online":
            color = C["success"]
            text = custom_text or "已连接"
        elif status == "loading":
            color = C["warn"]
            text = custom_text or "加载中..."
        else:
            color = C["error"]
            text = custom_text or "失败"

        dot.itemconfig(dot_id, fill=color)
        if lbl:
            lbl.config(fg=C["text"])
        if status_lbl:
            status_lbl.config(text=text, fg=color)
        if len(group) >= 5:
            retry_btn = group[4]
            if status == "offline":
                if not retry_btn.winfo_ismapped():
                    retry_btn.pack(side="left", padx=(4, 0))
            elif retry_btn.winfo_ismapped():
                retry_btn.pack_forget()

    def _retry_component(self, key: str):
        if self._shutting_down:
            return
        if self._runtime_maintenance_active() and key in {"env", "comfyui", "api", "tunnel"}:
            self._footer_label.config(text="  正在维护运行环境，请稍候")
            return
        if key == "models":
            threading.Thread(target=self._recheck_models, daemon=True).start()
            return
        if key == "env":
            self._start_background_runtime_recheck()
            return
        if key == "comfyui":
            threading.Thread(target=self._retry_comfyui_service, daemon=True).start()
            return
        if key == "api":
            threading.Thread(target=self._retry_api_service, daemon=True).start()
            return
        if key == "tunnel":
            threading.Thread(target=self._retry_tunnel_service, daemon=True).start()
            return
        if key == "server":
            threading.Thread(
                target=lambda: self._sync_to_server_with_retry(max_retries=SERVER_SYNC_MAX_RETRIES),
                daemon=True,
            ).start()
            return
        self._request_backend_start("启动后台")

    def _retry_env_check(self):
        if self._shutting_down or self._runtime_maintenance_active():
            return
        missing = missing_runtime_paths(BASE_DIR)
        result = {
            "package_ready": not missing,
            "ready": False,
            "missing": missing,
            "gpu_name": "",
            "message": "",
        }
        if missing:
            result["message"] = f"运行环境不完整，缺少 {len(missing)} 项"
            if not self._shutting_down:
                self.after(0, lambda data=result: self._finish_runtime_recheck(data))
            return

        system_info = _check_system_env(
            lambda args, **kwargs: self._run_runtime_probe(
                "environment-check", args, **kwargs
            )
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return
        torch_info = _check_torch(
            BASE_DIR / "runtime" / "python" / "python.exe",
            lambda args, **kwargs: self._run_runtime_probe(
                "torch-check", args, **kwargs
            ),
        )
        if self._shutting_down or self._runtime_maintenance_active():
            return

        result["gpu_name"] = str(torch_info.get("gpu_name") or system_info.get("gpu_name") or "")
        result["ready"] = bool(system_info.get("success") and torch_info.get("success"))
        if not result["ready"]:
            result["message"] = str(
                torch_info.get("error")
                or system_info.get("error")
                or "显卡或 PyTorch 检查未通过"
            )
        self.after(0, lambda data=result: self._finish_runtime_recheck(data))

    def _retry_comfyui_service(self):
        if not self._begin_backend_action("ComfyUI 重试"):
            return
        try:
            self.after(0, lambda: self._set_light("comfyui", "loading", "重试中"))
            error = self._kill_proc(self._comfy_proc, "ComfyUI")
            if error:
                self.after(0, lambda e=error: self._set_light("comfyui", "offline", "关闭失败"))
                self.after(0, lambda e=error: self._footer_label.config(text=f"  ComfyUI 无法重启：{e}"))
                return
            self._comfy_proc = None
            if not self._shutting_down and not self._runtime_maintenance_active():
                if self._run_ui_backend_step(self._start_comfyui_service):
                    self._ensure_health_polling()
        finally:
            self._end_backend_action()

    def _retry_api_service(self):
        if not self._begin_backend_action("API 重试"):
            return
        try:
            self.after(0, lambda: self._set_light("api", "loading", "重试中"))
            self._invalidate_health_polling()
            error = self._kill_proc(self._api_proc, "API")
            if error:
                self._ensure_health_polling()
                self.after(0, lambda e=error: self._set_light("api", "offline", "关闭失败"))
                self.after(0, lambda e=error: self._footer_label.config(text=f"  API 无法重启：{e}"))
                return
            self._api_proc = None
            if not self._shutting_down and not self._runtime_maintenance_active():
                if self._run_ui_backend_step(self._start_api_service):
                    self._ensure_health_polling()
        finally:
            self._end_backend_action()

    def _retry_tunnel_service(self):
        if not self._begin_backend_action("Tunnel 重试"):
            return
        try:
            import urllib.request as ur

            self.after(0, lambda: self._set_light("tunnel", "loading", "重试中"))
            if not self._run_ui_backend_step(self._clear_public_url):
                return
            api_key = self._current_local_api_key()
            if not api_key:
                raise RuntimeError("本地 API Key 尚未生成，请先重试 API 服务")
            req = ur.Request(
                f"{API_BASE}/v1/tunnel/restart",
                data=b"{}",
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            raw = ur.urlopen(req, timeout=20).read()
            result = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(result, dict) or not result.get("ok"):
                detail = result.get("error") if isinstance(result, dict) else ""
                raise RuntimeError(str(detail or "Tunnel 未能开始重连"))
            if not self._shutting_down:
                self._ensure_health_polling()
        except Exception as exc:
            if not self._shutting_down:
                self.after(0, lambda e=str(exc): self._set_light("tunnel", "offline", "重试失败"))
                self.after(0, lambda e=str(exc): self._footer_label.config(text=f"  Tunnel 重试失败：{e}"))
        finally:
            self._end_backend_action()

    def _on_server_unreachable(self):
        if self._shutting_down:
            return
        self._set_light("api", "offline")
        self._set_light("tunnel", "offline")
        self._set_light("comfyui", "offline")
        self._api_key = ""
        self._clear_public_url()
        self._key_label.config(text="（无法连接）")
        self._url_label.config(text="API 服务启动失败")
        self._footer_label.config(text="服务启动失败 — 查看 runtime/logs/")

    # ══════════════════════════════════════════════════════
    # 工作流更新
    # ══════════════════════════════════════════════════════
    def _update_workflow_display(self, data: dict):
        """按本地 API 返回的工作流列表动态刷新界面。"""
        if not hasattr(self, "_wf_sections"):
            return
        workflows = data.get("workflows") if isinstance(data, dict) else []
        if isinstance(workflows, int) or not isinstance(workflows, list):
            workflows = []

        for box in self._wf_sections.values():
            for child in box.winfo_children()[1:]:
                child.destroy()
        self._wf_rows = {}

        counts = {"image": 0, "video": 0, "text": 0}
        available_count = 0
        total_count = 0

        valid_ids = set()
        first_valid_id = ""
        default_workflow_id = (
            str(data.get("default_workflow_id") or data.get("default_workflow") or "").strip()
            if isinstance(data, dict)
            else ""
        )
        for workflow in workflows:
            if not isinstance(workflow, dict):
                continue
            wf_id = str(workflow.get("id") or "").strip()
            if not wf_id:
                continue
            group = self._workflow_group_for_display(workflow)
            parent_box = self._wf_sections.get(group) or self._wf_sections["image"]
            self._add_workflow_row(parent_box, workflow, group)
            counts[group] = counts.get(group, 0) + 1
            valid_ids.add(wf_id)
            if not first_valid_id:
                first_valid_id = wf_id
            total_count += 1
            if workflow.get("enabled", True) and self._workflow_model_available(workflow):
                available_count += 1

        empty_labels = {
            "image": "暂无图片工作流",
            "video": "暂无视频工作流",
            "text": "暂无文字工作流",
        }
        for group, box in self._wf_sections.items():
            visible_count = counts.get(group, 0)
            if not visible_count:
                tk.Label(
                    box,
                    text=empty_labels.get(group, "暂无工作流"),
                    font=F["small"],
                    fg=C["muted"],
                    bg=C["card"],
                ).pack(anchor="w", pady=(3, 6), padx=(24, 0))

        if self._workflow_mode.get() not in valid_ids:
            if default_workflow_id in valid_ids:
                self._workflow_mode.set(default_workflow_id)
            else:
                self._workflow_mode.set(first_valid_id or "")
        if hasattr(self, "_model_hint_label"):
            self._model_hint_label.config(text="")

    # ══════════════════════════════════════════════════════
    # 系统托盘
    # ══════════════════════════════════════════════════════
    def _create_tray_icon(self):
        try:
            from PIL import Image, ImageDraw
            import pystray

            icon_path = BASE_DIR / "icon.png"
            if icon_path.exists():
                img = Image.open(icon_path)
            else:
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=C["primary"])
                draw.text((18, 16), "AI", fill="white")

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._restore_from_tray, default=True),
                pystray.MenuItem("退出", lambda *_args: self.after(0, self._on_close)),
            )
            self._tray = pystray.Icon("ai_gateway", img, "灵镜造片厂", menu)
            self._tray.run()
        except Exception as e:
            print(f"[Tray] 托盘创建失败: {e}")

    def _restore_from_tray(self):
        self.after(0, self._do_restore)

    def _do_restore(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None

    def _on_minimize(self, event):
        if self._shutting_down:
            return
        if self.state() == "iconic":
            self.withdraw()
            if self._tray is None:
                self._tray_thread = threading.Thread(target=self._create_tray_icon, daemon=True)
                self._tray_thread.start()

    # ══════════════════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════════════════
    def _copy(self, text: str):
        if not text or text in ("等待隧道...", "生成中...", "—", "API 服务启动失败"):
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._footer_label.config(text="  已复制到剪贴板")
        self.after(3000, lambda: self._footer_label.config(text=""))

    def _center_popup(self, popup, w, h):
        popup.update_idletasks()
        px = self.winfo_x() + (self.winfo_width() - w) // 2
        py = self.winfo_y() + (self.winfo_height() - h) // 2
        popup.geometry(f"{w}x{h}+{px}+{py}")

    # ══════════════════════════════════════════════════════
    # 关闭确认 + 终止后台
    # ══════════════════════════════════════════════════════
    def _on_close(self):
        """Close the window only after every client-owned service is stopped."""
        if self._shutting_down:
            return
        confirmed = messagebox.askyesno(
            "确认退出",
            "确定要退出灵镜造片厂吗？\n\n"
            "退出后将关闭：\n"
            "  - ComfyUI\n"
            "  - 本地 API 服务\n"
            "  - Cloudflare Tunnel\n"
            "  - 当前生成任务",
            parent=self,
        )
        if not confirmed:
            return

        self._shutting_down = True
        self._anim_running = False
        self._heartbeat_run = False
        self._poll_run = False
        if self._server_session_token:
            threading.Thread(target=self._notify_server_offline, daemon=True).start()

        popup = tk.Toplevel(self)
        popup.title("正在关闭")
        popup.configure(bg=C["surface"])
        popup.transient(self)
        popup.grab_set()
        popup.resizable(False, False)
        popup.protocol("WM_DELETE_WINDOW", lambda: None)
        self._center_popup(popup, 440, 315)

        title_label = tk.Label(
            popup,
            text="正在彻底关闭后台服务...",
            font=F["bold"],
            fg=C["text"],
            bg=C["surface"],
        )
        title_label.pack(pady=(24, 12))

        list_frame = tk.Frame(popup, bg=C["surface"])
        list_frame.pack(fill="x", padx=30)

        items = {}
        for name in ["ComfyUI", "API 服务", "Tunnel", "后台操作"]:
            row = tk.Frame(list_frame, bg=C["surface"])
            row.pack(fill="x", pady=3)
            status_lbl = tk.Label(
                row,
                text="等待...",
                font=F["small"],
                fg=C["warn"],
                bg=C["surface"],
                width=13,
                anchor="w",
            )
            status_lbl.pack(side="left")
            name_lbl = tk.Label(
                row,
                text=name,
                font=F["small"],
                fg=C["text2"],
                bg=C["surface"],
                anchor="w",
            )
            name_lbl.pack(side="left", padx=(8, 0))
            items[name] = status_lbl

        close_btn = tk.Button(
            popup,
            text="再次强制清理并退出",
            font=F["small"],
            bg=C["error"],
            fg="#fff",
            activebackground="#c0392b",
            relief="flat",
            bd=0,
            cursor="hand2",
            command=lambda: self._force_destroy(popup),
            state="disabled",
        )
        close_btn.pack(pady=(14, 0), ipadx=10, ipady=4)
        error_label = tk.Label(
            popup,
            text="",
            font=F["tiny"],
            fg=C["error"],
            bg=C["surface"],
            wraplength=380,
            justify="left",
        )
        error_label.pack(fill="x", padx=30, pady=(8, 0))
        popup._shutdown_title = title_label
        popup._shutdown_error = error_label
        popup._shutdown_button = close_btn

        def update_item(name: str, text: str, color: str):
            label = items.get(name)
            if label is not None:
                label.config(text=text, fg=color)

        def finish_success():
            title_label.config(text="后台服务已全部停止", fg=C["success"])
            self.after(250, lambda: self._finish_shutdown(popup))

        def _do_shutdown():
            self.after(0, lambda: update_item("ComfyUI", "关闭中...", C["warn"]))
            err = self._process_supervisor.terminate("comfyui", timeout=8)
            self.after(0, lambda e=err: update_item("ComfyUI", "已停止 ✓" if not e else "需要清理", C["success"] if not e else C["error"]))
            if not self._process_supervisor.is_running("comfyui"):
                self._comfy_proc = None

            self.after(0, lambda: update_item("API 服务", "关闭中...", C["warn"]))
            self.after(0, lambda: update_item("Tunnel", "关闭中...", C["warn"]))
            api_error = self._process_supervisor.terminate("api", timeout=8)
            self.after(0, lambda e=api_error: update_item("API 服务", "已停止 ✓" if not e else "需要清理", C["success"] if not e else C["error"]))
            self.after(0, lambda e=api_error: update_item("Tunnel", "已停止 ✓" if not e else "需要清理", C["success"] if not e else C["error"]))
            if not self._process_supervisor.is_running("api"):
                self._api_proc = None

            self.after(0, lambda: update_item("后台操作", "关闭中...", C["warn"]))
            retry_results = self._process_supervisor.shutdown_all(timeout=8)
            remaining = self._process_supervisor.remaining()
            comfy_left = bool(remaining.get("comfyui"))
            api_left = bool(remaining.get("api"))
            self.after(0, lambda left=comfy_left: update_item("ComfyUI", "需要清理" if left else "已停止 ✓", C["error"] if left else C["success"]))
            self.after(0, lambda left=api_left: update_item("API 服务", "需要清理" if left else "已停止 ✓", C["error"] if left else C["success"]))
            self.after(0, lambda left=api_left: update_item("Tunnel", "需要清理" if left else "已停止 ✓", C["error"] if left else C["success"]))
            self.after(
                0,
                lambda left=remaining: update_item(
                    "后台操作",
                    "已停止 ✓" if not left else "需要清理",
                    C["success"] if not left else C["error"],
                ),
            )

            if remaining:
                details = [error for error in retry_results.values() if error]
                if not details:
                    details = [
                        f"{role}: PID {', '.join(map(str, pids))}"
                        for role, pids in remaining.items()
                    ]
                message = "；".join(details)
                self.after(0, lambda m=message: title_label.config(text="仍有服务需要强制清理", fg=C["error"]))
                self.after(0, lambda m=message: error_label.config(text=m))
                self.after(0, lambda: close_btn.config(state="normal"))
            else:
                self.after(0, finish_success)

        threading.Thread(target=_do_shutdown, daemon=True).start()

    def _kill_proc(self, proc, name: str) -> str:
        """Stop a registered service tree (used by component retries)."""
        role = "comfyui" if "comfy" in str(name).lower() else "api"
        error = self._process_supervisor.terminate(role, timeout=8)
        if role == "comfyui":
            self._comfy_proc = None
        else:
            self._api_proc = None
        return error

    def _shutdown_backend(self):
        """关闭所有后台进程（用于重启）"""
        self._poll_run = False
        errors = []
        for role in ("comfyui", "api"):
            error = self._process_supervisor.terminate(role, timeout=8)
            if error:
                errors.append(f"{role}: {error}")
        if not self._process_supervisor.is_running("comfyui"):
            self._comfy_proc = None
        if not self._process_supervisor.is_running("api"):
            self._api_proc = None
        return "；".join(errors)

    def _final_process_cleanup(self, timeout=4):
        """Non-UI, idempotent cleanup used by every final exit path."""
        self._heartbeat_run = False
        self._poll_run = False
        self._shutting_down = True
        results = {}
        try:
            results = self._process_supervisor.shutdown_all(timeout=timeout)
        except Exception as exc:
            print(f"[Shutdown] final cleanup failed: {exc}")
            results = {"supervisor": str(exc)}
        remaining = self._process_supervisor.remaining()
        for role, pids in remaining.items():
            results[role] = results.get(role) or f"PID {', '.join(map(str, pids))} 仍在运行"
        try:
            RuntimeState(BASE_DIR / "runtime").set_offline()
        except Exception as exc:
            # Runtime state does not own a process; report it without claiming
            # that background cleanup failed.
            print(f"[Shutdown] failed to persist offline state: {exc}")
        return {role: error for role, error in results.items() if error}

    @staticmethod
    def _cleanup_error_text(errors):
        return "；".join(f"{role}: {error}" for role, error in errors.items())

    def _show_shutdown_failure(self, popup, errors):
        message = self._cleanup_error_text(errors)
        print(f"[Shutdown] owned processes remain: {message}")
        try:
            if popup is not None and popup.winfo_exists():
                popup._shutdown_title.config(text="仍有后台程序未关闭", fg=C["error"])
                popup._shutdown_error.config(text=message)
                popup._shutdown_button.config(text="再次强制清理并退出", state="normal")
                return
        except Exception:
            pass
        try:
            messagebox.showerror("暂时无法退出", f"后台程序尚未完全停止：\n{message}", parent=self)
        except Exception:
            pass

    def _finish_shutdown(self, popup=None):
        errors = self._final_process_cleanup(timeout=2)
        if errors:
            self._show_shutdown_failure(popup, errors)
            return
        self._complete_destroy(popup)

    def _complete_destroy(self, popup=None):
        try:
            if hasattr(self, "_dashboard_pages"):
                self._dashboard_pages.cancel_pending()
        except Exception:
            pass
        try:
            if self._tray:
                self._tray.stop()
        except Exception:
            pass
        self._tray = None
        try:
            if popup is not None and popup.winfo_exists():
                popup.grab_release()
                popup.destroy()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _do_destroy(self):
        errors = self._final_process_cleanup(timeout=4)
        if errors:
            self._show_shutdown_failure(None, errors)
            return False
        self._complete_destroy()
        return True

    def _force_destroy(self, popup):
        """Force means close owned Jobs now; it never bypasses cleanup."""
        self._poll_run = False
        self._heartbeat_run = False

        def cleanup_and_finish():
            errors = self._final_process_cleanup(timeout=6)
            if errors:
                self.after(0, lambda e=errors: self._show_shutdown_failure(popup, e))
                return
            self._comfy_proc = None
            self._api_proc = None
            self.after(0, lambda: self._complete_destroy(popup))

        try:
            popup._shutdown_title.config(text="正在再次清理...", fg=C["warn"])
            popup._shutdown_error.config(text="")
            popup._shutdown_button.config(state="disabled")
        except Exception:
            pass

        threading.Thread(target=cleanup_and_finish, daemon=True).start()


def main():
    if not _acquire_instance_lock():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "灵镜造片厂已在运行",
            "已经打开了一个灵镜造片厂客户端。\n\n请使用已打开的窗口，避免多个客户端同时同步 URL / Key。",
            parent=root,
        )
        root.destroy()
        return
    app = None
    try:
        app = GatewayApp()
        app.mainloop()
    finally:
        if app is not None:
            app._final_process_cleanup()
        _release_instance_lock()


if __name__ == "__main__":
    main()

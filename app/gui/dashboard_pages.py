"""Product pages around the existing desktop gateway console.

The console itself remains in ``main_gateway.py``.  These pages deliberately
reuse the same visual tokens while presenting only actions that already exist
in the client.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path

from app.core.runtime_package import (
    REQUIRED_RUNTIME_PATHS,
    missing_runtime_paths,
)
from app.core.workflow_dependencies import workflow_dependency_report


BASE_DIR = Path(__file__).resolve().parents[2]


def runtime_package_status(base_dir: Path) -> tuple[str, list[str]]:
    """Return ``ready``, ``repair`` or ``missing`` for the portable runtime."""
    base_dir = Path(base_dir)
    missing = missing_runtime_paths(base_dir)
    if not missing:
        return "ready", []
    present_count = sum(1 for path in REQUIRED_RUNTIME_PATHS if (base_dir / path).is_file())
    return ("repair" if present_count else "missing"), missing


class StaticDashboardPages:
    """Build and refresh the three secondary product pages."""

    PAGE_BUILDERS = {
        "workflows": "_build_workflows",
        "resources": "_build_resources",
        "settings": "_build_settings",
    }

    def __init__(self, app, colors: dict, fonts: dict):
        self.app = app
        self.c = colors
        self.f = fonts
        self._pages: dict[str, tk.Frame] = {}
        self._last_snapshot = ""
        self._resource_targets: dict[str, tk.Widget] = {}
        self._runtime_focus_job = None

    def build(self, parent, page_id: str) -> tk.Frame:
        if page_id not in self.PAGE_BUILDERS:
            raise KeyError(f"Unknown dashboard page: {page_id}")
        page = tk.Frame(parent, bg=self.c["bg"])
        self._pages[page_id] = page
        getattr(self, self.PAGE_BUILDERS[page_id])(page)
        return page

    def refresh(self, data: dict | None = None):
        """Refresh model/workflow cards only when their source state changes."""
        data = data if isinstance(data, dict) else {}
        workflows = data.get("workflows") if isinstance(data.get("workflows"), list) else []
        snapshot = json.dumps(
            {
                "workflows": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "type": item.get("type") or item.get("output_type"),
                        "enabled": item.get("enabled", True),
                        "available": item.get("available"),
                        "missing": item.get("missing_models") or item.get("missingModels") or item.get("missing"),
                        "missing_nodes": item.get("missing_nodes") or item.get("missingNodes"),
                        "required_models": item.get("required_models"),
                        "unverified_models": item.get("unverified_models"),
                        "required_nodes": item.get("required_nodes"),
                        "is_default": item.get("is_default", False),
                        "dependency_status": item.get("dependency_status"),
                        "validation_status": item.get("validation_status"),
                        "dependencies": item.get("dependencies"),
                        "input_schema": item.get("input_schema"),
                        "workflow_json": item.get("workflow_json"),
                    }
                    for item in workflows
                    if isinstance(item, dict)
                ],
                "models": getattr(self.app, "_model_status", {}),
                "runtime": self._runtime_status(),
                "environment_check": getattr(self.app, "_environment_status", {}),
                "api_key": bool(getattr(self.app, "_api_key", "")),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        for page_id in ("workflows", "resources", "settings"):
            page = self._pages.get(page_id)
            if page is None:
                continue
            for child in page.winfo_children():
                child.destroy()
            getattr(self, self.PAGE_BUILDERS[page_id])(page)

    # ── shared pieces ──────────────────────────────────────
    def _body(self, page) -> tk.Frame:
        body = tk.Frame(page, bg=self.c["bg"])
        body.pack(fill="both", expand=True, padx=18, pady=(4, 14))
        return body

    def _card(self, parent, height: int | None = None):
        card = self.app._card(parent)
        if height:
            card.configure(height=height)
            card.pack_propagate(False)
            card.grid_propagate(False)
        return card

    def _section_heading(self, parent, title: str, subtitle: str = "", actions=None):
        row = tk.Frame(parent, bg=self.c["bg"])
        row.pack(fill="x", pady=(0, 8))
        title_box = tk.Frame(row, bg=self.c["bg"])
        title_box.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_box,
            text=title,
            font=self.f["title"],
            fg=self.c["text"],
            bg=self.c["bg"],
        ).pack(side="left")
        if subtitle:
            tk.Label(
                title_box,
                text=subtitle,
                font=self.f["small"],
                fg=self.c["muted"],
                bg=self.c["bg"],
            ).pack(side="left", padx=(12, 0), pady=(4, 0))
        if actions:
            action_box = tk.Frame(row, bg=self.c["bg"])
            action_box.pack(side="right")
            for index, (text, command, variant) in enumerate(actions):
                self.app._button(action_box, text, command, variant, width=112).pack(
                    side="left", padx=(0 if index == 0 else 8, 0)
                )
        return row

    def _badge(self, parent, text: str, tone: str = "neutral"):
        palette = {
            "primary": (self.c["soft_primary"], self.c["primary"]),
            "success": (self.c["soft_success"], self.c["success"]),
            "warn": (self.c["soft_warn"], self.c["warn"]),
            "danger": (self.c["soft_error"], self.c["error"]),
            "neutral": (self.c["hover"], self.c["text2"]),
        }
        bg, fg = palette.get(tone, palette["neutral"])
        return tk.Label(
            parent,
            text=f"  {text}  ",
            font=self.f["small"],
            fg=fg,
            bg=bg,
            padx=4,
            pady=3,
        )

    def _metric(
        self,
        parent,
        column: int,
        label: str,
        value: str,
        note: str,
        tone: str = "primary",
        height: int = 86,
    ):
        card = self._card(parent, height)
        card.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 5, 0 if column == 2 else 5),
        )
        accent = {
            "primary": self.c["primary"],
            "success": self.c["success"],
            "warn": self.c["warn"],
        }.get(tone, self.c["primary"])
        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")
        content = tk.Frame(card, bg=self.c["card"])
        content.pack(side="left", fill="both", expand=True, padx=13, pady=7)
        tk.Label(content, text=label, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(content, text=value, font=("Microsoft YaHei UI", 17, "bold"), fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(content, text=note, font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w")

    def _divider(self, parent, pady=(8, 8)):
        tk.Frame(parent, bg=self.c["border2"], height=1).pack(fill="x", pady=pady)

    def _action(self, parent, text: str, command, variant: str = "plain", width: int = 88):
        return self.app._button(parent, text, command, variant, width=width)

    def _short_path(self, path: Path, limit: int = 46) -> str:
        text = str(path)
        if len(text) <= limit:
            return text
        return f"{text[:18]}...{text[-(limit - 21):]}"

    @staticmethod
    def _short_text(value, limit: int = 24) -> str:
        text = str(value or "")
        return text if len(text) <= limit else f"{text[:max(1, limit - 1)]}…"

    # ── data helpers ───────────────────────────────────────
    def _runtime_ready(self) -> bool:
        return self._runtime_status()[0] == "ready"

    def _runtime_status(self) -> tuple[str, list[str]]:
        return runtime_package_status(BASE_DIR)

    def _set_card_outline(self, card, color: str):
        """Set a card outline without changing its size in CTk or fallback mode."""
        if hasattr(card, "_outline") and hasattr(card, "_draw_bg"):
            card._outline = color
            card._draw_bg()
            return
        card.configure(border_color=color)

    def focus_runtime_maintenance(self):
        """Briefly highlight the current runtime card after a console deep link."""
        card = self._resource_targets.get("runtime")
        try:
            if card is None or not card.winfo_exists():
                return
            if self._runtime_focus_job is not None:
                self.app.after_cancel(self._runtime_focus_job)
        except Exception:
            self._runtime_focus_job = None
            return

        self._set_card_outline(card, self.c["primary"])

        def restore(target=card):
            self._runtime_focus_job = None
            try:
                if target.winfo_exists() and self._resource_targets.get("runtime") is target:
                    self._set_card_outline(target, self.c["border2"])
            except Exception:
                pass

        self._runtime_focus_job = self.app.after(1600, restore)

    def cancel_pending(self):
        """Cancel page-owned timers before the root window is destroyed."""
        job, self._runtime_focus_job = self._runtime_focus_job, None
        if job is None:
            return
        try:
            self.app.after_cancel(job)
        except Exception:
            pass

    def _workflow_records(self) -> list[dict]:
        health = getattr(self.app, "_last_health", {})
        workflows = health.get("workflows") if isinstance(health, dict) else None
        if isinstance(workflows, list) and workflows:
            return [dict(item) for item in workflows if isinstance(item, dict)]

        records = []
        workflows_dir = BASE_DIR / "workflows"
        if not workflows_dir.exists():
            return records
        configured = {}
        default_workflow_id = ""
        config_path = BASE_DIR / "runtime" / "workflow_config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(config, dict):
                default_workflow_id = str(config.get("default_workflow_id") or "")
                configured = {
                    str(item.get("id") or ""): item
                    for item in config.get("workflows", [])
                    if isinstance(item, dict) and item.get("id")
                }
        except (OSError, json.JSONDecodeError, TypeError):
            configured = {}
        for folder in sorted(workflows_dir.iterdir()):
            manifest_path = folder / "manifest.json"
            if not folder.is_dir() or not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                records.append(
                    {
                        "id": folder.name,
                        "name": folder.name,
                        "enabled": False,
                        "output_type": "",
                        "manifest_error": True,
                    }
                )
                continue
            workflow_type = str(manifest.get("type") or "").lower()
            if workflow_type.startswith("text"):
                output_type = "text"
            elif workflow_type.startswith("video"):
                output_type = "video"
            elif workflow_type.startswith("image"):
                output_type = "image"
            else:
                output_type = str(manifest.get("output_type") or "")
            records.append(
                {
                    **manifest,
                    **configured.get(str(manifest.get("id") or folder.name), {}),
                    "id": str(manifest.get("id") or folder.name),
                    "name": str(manifest.get("name") or folder.name),
                    "enabled": configured.get(str(manifest.get("id") or folder.name), {}).get(
                        "enabled", manifest.get("enabled", True)
                    ),
                    "output_type": output_type,
                    "workflow_json": f"{folder.name}/workflow.json" if (folder / "workflow.json").exists() else "",
                    "is_default": str(manifest.get("id") or folder.name) == default_workflow_id,
                }
            )
        return records

    def _workflow_type_label(self, workflow: dict) -> tuple[str, str, str]:
        text = str(workflow.get("output_type") or workflow.get("type") or "").lower()
        if "video" in text:
            return "影", "视频", "warn"
        if "text" in text or "chat" in text:
            return "文", "文字", "primary"
        return "图", "图片", "success"

    def _workflow_state(self, workflow: dict) -> tuple[str, str, str, str]:
        if workflow.get("manifest_error") or not workflow.get("workflow_json", True):
            return "文件异常", "danger", "工作流文件不完整", ""
        if not workflow.get("enabled", True):
            return "已停用", "neutral", "需要启用后才能调用", ""

        model_key = ""
        try:
            model_key = str(self.app._workflow_model_key(workflow) or "")
        except Exception:
            pass
        missing = []
        missing_nodes = []
        model_status = getattr(self.app, "_model_status", {})
        if model_key:
            missing = list((model_status.get("missing") or {}).get(model_key) or [])
        for key in ("missing_models", "missingModels", "missing"):
            value = workflow.get(key)
            if isinstance(value, (list, tuple)):
                missing.extend(str(item) for item in value if item)
        for key in ("missing_nodes", "missingNodes"):
            value = workflow.get(key)
            if isinstance(value, (list, tuple)):
                missing_nodes.extend(str(item) for item in value if item)

        dependency_status = str(workflow.get("dependency_status") or "").lower()
        dependencies = workflow.get("dependencies")
        if isinstance(dependencies, dict):
            report = workflow_dependency_report(dependencies, BASE_DIR / "models")
            if not missing:
                missing.extend(report["missing_models"])
            if not dependency_status:
                dependency_status = report["dependency_status"]
        missing = list(dict.fromkeys(missing))
        if missing:
            detail = "、".join(missing[:2])
            if len(missing) > 2:
                detail += f" 等 {len(missing)} 个文件"
            return f"缺少 {len(missing)} 个模型", "warn", detail, model_key

        missing_nodes = list(dict.fromkeys(missing_nodes))
        if missing_nodes:
            detail = "、".join(missing_nodes[:2])
            if len(missing_nodes) > 2:
                detail += f" 等 {len(missing_nodes)} 个节点"
            return f"缺少 {len(missing_nodes)} 个节点", "danger", detail, ""

        if dependency_status in {"unknown", "unchecked", "unverified", ""}:
            return "依赖待确认", "warn", "模型已扫描；节点将在 ComfyUI 启动后核对", ""

        try:
            available = bool(self.app._workflow_model_available(workflow))
        except Exception:
            available = True
        if not available:
            return "需要检查", "warn", "模型或依赖尚未准备完成", model_key
        return "可以使用", "success", "输入和输出已经配置完成", model_key

    # ── workflows ──────────────────────────────────────────
    def _build_workflows(self, page):
        body = self._body(page)
        workflows = self._workflow_records()
        states = [self._workflow_state(item) for item in workflows]
        ready_count = sum(1 for state, *_ in states if state == "可以使用")
        issue_count = sum(
            1 for state, *_ in states if state not in {"可以使用", "已停用"}
        )

        metrics = tk.Frame(body, bg=self.c["bg"])
        metrics.pack(fill="x", pady=(0, 14))
        for col in range(3):
            metrics.columnconfigure(col, weight=1, uniform="workflow_metrics")
        self._metric(metrics, 0, "工作流", f"{len(workflows)} 个", "文字、图片与视频能力")
        self._metric(metrics, 1, "可以使用", f"{ready_count} 个", "可直接通过 URL + Key 调用", "success")
        self._metric(metrics, 2, "需要处理", f"{issue_count} 个", "缺少模型、节点或配置", "warn")

        self._section_heading(
            body,
            "我的工作流",
            "已识别的模型状态会自动更新",
            actions=[
                ("添加教程", self.app._show_workflow_tutorial, "plain"),
                ("＋ 添加工作流", self.app._show_workflow_upload_dialog, "primary"),
            ],
        )
        listing_height = max(132, min(244, 24 + len(workflows) * 66))
        listing = self._card(body, listing_height)
        listing.pack(fill="x", pady=(0, 14))

        rows_parent = listing
        scroll_canvas = None
        if len(workflows) > 3:
            scrollbar = tk.Scrollbar(listing, orient="vertical")
            scrollbar.pack(side="right", fill="y", padx=(0, 3), pady=5)
            scroll_canvas = tk.Canvas(
                listing,
                bg=self.c["card"],
                highlightthickness=0,
                bd=0,
                yscrollcommand=scrollbar.set,
            )
            scroll_canvas.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=3)
            scrollbar.configure(command=scroll_canvas.yview)
            rows_parent = tk.Frame(scroll_canvas, bg=self.c["card"])
            rows_window = scroll_canvas.create_window((0, 0), window=rows_parent, anchor="nw")
            rows_parent.bind(
                "<Configure>",
                lambda _event, canvas=scroll_canvas: canvas.configure(
                    scrollregion=canvas.bbox("all")
                ),
            )
            scroll_canvas.bind(
                "<Configure>",
                lambda event, canvas=scroll_canvas, window=rows_window: canvas.itemconfigure(
                    window, width=event.width
                ),
            )

        if not workflows:
            tk.Label(listing, text="还没有工作流", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(pady=(28, 4))
            tk.Label(listing, text="点击右上角“添加工作流”即可开始。", font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack()
        else:
            for index, workflow in enumerate(workflows):
                glyph, kind, type_tone = self._workflow_type_label(workflow)
                state, state_tone, detail, model_key = self._workflow_state(workflow)
                tone_fg = {
                    "primary": self.c["primary"],
                    "success": self.c["success"],
                    "warn": self.c["warn"],
                }[type_tone]
                tone_bg = {
                    "primary": self.c["soft_primary"],
                    "success": self.c["soft_success"],
                    "warn": self.c["soft_warn"],
                }[type_tone]
                row = tk.Frame(rows_parent, bg=self.c["card"])
                row.pack(fill="x", padx=14, pady=(9 if index == 0 else 6, 6))
                tk.Label(row, text=glyph, font=self.f["bold"], fg=tone_fg, bg=tone_bg, width=3, pady=6).pack(side="left")
                name_box = tk.Frame(row, bg=self.c["card"], width=180, height=44)
                name_box.pack(side="left", padx=(11, 0), fill="x", expand=True)
                name_box.pack_propagate(False)
                name_line = tk.Frame(name_box, bg=self.c["card"])
                name_line.pack(fill="x", anchor="w")
                display_name = self._short_text(
                    workflow.get("name") or workflow.get("id") or "未命名工作流",
                    18,
                )
                tk.Label(name_line, text=display_name, font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
                if workflow.get("is_default"):
                    self._badge(name_line, "默认", "primary").pack(side="left", padx=(7, 0))
                tk.Label(name_box, text=self._short_text(workflow.get("id"), 24), font=self.f["mono"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w")
                tk.Label(row, text=kind, width=6, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left")
                detail_box = tk.Frame(row, bg=self.c["card"], width=174, height=44)
                detail_box.pack(side="left", padx=(4, 8))
                detail_box.pack_propagate(False)
                self._badge(detail_box, state, state_tone).pack(anchor="w")
                tk.Label(detail_box, text=self._short_text(detail, 24), font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"], anchor="w").pack(anchor="w", pady=(2, 0))

                if state_tone == "warn" and model_key:
                    self._action(
                        row,
                        "修复",
                        lambda key=model_key: self.app._show_model_install_help(key),
                        "primary",
                        58,
                    ).pack(side="right", padx=(6, 0))
                self._action(
                    row,
                    "查看参数",
                    lambda item=dict(workflow): self.app._show_workflow_schema(item),
                    "plain",
                    78,
                ).pack(side="right")
                workflow_id = str(workflow.get("id") or "")
                enabled = bool(workflow.get("enabled", True))
                self._action(
                    row,
                    "停用" if enabled else "启用",
                    lambda wf_id=workflow_id, next_enabled=not enabled: self.app._set_workflow_enabled(
                        wf_id, next_enabled
                    ),
                    "plain" if enabled else "primary",
                    56,
                ).pack(side="right", padx=(6, 0))
                can_be_default = (
                    enabled
                    and state not in {"文件异常", "需要检查"}
                    and not state.startswith("缺少")
                )
                if can_be_default and not workflow.get("is_default"):
                    self._action(
                        row,
                        "设为默认",
                        lambda wf_id=workflow_id: self.app._set_default_workflow(wf_id),
                        "plain",
                        68,
                    ).pack(side="right", padx=(6, 0))
                if index < len(workflows) - 1:
                    tk.Frame(rows_parent, bg=self.c["border2"], height=1).pack(fill="x", padx=14)

            if scroll_canvas is not None:
                def scroll(event, canvas=scroll_canvas):
                    canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

                def bind_wheel(widget):
                    widget.bind("<MouseWheel>", scroll, add="+")
                    for child in widget.winfo_children():
                        bind_wheel(child)

                bind_wheel(rows_parent)

        guide = self._card(body, 96)
        guide.pack(fill="x")
        left = tk.Frame(guide, bg=self.c["card"])
        left.pack(side="left", fill="both", expand=True, padx=16, pady=13)
        tk.Label(left, text="第一次添加工作流？", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(
            left,
            text="导出 ComfyUI API 工作流 → 选择文件或文件夹 → 确认输入与输出；未声明的依赖会提示补充。",
            font=self.f["small"],
            fg=self.c["text2"],
            bg=self.c["card"],
        ).pack(anchor="w", pady=(5, 0))
        self._action(guide, "查看完整教程", self.app._show_workflow_tutorial, "plain", 112).pack(side="right", padx=16)

    # ── models and environment ─────────────────────────────
    def _build_resources(self, page):
        body = self._body(page)
        runtime_state, runtime_missing = self._runtime_status()
        runtime_ok = runtime_state == "ready"
        environment_check = getattr(self.app, "_environment_status", {})
        model_status = getattr(self.app, "_model_status", {})
        model_keys = ("Qwen3.5", "Flux2", "Wan2.1")
        ready_models = sum(1 for key in model_keys if model_status.get(key) == "完整")
        missing_count = sum(len((model_status.get("missing") or {}).get(key) or []) for key in model_keys)

        metrics = tk.Frame(body, bg=self.c["bg"])
        metrics.pack(fill="x", pady=(0, 14))
        for col in range(3):
            metrics.columnconfigure(col, weight=1, uniform="resource_metrics")
        runtime_metric = {
            "ready": ("已就绪", "环境文件完整", "success"),
            "repair": ("需要修复", f"缺少 {len(runtime_missing)} 项", "warn"),
            "missing": ("需要安装", "首次使用先安装", "warn"),
        }[runtime_state]
        if runtime_state == "ready" and environment_check and not environment_check.get("ready"):
            runtime_metric = ("需要检查", "文件完整，运行检查未通过", "warn")
        self._metric(metrics, 0, "运行环境", *runtime_metric)
        self._metric(metrics, 1, "模型组", f"{ready_models}/{len(model_keys)}", "按工作流自动检查", "success" if ready_models == len(model_keys) else "warn")
        self._metric(metrics, 2, "缺少文件", f"{missing_count} 个", "只下载实际需要的内容", "warn" if missing_count else "success")

        self._section_heading(body, "运行环境维护", "安装一次，之后由客户端自动启动")
        runtime_card = self._card(body, 78)
        self._resource_targets["runtime"] = runtime_card
        runtime_card.pack(fill="x", pady=(0, 14))
        runtime_left = tk.Frame(runtime_card, bg=self.c["card"])
        runtime_left.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        title_row = tk.Frame(runtime_left, bg=self.c["card"])
        title_row.pack(fill="x")
        tk.Label(title_row, text="本地生成环境", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
        runtime_check_failed = runtime_ok and environment_check and not environment_check.get("ready")
        badge_text = {"ready": "环境完整", "repair": "需要修复", "missing": "尚未安装"}[runtime_state]
        if runtime_check_failed:
            badge_text = "运行需处理"
        self._badge(title_row, badge_text, "warn" if runtime_check_failed or not runtime_ok else "success").pack(side="left", padx=(10, 0))
        if runtime_state == "ready" and environment_check:
            if environment_check.get("ready"):
                gpu_name = str(environment_check.get("gpu_name") or "显卡环境正常")
                runtime_note = f"检查通过 · {gpu_name}"
            else:
                runtime_note = str(environment_check.get("message") or "环境文件完整，运行检查需要处理。")
        elif runtime_state == "ready":
            runtime_note = "环境文件完整；可检查显卡与 PyTorch，或执行修复更新。"
        elif runtime_state == "repair":
            short_missing = "、".join(Path(item).name for item in runtime_missing[:2])
            runtime_note = f"环境不完整，缺少 {len(runtime_missing)} 项：{short_missing}{'…' if len(runtime_missing) > 2 else ''}"
        else:
            runtime_note = "首次使用先安装运行环境；模型文件继续单独管理，不会重复下载。"
        tk.Label(runtime_left, text=runtime_note, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", pady=(6, 0))

        runtime_actions = tk.Frame(runtime_card, bg=self.c["card"])
        runtime_actions.pack(side="right", padx=16)
        if runtime_ok:
            self._action(runtime_actions, "检查环境", self.app._start_background_runtime_recheck, "primary", 88).pack(side="left")
            self._action(runtime_actions, "修复 / 更新", self.app._show_runtime_maintenance, "plain", 94).pack(side="left", padx=(8, 0))
            self._action(runtime_actions, "打开目录", self.app._open_runtime_dir, "plain", 82).pack(side="left", padx=(8, 0))
        else:
            self._action(runtime_actions, "一键安装", self.app._install_runtime_from_mirror, "primary", 88).pack(side="left")
            self._action(runtime_actions, "本地安装包", self.app._select_runtime, "plain", 94).pack(side="left", padx=(8, 0))
            self._action(runtime_actions, "更多方式", self.app._show_runtime_maintenance, "plain", 82).pack(side="left", padx=(8, 0))

        self._section_heading(
            body,
            "模型维护",
            "模型与环境分开管理，缺什么就补什么",
            actions=[
                ("导入已有模型", self.app._import_models, "plain"),
                ("重新检查", lambda: self.app._start_background_model_recheck(), "primary"),
            ],
        )
        models_card = self._card(body, 198)
        models_card.pack(fill="x", pady=(0, 14))
        labels = {
            "Qwen3.5": ("Qwen 3.5 文字生成", "文字", "文", "primary"),
            "Flux2": ("Flux 2 图片生成", "图片", "图", "success"),
            "Wan2.1": ("Wan 2.1 视频生成", "视频", "影", "warn"),
        }
        for index, key in enumerate(model_keys):
            title, kind, glyph, tone = labels[key]
            missing = list((model_status.get("missing") or {}).get(key) or [])
            ready = model_status.get(key) == "完整"
            row = tk.Frame(models_card, bg=self.c["card"])
            row.pack(fill="x", padx=14, pady=(9 if index == 0 else 7, 7))
            icon_bg = {
                "primary": self.c["soft_primary"],
                "success": self.c["soft_success"],
                "warn": self.c["soft_warn"],
            }[tone]
            icon_fg = {
                "primary": self.c["primary"],
                "success": self.c["success"],
                "warn": self.c["warn"],
            }[tone]
            tk.Label(row, text=glyph, font=self.f["bold"], fg=icon_fg, bg=icon_bg, width=3, pady=6).pack(side="left")
            name_box = tk.Frame(row, bg=self.c["card"])
            name_box.pack(side="left", fill="x", expand=True, padx=(11, 0))
            tk.Label(name_box, text=title, font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
            note = "模型完整，可以直接使用" if ready else f"缺少：{'、'.join(missing[:2])}{'…' if len(missing) > 2 else ''}"
            tk.Label(name_box, text=note, font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w")
            tk.Label(row, text=kind, width=7, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left")
            self._badge(row, "完整" if ready else f"缺少 {len(missing)} 个", "success" if ready else "warn").pack(side="left", padx=(6, 10))
            if not ready:
                self._action(row, "下载缺失", lambda item=key: self.app._show_model_install_help(item), "primary", 88).pack(side="right")
            if index < len(model_keys) - 1:
                tk.Frame(models_card, bg=self.c["border2"], height=1).pack(fill="x", padx=14)

        storage = self._card(body, 74)
        storage.pack(fill="x")
        paths = [
            ("模型", BASE_DIR / "models", self.app._open_models),
            ("工作流", BASE_DIR / "workflows", self.app._open_workflows_dir),
            ("生成结果", BASE_DIR / "outputs", self.app._open_outputs),
        ]
        for index, (label, path, command) in enumerate(paths):
            cell = tk.Frame(storage, bg=self.c["card"])
            cell.pack(side="left", fill="both", expand=True, padx=(16 if index == 0 else 8, 8), pady=8)
            cell_head = tk.Frame(cell, bg=self.c["card"])
            cell_head.pack(fill="x")
            tk.Label(cell_head, text=f"{label}位置", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            self._action(cell_head, "打开", command, "plain", 66).pack(side="right")
            tk.Label(cell, text=self._short_path(path, 30), font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w", pady=(3, 0))

    # ── settings ───────────────────────────────────────────
    def _build_settings(self, page):
        body = self._body(page)
        self._section_heading(body, "访问与安全", "用于其他软件调用本客户端")
        access = self._card(body, 94)
        access.pack(fill="x", pady=(0, 14))
        access_left = tk.Frame(access, bg=self.c["card"])
        access_left.pack(side="left", fill="both", expand=True, padx=16, pady=13)
        tk.Label(access_left, text="访问密钥", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        key = str(getattr(self.app, "_api_key", "") or "")
        masked = f"{key[:12]}{'•' * 12}{key[-6:]}" if len(key) > 20 else "服务启动后自动生成"
        self.app._settings_key_label = tk.Label(access_left, text=masked, font=self.f["mono"], fg=self.c["primary"], bg=self.c["card"])
        self.app._settings_key_label.pack(anchor="w", pady=(5, 0))
        tk.Label(access_left, text="密钥只保存在本机；请不要发送给不信任的人。", font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w", pady=(3, 0))
        access_actions = tk.Frame(access, bg=self.c["card"])
        access_actions.pack(side="right", padx=16)
        self._action(access_actions, "修改密钥", self.app._edit_api_key, "plain", 84).pack(side="left", padx=(0, 8))
        self._action(access_actions, "复制密钥", self.app._copy_api_key, "primary", 84).pack(side="left")

        self._section_heading(body, "文件存放位置", "当前随项目保存，点击即可打开查看")
        paths_card = self._card(body, 188)
        paths_card.pack(fill="x", pady=(0, 14))
        path_rows = [
            ("模型", BASE_DIR / "models", self.app._open_models),
            ("工作流", BASE_DIR / "workflows", self.app._open_workflows_dir),
            ("生成结果", BASE_DIR / "outputs", self.app._open_outputs),
            ("运行日志", BASE_DIR / "runtime" / "logs", self.app._open_logs_dir),
        ]
        for index, (label, path, command) in enumerate(path_rows):
            row = tk.Frame(paths_card, bg=self.c["card"])
            row.pack(fill="x", padx=16, pady=(9 if index == 0 else 5, 5))
            tk.Label(row, text=label, width=10, anchor="w", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=self._short_path(path, 68), anchor="w", font=self.f["mono"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left", fill="x", expand=True)
            self._action(row, "打开", command, "plain", 66).pack(side="right")
            if index < len(path_rows) - 1:
                tk.Frame(paths_card, bg=self.c["border2"], height=1).pack(fill="x", padx=16)

        lower = tk.Frame(body, bg=self.c["bg"])
        lower.pack(fill="x")
        lower.columnconfigure(0, weight=1, uniform="settings_lower")
        lower.columnconfigure(1, weight=1, uniform="settings_lower")

        behavior = self._card(lower, 168)
        behavior.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(behavior, text="运行方式", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w", padx=16, pady=(13, 7))
        behavior_rows = [
            ("关闭主窗口", "停止全部后台服务"),
            ("最小化窗口", "继续后台运行"),
            ("任务并发", "1 个任务"),
            ("单实例运行", "已开启"),
        ]
        for label, value in behavior_rows:
            row = tk.Frame(behavior, bg=self.c["card"])
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=label, font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=value, font=self.f["small"], fg=self.c["text"], bg=self.c["card"]).pack(side="right")

        advanced = self._card(lower, 168)
        advanced.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        head = tk.Frame(advanced, bg=self.c["card"])
        head.pack(fill="x", padx=16, pady=(13, 7))
        tk.Label(head, text="专业配置", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
        self._badge(head, "高级", "primary").pack(side="right")
        advanced_rows = [
            ("API", "端口 18188"),
            ("ComfyUI", "端口 8188"),
            ("Tunnel", "自动建立公网连接"),
        ]
        for label, value in advanced_rows:
            row = tk.Frame(advanced, bg=self.c["card"])
            row.pack(fill="x", padx=16, pady=3)
            tk.Label(row, text=label, font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=value, font=self.f["small"], fg=self.c["text"], bg=self.c["card"]).pack(side="right")
        self._action(advanced, "打开配置文件", self.app._open_runtime_config, "plain", 106).pack(anchor="e", padx=16, pady=(7, 0))

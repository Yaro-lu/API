"""Static product pages for the desktop gateway shell.

The first interface pass intentionally keeps these pages presentation-only.
Business actions remain in ``main_gateway.py`` until their contracts are fixed.
"""

from __future__ import annotations

import tkinter as tk


class StaticDashboardPages:
    """Build the non-overview pages while sharing the gateway's visual tokens."""

    def __init__(self, app, colors: dict, fonts: dict):
        self.app = app
        self.c = colors
        self.f = fonts

    def build(self, parent, page_id: str) -> tk.Frame:
        page = tk.Frame(parent, bg=self.c["bg"])
        builders = {
            "services": self._build_services,
            "workflows": self._build_workflows,
            "tasks": self._build_tasks,
            "network": self._build_network,
            "resources": self._build_resources,
            "settings": self._build_settings,
        }
        builders[page_id](page)
        return page

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

    def _section_heading(self, parent, title: str, subtitle: str = ""):
        row = tk.Frame(parent, bg=self.c["bg"])
        row.pack(fill="x", pady=(0, 8))
        tk.Label(
            row,
            text=title,
            font=self.f["title"],
            fg=self.c["text"],
            bg=self.c["bg"],
        ).pack(side="left")
        if subtitle:
            tk.Label(
                row,
                text=subtitle,
                font=self.f["small"],
                fg=self.c["muted"],
                bg=self.c["bg"],
            ).pack(side="right", pady=(3, 0))

    def _badge(self, parent, text: str, tone: str = "neutral"):
        palette = {
            "primary": (self.c["soft_primary"], self.c["primary"]),
            "success": (self.c["soft_success"], self.c["success"]),
            "warn": (self.c["soft_warn"], self.c["warn"]),
            "danger": (self.c["soft_error"], self.c["error"]),
            "neutral": (self.c["hover"], self.c["text2"]),
        }
        bg, fg = palette.get(tone, palette["neutral"])
        label = tk.Label(
            parent,
            text=f"  {text}  ",
            font=self.f["small"],
            fg=fg,
            bg=bg,
            padx=4,
            pady=3,
        )
        return label

    def _metric(self, parent, column: int, label: str, value: str, note: str, tone: str = "primary"):
        card = self._card(parent, 92)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 2 else 5))
        accent = {
            "primary": self.c["primary"],
            "success": self.c["success"],
            "warn": self.c["warn"],
        }.get(tone, self.c["primary"])
        strip = tk.Frame(card, bg=accent, width=4)
        strip.pack(side="left", fill="y")
        content = tk.Frame(card, bg=self.c["card"])
        content.pack(side="left", fill="both", expand=True, padx=13, pady=7)
        tk.Label(content, text=label, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(content, text=value, font=("Microsoft YaHei UI", 17, "bold"), fg=self.c["text"], bg=self.c["card"]).pack(anchor="w", pady=(1, 0))
        tk.Label(content, text=note, font=self.f["tiny"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w")

    def _disabled_action(self, parent, text: str, width: int = 12):
        return tk.Label(
            parent,
            text=text,
            font=self.f["small"],
            fg=self.c["muted"],
            bg=self.c["hover"],
            width=width,
            padx=7,
            pady=5,
        )

    def _divider(self, parent, pady=(8, 8)):
        tk.Frame(parent, bg=self.c["border2"], height=1).pack(fill="x", pady=pady)

    def _key_value(self, parent, label: str, value: str, mono: bool = False, value_color: str | None = None):
        row = tk.Frame(parent, bg=self.c["card"])
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(side="left")
        tk.Label(
            row,
            text=value,
            font=self.f["mono"] if mono else self.f["small"],
            fg=value_color or self.c["text"],
            bg=self.c["card"],
        ).pack(side="right")

    # ── interface services ─────────────────────────────────
    def _build_services(self, page):
        body = self._body(page)

        metrics = tk.Frame(body, bg=self.c["bg"])
        metrics.pack(fill="x", pady=(0, 14))
        for col in range(3):
            metrics.columnconfigure(col, weight=1, uniform="service_metrics")
        self._metric(metrics, 0, "兼容协议", "3 类", "文字 · 图片 · 视频")
        self._metric(metrics, 1, "调用方式", "异步", "统一返回 task_id", "success")
        self._metric(metrics, 2, "访问鉴权", "URL + Key", "调用方无需安装组件", "warn")

        self._section_heading(body, "对外兼容服务", "本阶段仅展示接口结构，尚未接入配置动作")
        services = tk.Frame(body, bg=self.c["bg"])
        services.pack(fill="x", pady=(0, 14))
        for col in range(3):
            services.columnconfigure(col, weight=1, uniform="service_cards")

        cards = [
            ("文", "文字生成", "DeepSeek / OpenAI", "POST  /v1/chat/completions", "local-text-default", "primary"),
            ("图", "图片生成", "即梦 / 火山兼容", "POST  /v1/images/generations", "local-image-default", "success"),
            ("影", "视频生成", "Seedance 2.0 兼容", "POST  /v1/videos/generations", "local-video-default", "warn"),
        ]
        for col, (glyph, title, protocol, path, model, tone) in enumerate(cards):
            card = self._card(services, 172)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 2 else 5))
            top = tk.Frame(card, bg=self.c["card"])
            top.pack(fill="x", padx=14, pady=(13, 7))
            icon_bg = {"primary": self.c["soft_primary"], "success": self.c["soft_success"], "warn": self.c["soft_warn"]}[tone]
            icon_fg = {"primary": self.c["primary"], "success": self.c["success"], "warn": self.c["warn"]}[tone]
            tk.Label(top, text=glyph, font=self.f["bold"], fg=icon_fg, bg=icon_bg, width=3, height=1, pady=6).pack(side="left")
            title_box = tk.Frame(top, bg=self.c["card"])
            title_box.pack(side="left", padx=(10, 0))
            tk.Label(title_box, text=title, font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
            tk.Label(title_box, text=protocol, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w")
            self._badge(top, "待接入", "neutral").pack(side="right")
            self._divider(card, (2, 7))
            tk.Label(card, text=path, font=self.f["mono"], fg=self.c["primary"], bg=self.c["card"]).pack(anchor="w", padx=14)
            tk.Label(card, text=f"默认模型  {model}", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", padx=14, pady=(7, 0))

        self._section_heading(body, "模型路由", "URL 或请求体指定英文模型名；未指定时使用单一默认模型")
        route = self._card(body, 128)
        route.pack(fill="x")
        header = tk.Frame(route, bg=self.c["hover"])
        header.pack(fill="x", padx=1, pady=(1, 0))
        for text, width, anchor in (("类型", 10, "w"), ("公开模型标识", 28, "w"), ("执行目标", 22, "w"), ("状态", 12, "e")):
            tk.Label(header, text=text, width=width, anchor=anchor, font=self.f["small"], fg=self.c["text2"], bg=self.c["hover"], padx=10, pady=6).pack(side="left", fill="x", expand=anchor == "w")
        rows = [
            ("文字", "local-text-default", "文字工作流 / 第三方适配器"),
            ("图片", "local-image-default", "图片工作流 / 第三方适配器"),
            ("视频", "local-video-default", "视频工作流 / 第三方适配器"),
        ]
        for kind, model, target in rows:
            row = tk.Frame(route, bg=self.c["card"])
            row.pack(fill="x", padx=10, pady=4)
            tk.Label(row, text=kind, width=9, anchor="w", font=self.f["small"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=model, width=27, anchor="w", font=self.f["mono"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=target, anchor="w", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left", fill="x", expand=True)
            self._badge(row, "规划中").pack(side="right")

    # ── workflows ─────────────────────────────────────────
    def _build_workflows(self, page):
        body = self._body(page)
        metrics = tk.Frame(body, bg=self.c["bg"])
        metrics.pack(fill="x", pady=(0, 14))
        for col in range(3):
            metrics.columnconfigure(col, weight=1, uniform="workflow_metrics")
        self._metric(metrics, 0, "工作流包", "3 个", "当前项目已发现", "primary")
        self._metric(metrics, 1, "输出类型", "3 类", "文字 · 图片 · 视频", "success")
        self._metric(metrics, 2, "安装方式", "独立包", "与环境和模型分离", "warn")

        self._section_heading(body, "已发现的工作流", "英文标识将用于接口模型路由")
        listing = self._card(body, 224)
        listing.pack(fill="x", pady=(0, 14))
        workflows = [
            ("文", "Qwen3 文字生成", "llm_qwen3_text_gen", "文字", "本地 LLM", "已发现", "primary"),
            ("图", "Flux 2 文生图", "flux_t2i_v1", "图片", "Flux2 模型组", "已发现", "success"),
            ("影", "Wan 2.1 首尾帧视频", "wan_flf2v_v1", "视频", "Wan2.1 模型组", "已发现", "warn"),
        ]
        for index, (glyph, title, wf_id, output, dependency, status, tone) in enumerate(workflows):
            row = tk.Frame(listing, bg=self.c["card"])
            row.pack(fill="x", padx=14, pady=(11 if index == 0 else 7, 7))
            tone_fg = {"primary": self.c["primary"], "success": self.c["success"], "warn": self.c["warn"]}[tone]
            tone_bg = {"primary": self.c["soft_primary"], "success": self.c["soft_success"], "warn": self.c["soft_warn"]}[tone]
            tk.Label(row, text=glyph, font=self.f["bold"], fg=tone_fg, bg=tone_bg, width=3, pady=6).pack(side="left")
            name_box = tk.Frame(row, bg=self.c["card"])
            name_box.pack(side="left", padx=(11, 0), fill="x", expand=True)
            tk.Label(name_box, text=title, font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
            tk.Label(name_box, text=wf_id, font=self.f["mono"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w")
            tk.Label(row, text=output, width=8, font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=dependency, width=18, anchor="w", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left")
            self._badge(row, status, "success").pack(side="left", padx=(4, 8))
            self._disabled_action(row, "查看参数").pack(side="right")
            if index < len(workflows) - 1:
                tk.Frame(listing, bg=self.c["border2"], height=1).pack(fill="x", padx=14)

        self._section_heading(body, "导入新工作流", "后续支持 ComfyUI API JSON、工作流目录和带 manifest 的工作流包")
        import_card = self._card(body, 112)
        import_card.pack(fill="x")
        left = tk.Frame(import_card, bg=self.c["card"])
        left.pack(side="left", fill="both", expand=True, padx=16, pady=15)
        tk.Label(left, text="＋  添加任意工作流", font=self.f["h2"], fg=self.c["primary"], bg=self.c["card"]).pack(anchor="w")
        tk.Label(left, text="导入后声明输入结构、输出类型、模型依赖、自定义节点和版本。", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", pady=(5, 0))
        self._disabled_action(import_card, "功能接入后可用", 18).pack(side="right", padx=16)

    # ── tasks ─────────────────────────────────────────────
    def _build_tasks(self, page):
        body = self._body(page)
        metrics = tk.Frame(body, bg=self.c["bg"])
        metrics.pack(fill="x", pady=(0, 14))
        for col in range(3):
            metrics.columnconfigure(col, weight=1, uniform="task_metrics")
        self._metric(metrics, 0, "等待队列", "0", "尚无排队任务", "primary")
        self._metric(metrics, 1, "正在运行", "0", "执行器当前空闲", "success")
        self._metric(metrics, 2, "需要处理", "0", "失败或中断任务", "warn")

        self._section_heading(body, "任务记录", "所有文字、图片和视频调用都将使用统一异步任务")
        panel = self._card(body, 224)
        panel.pack(fill="x", pady=(0, 14))
        filters = tk.Frame(panel, bg=self.c["card"])
        filters.pack(fill="x", padx=14, pady=(12, 8))
        for index, label in enumerate(("全部", "排队中", "运行中", "已完成", "失败", "已中断")):
            tone = "primary" if index == 0 else "neutral"
            self._badge(filters, label, tone).pack(side="left", padx=(0, 7))
        self._divider(panel, (0, 0))
        empty = tk.Frame(panel, bg=self.c["card"])
        empty.pack(fill="both", expand=True)
        tk.Label(empty, text="◷", font=("Microsoft YaHei UI", 28), fg=self.c["border"], bg=self.c["card"]).pack(pady=(30, 4))
        tk.Label(empty, text="暂无任务记录", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack()
        tk.Label(empty, text="接口接入后，这里会展示 task_id、来源、进度、耗时和结果。", font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(pady=(5, 0))

        self._section_heading(body, "重启恢复规则", "本地执行器与第三方任务采用不同恢复策略")
        recovery = tk.Frame(body, bg=self.c["bg"])
        recovery.pack(fill="x")
        for col in range(2):
            recovery.columnconfigure(col, weight=1, uniform="recovery_cards")
        items = [
            ("本地工作流", "客户端退出会同时结束 ComfyUI。运行中的任务标记为“已中断”，可选择从头重试。", "warn"),
            ("第三方 API", "已取得供应商 task_id 的任务会持久化，客户端重启后继续查询状态和结果。", "success"),
        ]
        for col, (title, desc, tone) in enumerate(items):
            card = self._card(recovery, 112)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 1 else 5))
            head = tk.Frame(card, bg=self.c["card"])
            head.pack(fill="x", padx=14, pady=(13, 6))
            tk.Label(head, text=title, font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            self._badge(head, "规则已确定", tone).pack(side="right")
            tk.Label(card, text=desc, wraplength=390, justify="left", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", padx=14)

    # ── network ───────────────────────────────────────────
    def _build_network(self, page):
        body = self._body(page)
        self._section_heading(body, "访问模式", "同一套 URL + Key 调用方式，适配本机、局域网与公网环境")
        modes = tk.Frame(body, bg=self.c["bg"])
        modes.pack(fill="x", pady=(0, 14))
        for col in range(3):
            modes.columnconfigure(col, weight=1, uniform="network_modes")
        data = [
            ("本机访问", "127.0.0.1", "当前可用", "仅当前电脑", "success"),
            ("局域网访问", "LAN IP", "待配置", "同一内网设备", "primary"),
            ("公网访问", "Tunnel / 域名", "临时通道", "任何联网设备", "warn"),
        ]
        for col, (title, address, status, note, tone) in enumerate(data):
            card = self._card(modes, 132)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 2 else 5))
            head = tk.Frame(card, bg=self.c["card"])
            head.pack(fill="x", padx=14, pady=(13, 5))
            tk.Label(head, text=title, font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            self._badge(head, status, tone).pack(side="right")
            tk.Label(card, text=address, font=("Consolas", 15, "bold"), fg=self.c["primary"], bg=self.c["card"]).pack(anchor="w", padx=14, pady=(6, 1))
            tk.Label(card, text=note, font=self.f["small"], fg=self.c["muted"], bg=self.c["card"]).pack(anchor="w", padx=14)

        self._section_heading(body, "连接信息", "真实公网地址和访问 Key 仍以概览页运行状态为准")
        connection = self._card(body, 150)
        connection.pack(fill="x", pady=(0, 14))
        grid = tk.Frame(connection, bg=self.c["card"])
        grid.pack(fill="both", expand=True, padx=16, pady=12)
        left = tk.Frame(grid, bg=self.c["card"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 24))
        right = tk.Frame(grid, bg=self.c["card"])
        right.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="本地端点", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        self._key_value(left, "API Base", "http://127.0.0.1:18188", True)
        self._key_value(left, "ComfyUI", "http://127.0.0.1:8188", True)
        self._key_value(left, "鉴权", "Bearer API Key")
        tk.Label(right, text="公网策略", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
        self._key_value(right, "开发测试", "Cloudflare Quick Tunnel")
        self._key_value(right, "正式部署", "Named Tunnel / 自有反代")
        self._key_value(right, "大文件", "流式 + Range + 断点续传")

        self._section_heading(body, "上线前检查")
        checklist = self._card(body, 100)
        checklist.pack(fill="x")
        items = [
            ("Key 鉴权", "已规划", "primary"),
            ("访问限流", "待接入", "neutral"),
            ("HTTPS / 固定域名", "待配置", "warn"),
            ("大视频断点续传", "待实现", "neutral"),
        ]
        for index, (label, status, tone) in enumerate(items):
            item = tk.Frame(checklist, bg=self.c["card"])
            item.pack(side="left", fill="both", expand=True, padx=(14 if index == 0 else 7, 14 if index == len(items) - 1 else 7), pady=18)
            tk.Label(item, text=label, font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w")
            self._badge(item, status, tone).pack(anchor="w", pady=(8, 0))

    # ── resources ─────────────────────────────────────────
    def _build_resources(self, page):
        body = self._body(page)
        self._section_heading(body, "独立资源层", "项目、运行环境、模型和工作流分别安装与升级")
        layers = tk.Frame(body, bg=self.c["bg"])
        layers.pack(fill="x", pady=(0, 14))
        for col in range(4):
            layers.columnconfigure(col, weight=1, uniform="resource_layers")
        data = [
            ("01", "客户端程序", "Git 管理", "界面、API 与任务逻辑", "primary"),
            ("02", "运行环境", "v1.0.0", "Python · PyTorch · ComfyUI", "success"),
            ("03", "模型资源", "独立目录", "不进入 Git 或通用环境包", "warn"),
            ("04", "工作流包", "3 个", "manifest · JSON · 依赖", "primary"),
        ]
        for col, (index, title, value, desc, tone) in enumerate(data):
            card = self._card(layers, 148)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0 if col == 3 else 4))
            head = tk.Frame(card, bg=self.c["card"])
            head.pack(fill="x", padx=13, pady=(12, 5))
            tk.Label(head, text=index, font=self.f["mono"], fg=self.c["muted"], bg=self.c["card"]).pack(side="left")
            self._badge(head, value, tone).pack(side="right")
            tk.Label(card, text=title, font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(anchor="w", padx=13, pady=(4, 5))
            tk.Label(card, text=desc, wraplength=190, justify="left", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", padx=13)

        self._section_heading(body, "版本与维护", "当前页面只展示资源关系，安装和维护动作仍从概览进入")
        maintenance = self._card(body, 196)
        maintenance.pack(fill="x", pady=(0, 14))
        rows = [
            ("客户端程序", "当前 main", "私有 GitHub 仓库", "可通过 Git 提交回滚", "查看版本"),
            ("运行环境", "runtime-v1.0.0", "私有 Release / 本地 7z", "已通过迁移冒烟测试", "检查环境"),
            ("模型目录", "用户本地保留", "models/", "不上传、不随卸载删除", "打开目录"),
        ]
        for index, (name, version, source, status, action) in enumerate(rows):
            row = tk.Frame(maintenance, bg=self.c["card"])
            row.pack(fill="x", padx=14, pady=(12 if index == 0 else 8, 7))
            tk.Label(row, text=name, width=14, anchor="w", font=self.f["bold"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=version, width=22, anchor="w", font=self.f["mono"], fg=self.c["primary"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=source, width=28, anchor="w", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left")
            tk.Label(row, text=status, anchor="w", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left", fill="x", expand=True)
            self._disabled_action(row, action).pack(side="right")
            if index < len(rows) - 1:
                tk.Frame(maintenance, bg=self.c["border2"], height=1).pack(fill="x", padx=14)

        note = self._card(body, 94)
        note.pack(fill="x")
        tk.Label(note, text="模型始终独立", font=self.f["h2"], fg=self.c["warn"], bg=self.c["card"]).pack(anchor="w", padx=15, pady=(13, 4))
        tk.Label(note, text="公开源码或更新客户端时，不上传、不重复打包现有模型；环境修复和卸载默认保留模型与用户结果。", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(anchor="w", padx=15)

    # ── settings ──────────────────────────────────────────
    def _build_settings(self, page):
        body = self._body(page)
        banner = self._card(body, 74)
        banner.pack(fill="x", pady=(0, 14))
        tk.Label(banner, text="设置页面预览", font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left", padx=(16, 10))
        tk.Label(banner, text="以下字段尚未保存到配置文件，仅用于确认信息结构。", font=self.f["small"], fg=self.c["text2"], bg=self.c["card"]).pack(side="left")
        self._badge(banner, "待接入", "warn").pack(side="right", padx=16)

        grid = tk.Frame(body, bg=self.c["bg"])
        grid.pack(fill="both", expand=True)
        for col in range(2):
            grid.columnconfigure(col, weight=1, uniform="settings_columns")
        for row in range(2):
            grid.rowconfigure(row, weight=1, uniform="settings_rows")

        cards = [
            (0, 0, "访问与安全", [
                ("客户端访问 Key", "••••••••••••••••", True),
                ("第三方 Key 保存", "Windows 本地加密", False),
                ("日志敏感信息", "自动掩码", False),
            ]),
            (0, 1, "任务与并发", [
                ("默认并发", "1 个任务", False),
                ("任务超时", "由工作流单独定义", False),
                ("重启恢复", "按执行器类型处理", False),
            ]),
            (1, 0, "存储与清理", [
                ("生成结果", "outputs/", True),
                ("任务数据", "runtime/requests/", True),
                ("自动清理", "关闭", False),
            ]),
            (1, 1, "默认路由", [
                ("文字模型", "local-text-default", True),
                ("图片模型", "local-image-default", True),
                ("视频模型", "local-video-default", True),
            ]),
        ]
        for row, col, title, values in cards:
            card = self._card(grid, 202)
            card.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 5, 0 if col == 1 else 5), pady=(0 if row == 0 else 5, 0 if row == 1 else 5))
            head = tk.Frame(card, bg=self.c["card"])
            head.pack(fill="x", padx=14, pady=(13, 8))
            tk.Label(head, text=title, font=self.f["h2"], fg=self.c["text"], bg=self.c["card"]).pack(side="left")
            self._badge(head, "静态预览").pack(side="right")
            for label, value, mono in values:
                field = tk.Frame(card, bg=self.c["hover"])
                field.pack(fill="x", padx=14, pady=4)
                tk.Label(field, text=label, width=16, anchor="w", font=self.f["small"], fg=self.c["text2"], bg=self.c["hover"], padx=9, pady=7).pack(side="left")
                tk.Label(field, text=value, anchor="e", font=self.f["mono"] if mono else self.f["small"], fg=self.c["text"], bg=self.c["hover"], padx=9).pack(side="right", fill="x", expand=True)

        footer = tk.Frame(body, bg=self.c["bg"])
        footer.pack(fill="x", pady=(14, 0))
        tk.Label(footer, text="设置接入前不会写入任何本地配置", font=self.f["small"], fg=self.c["muted"], bg=self.c["bg"]).pack(side="left")
        self._disabled_action(footer, "功能接入后可保存", 18).pack(side="right")

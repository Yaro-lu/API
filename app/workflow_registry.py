"""
工作流注册表 — 动态扫描根目录 workflows/ 文件夹，自动发现工作流

目录结构约定：
  workflows/                  ← 项目根目录下
    flux_t2i_v1/
      manifest.json           ← 必选（id, name, type, ...）
      workflow.json           ← 可选（ComfyUI API workflow）
      *_api.json              ← 原始 API workflow（保留备用）
      *.json                  ← 原始 ComfyUI workflow 导出
    wan_flf2v_v1/
      ...

URL 路由：
  /v1/workflows/run/{workflow_id} → 调用指定工作流
  /v1/workflows/run             → 调用默认工作流
"""
import json
from pathlib import Path
from typing import Any, List, Optional


# manifest["type"] → output_type
_TYPE_MAP = {
    "video.first_last_to_video": "video",
    "video.image_to_video": "video",
    "image.text_to_image": "image",
    "text.chat": "text",
}


class WorkflowDef:
    def __init__(
        self,
        id: str = "",
        name: str = "",
        enabled: bool = True,
        description: str = "",
        workflow_json: str = "",
        output_type: str = "",
        folder_name: str = "",
        input_schema: Optional[dict] = None,
        inputs: Optional[list] = None,
    ):
        self.id = id
        self.name = name
        self.enabled = enabled
        self.description = description
        self.workflow_json = workflow_json       # ComfyUI workflow JSON 相对路径
        self.output_type = output_type           # image / video / text / audio / 3d
        self.folder_name = folder_name           # 所属子文件夹名
        self.input_schema = input_schema or {}
        self.inputs = inputs or []
        self._workflows_dir: Optional[Path] = None  # 由 Registry 注入

    @property
    def folder(self) -> Optional[Path]:
        if self._workflows_dir and self.folder_name:
            return self._workflows_dir / self.folder_name
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "description": self.description,
            "workflow_json": self.workflow_json,
            "output_type": self.output_type,
            "folder_name": self.folder_name,
            "input_schema": self.input_schema,
            "inputs": self.inputs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowDef":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            enabled=d.get("enabled", True),
            description=d.get("description", ""),
            workflow_json=d.get("workflow_json", ""),
            output_type=d.get("output_type", ""),
            folder_name=d.get("folder_name", ""),
            input_schema=d.get("input_schema") or {},
            inputs=d.get("inputs") or [],
        )

    @classmethod
    def from_manifest(cls, folder: Path, manifest: dict) -> "WorkflowDef":
        """从 manifest.json 创建 WorkflowDef"""
        wf_id = manifest.get("id", folder.name)
        wf_type = manifest.get("type", "")
        output_type = _TYPE_MAP.get(wf_type, wf_type.split(".")[-1] if "." in wf_type else "")

        # 自动检测 workflow.json（存相对路径）
        wf_json_path = ""
        wf_json_file = folder / "workflow.json"
        if wf_json_file.exists():
            # 相对于 workflows 目录的路径
            wf_json_path = f"{folder.name}/workflow.json"

        return cls(
            id=wf_id,
            name=manifest.get("name", wf_id),
            enabled=True,
            description=manifest.get("description", manifest.get("name", "")),
            workflow_json=wf_json_path,
            output_type=output_type,
            folder_name=folder.name,
            input_schema=manifest.get("input_schema") or manifest.get("inputSchema") or {},
            inputs=manifest.get("inputs") or (manifest.get("input_schema") or {}).get("inputs") or [],
        )


class WorkflowRegistry:
    def __init__(self, config_path: Optional[Path] = None, workflows_dir: Optional[Path] = None):
        self.config_path = config_path
        self.workflows_dir = workflows_dir
        self.workflows: List[WorkflowDef] = []
        self.default_workflow_id: Optional[str] = None

        # 有配置文件 → 加载；否则尝试扫描文件夹
        if config_path and config_path.exists():
            self.load()
        elif workflows_dir and workflows_dir.exists():
            self.scan_folder(workflows_dir)

    # ══════════════════════════════════════════════════════
    # 动态扫描
    # ══════════════════════════════════════════════════════
    def scan_folder(self, folder: Optional[Path] = None) -> List[WorkflowDef]:
        """
        扫描文件夹，自动发现所有含 manifest.json 的子目录。
        返回新发现的工作流列表。
        """
        folder = folder or self.workflows_dir
        if not folder or not folder.exists():
            return []

        # 保留已有工作流的 enabled 状态和默认设置
        existing_map: dict[str, WorkflowDef] = {w.id: w for w in self.workflows}

        discovered: List[WorkflowDef] = []
        new_workflows: List[WorkflowDef] = []

        for subdir in sorted(folder.iterdir()):
            if not subdir.is_dir():
                continue
            manifest_file = subdir / "manifest.json"
            if not manifest_file.exists():
                continue

            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            wf = WorkflowDef.from_manifest(subdir, manifest)
            wf._workflows_dir = folder  # 注入根目录，供 folder 属性使用

            # 恢复用户设置
            existing = existing_map.get(wf.id)
            if existing:
                wf.enabled = existing.enabled
                wf.description = existing.description or wf.description

            discovered.append(wf)
            if wf.id not in existing_map:
                new_workflows.append(wf)

        self.workflows = discovered

        # 校验默认工作流
        if self.default_workflow_id and not self.get(self.default_workflow_id):
            enabled = self.enabled_workflows
            self.default_workflow_id = enabled[0].id if enabled else None

        self.save()
        return new_workflows

    # ══════════════════════════════════════════════════════
    # 持久化
    # ══════════════════════════════════════════════════════
    def load(self):
        if not self.config_path or not self.config_path.exists():
            self.workflows = []
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.workflows = [
            WorkflowDef.from_dict(w) for w in data.get("workflows", [])
        ]
        self.default_workflow_id = data.get("default_workflow_id")
        # 注入 workflows_dir
        for w in self.workflows:
            w._workflows_dir = self.workflows_dir

    def save(self):
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "workflows": [w.to_dict() for w in self.workflows],
            "default_workflow_id": self.default_workflow_id,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ══════════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════════
    def get(self, wf_id: str) -> Optional[WorkflowDef]:
        for wf in self.workflows:
            if wf.id == wf_id:
                return wf
        return None

    @property
    def enabled_workflows(self) -> List[WorkflowDef]:
        return [w for w in self.workflows if w.enabled]

    def resolve(self, workflow_id: Optional[str] = None) -> Optional[WorkflowDef]:
        """
        核心路由：
        - 指定 workflow_id → 查对应工作流（必须 enabled）
        - 未指定 → 返回默认工作流（或第一个 enabled）
        """
        if workflow_id:
            wf = self.get(workflow_id)
            return wf if (wf and wf.enabled) else None

        if self.default_workflow_id:
            fallback = self.get(self.default_workflow_id)
            if fallback and fallback.enabled:
                return fallback

        enabled = self.enabled_workflows
        return enabled[0] if enabled else None

    # ══════════════════════════════════════════════════════
    # 增删改
    # ══════════════════════════════════════════════════════
    def add(self, wf: WorkflowDef):
        self.workflows.append(wf)
        self.save()

    def remove(self, wf_id: str):
        self.workflows = [w for w in self.workflows if w.id != wf_id]
        if self.default_workflow_id == wf_id:
            self.default_workflow_id = None
        self.save()

    def update(self, wf_id: str, **kwargs):
        for wf in self.workflows:
            if wf.id == wf_id:
                for k, v in kwargs.items():
                    if hasattr(wf, k):
                        setattr(wf, k, v)
                self.save()
                return True
        return False

    def move_up(self, wf_id: str) -> bool:
        for i, wf in enumerate(self.workflows):
            if wf.id == wf_id and i > 0:
                self.workflows[i], self.workflows[i - 1] = (
                    self.workflows[i - 1], self.workflows[i],
                )
                self.save()
                return True
        return False

    def move_down(self, wf_id: str) -> bool:
        for i, wf in enumerate(self.workflows):
            if wf.id == wf_id and i < len(self.workflows) - 1:
                self.workflows[i], self.workflows[i + 1] = (
                    self.workflows[i + 1], self.workflows[i],
                )
                self.save()
                return True
        return False

    def set_default(self, wf_id: str):
        self.default_workflow_id = wf_id
        self.save()

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.workflow_registry import (
    WorkflowRegistry,
    merge_workflow_catalog,
    read_local_workflow_catalog,
)


class WorkflowRegistryTests(unittest.TestCase):
    def test_catalog_merge_keeps_local_workflows_and_overlays_live_status(self):
        local = [
            {
                "id": "image-local",
                "name": "本地图片",
                "type": "image.text_to_image",
                "capability": "text_to_image",
                "model_group": "Image Model",
            },
            {"id": "missing-local", "name": "缺模型也要显示"},
        ]
        remote = [
            {"id": "image-local", "name": "", "available": False, "missing_models": ["a.safetensors"]},
            {"id": "remote-only", "name": "用户工作流", "available": True},
        ]

        merged = merge_workflow_catalog(local, remote)

        self.assertEqual([item["id"] for item in merged], ["image-local", "missing-local", "remote-only"])
        self.assertEqual(merged[0]["name"], "本地图片")
        self.assertEqual(merged[0]["capability"], "text_to_image")
        self.assertFalse(merged[0]["available"])
        self.assertEqual(merged[0]["missing_models"], ["a.safetensors"])

    @staticmethod
    def _workflow(root: Path, workflow_id: str, *, enabled=True, output_type="image") -> Path:
        folder = root / workflow_id
        folder.mkdir(parents=True)
        manifest_type = {
            "text": "text.chat",
            "video": "video.image_to_video",
            "image": "image.text_to_image",
        }[output_type]
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "id": workflow_id,
                    "name": workflow_id,
                    "type": manifest_type,
                    "engine": "comfyui",
                    "enabled": enabled,
                }
            ),
            encoding="utf-8",
        )
        (folder / "workflow.json").write_text(
            json.dumps({"1": {"class_type": "SaveImage", "inputs": {}}}),
            encoding="utf-8",
        )
        return folder

    def test_manifest_disabled_and_default_invariants_persist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            self._workflow(workflows, "alpha", enabled=False)
            self._workflow(workflows, "beta", enabled=True)
            config = base / "runtime" / "workflow_config.json"

            registry = WorkflowRegistry(config, workflows)

            self.assertFalse(registry.get("alpha").enabled)
            self.assertEqual(registry.default_workflow_id, "beta")
            self.assertFalse(registry.set_default("alpha"))
            reloaded = WorkflowRegistry(config, workflows)
            self.assertEqual(reloaded.default_workflow_id, "beta")

    def test_disabling_and_removing_default_promotes_next_enabled_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            self._workflow(workflows, "alpha")
            self._workflow(workflows, "beta")
            config = base / "runtime" / "workflow_config.json"
            registry = WorkflowRegistry(config, workflows)

            self.assertEqual(registry.default_workflow_id, "alpha")
            self.assertTrue(registry.set_enabled("alpha", False))
            self.assertEqual(registry.default_workflow_id, "beta")
            registry.remove("beta")
            self.assertIsNone(registry.default_workflow_id)

    def test_scanning_new_workflow_never_steals_existing_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            self._workflow(workflows, "alpha")
            config = base / "runtime" / "workflow_config.json"
            registry = WorkflowRegistry(config, workflows)
            self.assertEqual(registry.default_workflow_id, "alpha")

            self._workflow(workflows, "new_image")
            registry.scan_folder()

            self.assertEqual(registry.default_workflow_id, "alpha")
            self.assertEqual([item.id for item in registry.workflows], ["alpha", "new_image"])

    def test_scan_never_publishes_an_active_import_staging_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            self._workflow(workflows, "alpha")
            self._workflow(workflows, ".importing_partial")

            registry = WorkflowRegistry(base / "runtime" / "workflow_config.json", workflows)

            self.assertEqual([item.id for item in registry.workflows], ["alpha"])

    def test_scan_rejects_a_reparse_workflows_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            config = base / "runtime" / "workflow_config.json"
            with mock.patch(
                "app.core.workflow_import._is_reparse_point",
                side_effect=lambda path: Path(path) == workflows,
            ):
                with self.assertRaises(ValueError):
                    WorkflowRegistry(config, workflows)

    def test_stale_registry_instance_cannot_restore_disabled_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            self._workflow(workflows, "alpha")
            self._workflow(workflows, "beta")
            config = base / "runtime" / "workflow_config.json"
            first = WorkflowRegistry(config, workflows)
            stale = WorkflowRegistry(config, workflows)

            self.assertTrue(first.set_enabled("beta", False))
            self.assertTrue(stale.set_default("alpha"))

            latest = WorkflowRegistry(config, workflows)
            self.assertFalse(latest.get("beta").enabled)
            self.assertEqual(latest.default_workflow_id, "alpha")

    def test_atomic_save_failure_keeps_previous_config_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            self._workflow(workflows, "alpha")
            config = base / "runtime" / "workflow_config.json"
            registry = WorkflowRegistry(config, workflows)
            previous = config.read_bytes()

            with mock.patch("app.workflow_registry.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    registry.set_enabled("alpha", False)

            self.assertEqual(config.read_bytes(), previous)
            self.assertEqual(list(config.parent.glob(".workflow_config.json.*.tmp")), [])

    def test_manifest_metadata_survives_scan_save_and_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            folder = self._workflow(workflows, "klein")
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            manifest.update(
                {
                    "type": "image.text_image_to_image",
                    "capability": "text_image_to_image",
                    "model_group": "Flux2 Klein 4B",
                    "workflow_variants": {
                        "text_to_image": "workflow_t2i.json",
                        "image_to_image": "workflow.json",
                        "unsafe": "../outside.json",
                    },
                }
            )
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (folder / "workflow_t2i.json").write_text("{}", encoding="utf-8")
            config = base / "runtime" / "workflow_config.json"

            registry = WorkflowRegistry(config, workflows)
            reloaded = WorkflowRegistry(config, workflows)
            workflow = reloaded.get("klein")

            self.assertEqual(workflow.workflow_type, "image.text_image_to_image")
            self.assertEqual(workflow.capability, "text_image_to_image")
            self.assertEqual(workflow.model_group, "Flux2 Klein 4B")
            self.assertEqual(
                workflow.workflow_variants,
                {
                    "text_to_image": "workflow_t2i.json",
                    "image_to_image": "workflow.json",
                },
            )

    def test_local_catalog_lists_missing_model_workflows_without_writing_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            workflows = base / "workflows"
            workflows.mkdir()
            folder = self._workflow(workflows, "missing_model")
            (folder / "workflow.json").write_text(
                json.dumps(
                    {
                        "1": {
                            "class_type": "UNETLoader",
                            "inputs": {"unet_name": "not-installed.safetensors"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = base / "runtime" / "workflow_config.json"

            records = read_local_workflow_catalog(workflows, config)

            self.assertEqual([item["id"] for item in records], ["missing_model"])
            self.assertEqual(
                records[0]["dependencies"]["models"][0]["name"],
                "not-installed.safetensors",
            )
            self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()

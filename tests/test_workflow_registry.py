import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.workflow_registry import WorkflowRegistry


class WorkflowRegistryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

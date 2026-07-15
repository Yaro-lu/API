import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.model_maintenance import (
    check_model_groups,
    cleanup_incomplete_imports,
    import_model_directory,
    model_file_ready,
)


FIXTURE_REQUIREMENTS = {
    "Fixture": {
        "title": "Fixture",
        "items": [
            {
                "path": "diffusion_models/known.safetensors",
                "size_bytes": len(b"model"),
            }
        ],
    }
}


class ModelMaintenanceTests(unittest.TestCase):
    def test_cleanup_removes_only_interrupted_import_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            incomplete = models / "diffusion_models" / ".model.safetensors.importing"
            incomplete.parent.mkdir(parents=True)
            incomplete.write_bytes(b"partial")
            resumable = models / "diffusion_models" / "model.safetensors.part"
            resumable.write_bytes(b"partial")

            self.assertEqual(cleanup_incomplete_imports(models), 1)
            self.assertFalse(incomplete.exists())
            self.assertTrue(resumable.exists())

    def test_known_model_requires_exact_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = Path(tmp)
            target = models / "diffusion_models" / "known.safetensors"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bad")

            self.assertFalse(model_file_ready(target, len(b"model")))
            self.assertFalse(check_model_groups(models, FIXTURE_REQUIREMENTS)["all_ok"])

            target.write_bytes(b"model")
            self.assertTrue(check_model_groups(models, FIXTURE_REQUIREMENTS)["all_ok"])

    def test_import_routes_known_models_and_flattens_models_root(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source = Path(source_tmp)
            models = Path(target_tmp)
            known = source / "downloads" / "known.safetensors"
            known.parent.mkdir(parents=True)
            known.write_bytes(b"model")
            extra = source / "models" / "loras" / "extra.gguf"
            extra.parent.mkdir(parents=True)
            extra.write_bytes(b"extra")

            result = import_model_directory(source, models, FIXTURE_REQUIREMENTS)

            self.assertEqual(result["imported"], 2)
            self.assertEqual(
                (models / "diffusion_models" / "known.safetensors").read_bytes(),
                b"model",
            )
            self.assertEqual((models / "loras" / "extra.gguf").read_bytes(), b"extra")
            self.assertFalse(any(models.rglob("*.importing")))

    def test_import_failure_keeps_existing_destination_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source = Path(source_tmp)
            models = Path(target_tmp)
            source_file = source / "known.safetensors"
            source_file.write_bytes(b"model")
            destination = models / "diffusion_models" / "known.safetensors"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")

            with mock.patch(
                "app.core.model_maintenance.shutil.copy2",
                side_effect=OSError("disk full"),
            ):
                result = import_model_directory(source, models, FIXTURE_REQUIREMENTS)

            self.assertEqual(result["failed"], 1)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(any(models.rglob("*.importing")))

    def test_unknown_root_model_is_reported_unclassified(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source = Path(source_tmp)
            models = Path(target_tmp)
            (source / "mystery.gguf").write_bytes(b"model")

            result = import_model_directory(source, models, FIXTURE_REQUIREMENTS)

            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["imported"], 0)
            self.assertFalse((models / "mystery.gguf").exists())
            self.assertIn("无法判断模型类型", result["errors"][0])

    def test_selecting_a_comfy_category_routes_root_models(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source = Path(source_tmp) / "loras"
            source.mkdir()
            models = Path(target_tmp)
            (source / "custom.safetensors").write_bytes(b"lora")

            result = import_model_directory(source, models, FIXTURE_REQUIREMENTS)

            self.assertEqual(result["imported"], 1)
            self.assertEqual((models / "loras" / "custom.safetensors").read_bytes(), b"lora")

    def test_explicit_models_root_keeps_custom_node_categories(self):
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source = Path(source_tmp)
            models = Path(target_tmp)
            custom = source / "models" / "ipadapter" / "adapter.bin"
            custom.parent.mkdir(parents=True)
            custom.write_bytes(b"adapter")

            result = import_model_directory(source, models, FIXTURE_REQUIREMENTS)

            self.assertEqual(result["imported"], 1)
            self.assertEqual((models / "ipadapter" / "adapter.bin").read_bytes(), b"adapter")


if __name__ == "__main__":
    unittest.main()

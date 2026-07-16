import tempfile
import unittest
from pathlib import Path

from app.core.workflow_dependencies import (
    clear_model_index_cache,
    extract_workflow_dependencies,
    normalize_workflow_dependencies,
    workflow_dependency_report,
)


class WorkflowDependencyTests(unittest.TestCase):
    def tearDown(self):
        clear_model_index_cache()

    def test_detects_model_loaders_and_node_types_without_treating_input_images_as_models(self):
        workflow = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "checkpoints/model.safetensors"},
            },
            "2": {
                "class_type": "LoadImage",
                "inputs": {"image": "input.png"},
            },
        }

        dependencies = extract_workflow_dependencies(workflow)

        self.assertEqual(
            [item["source"] for item in dependencies["models"]],
            ["checkpoints/model.safetensors"],
        )
        self.assertEqual(
            dependencies["nodes"],
            ["CheckpointLoaderSimple", "LoadImage"],
        )

    def test_dependency_metadata_is_sanitized_and_bounded(self):
        long_value = "x" * 1000 + ".safetensors"
        normalized = normalize_workflow_dependencies(
            {
                "models": [
                    {
                        "source": long_value,
                        "name": long_value,
                        "extra": "secret" * 1000,
                    }
                ],
                "nodes": "SingleNode",
            }
        )

        self.assertEqual(normalized["models"], [])
        self.assertEqual(normalized["nodes"], ["SingleNode"])
        self.assertNotIn("extra", str(normalized))

    def test_automatic_detection_respects_dependency_limit(self):
        workflow = {
            str(index): {
                "class_type": f"Loader{index}",
                "inputs": {"model_name": f"model-{index}.safetensors"},
            }
            for index in range(4200)
        }

        dependencies = extract_workflow_dependencies(workflow)

        self.assertEqual(len(dependencies["models"]), 4096)
        self.assertEqual(len(dependencies["nodes"]), 4096)

    def test_declared_model_requires_exact_path_and_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models = Path(temp_dir)
            wrong_category = models / "vae" / "model.safetensors"
            wrong_category.parent.mkdir(parents=True)
            wrong_category.write_bytes(b"1234")
            dependencies = {
                "models": [
                    {
                        "source": "diffusion_models/model.safetensors",
                        "size_bytes": 4,
                    }
                ]
            }

            report = workflow_dependency_report(
                dependencies,
                models,
                installed_nodes=set(),
                cache_seconds=0,
            )
            self.assertEqual(report["dependency_status"], "missing")

            correct = models / "diffusion_models" / "model.safetensors"
            correct.parent.mkdir(parents=True)
            correct.write_bytes(b"x")
            report = workflow_dependency_report(
                dependencies,
                models,
                installed_nodes=set(),
                cache_seconds=0,
            )
            self.assertEqual(report["dependency_status"], "missing")

            correct.write_bytes(b"1234")
            report = workflow_dependency_report(
                dependencies,
                models,
                installed_nodes=set(),
                cache_seconds=0,
            )
            self.assertEqual(report["dependency_status"], "ready")
            self.assertTrue(report["available"])

    def test_friendly_model_name_does_not_replace_the_source_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models = Path(temp_dir)
            (models / "actual.safetensors").write_bytes(b"model")

            report = workflow_dependency_report(
                {
                    "models": [
                        {
                            "name": "Friendly model label",
                            "source": "actual.safetensors",
                        }
                    ]
                },
                models,
                installed_nodes=set(),
                cache_seconds=0,
                model_requirements={},
            )

            self.assertEqual(report["required_models"], ["Friendly model label"])
            self.assertEqual(report["missing_models"], [])
            self.assertEqual(report["dependency_status"], "ready")

    def test_node_state_transitions_from_unverified_to_missing_to_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dependencies = {"nodes": ["RequiredNode"]}
            models = Path(temp_dir)

            unverified = workflow_dependency_report(dependencies, models)
            missing = workflow_dependency_report(
                dependencies,
                models,
                installed_nodes={"OtherNode"},
            )
            ready = workflow_dependency_report(
                dependencies,
                models,
                installed_nodes={"RequiredNode"},
            )

            self.assertEqual(unverified["dependency_status"], "unverified")
            self.assertEqual(missing["missing_nodes"], ["RequiredNode"])
            self.assertEqual(missing["dependency_status"], "missing")
            self.assertEqual(ready["dependency_status"], "ready")

    def test_known_bundled_model_uses_its_exact_contract_path_and_size(self):
        requirements = {
            "Known": {
                "items": [
                    {
                        "path": "diffusion_models/known.safetensors",
                        "size_bytes": 4,
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            models = Path(temp_dir)
            wrong = models / "vae" / "known.safetensors"
            wrong.parent.mkdir(parents=True)
            wrong.write_bytes(b"1234")

            report = workflow_dependency_report(
                {"models": ["known.safetensors"]},
                models,
                installed_nodes=set(),
                cache_seconds=0,
                model_requirements=requirements,
            )
            self.assertEqual(report["dependency_status"], "missing")

            correct = models / "diffusion_models" / "known.safetensors"
            correct.parent.mkdir(parents=True)
            correct.write_bytes(b"1234")
            report = workflow_dependency_report(
                {"models": ["known.safetensors"]},
                models,
                installed_nodes=set(),
                cache_seconds=0,
                model_requirements=requirements,
            )
            self.assertEqual(report["dependency_status"], "ready")

    def test_declared_hash_is_never_claimed_verified_by_health_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models = Path(temp_dir)
            model = models / "model.safetensors"
            model.write_bytes(b"model")

            report = workflow_dependency_report(
                {
                    "models": [
                        {
                            "source": "model.safetensors",
                            "sha256": "a" * 64,
                        }
                    ]
                },
                models,
                installed_nodes=set(),
                cache_seconds=0,
            )

            self.assertEqual(report["unverified_models"], ["model.safetensors"])
            self.assertEqual(report["dependency_status"], "unverified")
            self.assertFalse(report["available"])


if __name__ == "__main__":
    unittest.main()

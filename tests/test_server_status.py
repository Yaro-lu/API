import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import server
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS


FIXTURE_REQUIREMENTS = {
    "Qwen3.5": {
        "items": [{"path": "text_encoders/qwen.safetensors", "size_bytes": 5}],
    },
    "Flux2": {
        "items": [{"path": "diffusion_models/flux.safetensors", "size_bytes": 5}],
    },
    "Wan2.1": {
        "items": [{"path": "diffusion_models/wan.safetensors", "size_bytes": 5}],
    },
}


class ServerStatusTests(unittest.TestCase):
    def test_wrong_size_models_are_not_published_as_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for spec in FIXTURE_REQUIREMENTS.values():
                target = base / "models" / spec["items"][0]["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x")

            status = server._local_model_status(base, FIXTURE_REQUIREMENTS)

            self.assertEqual(status, {"qwen35": False, "flux2": False, "wan21": False})
            with (
                mock.patch.object(server, "BASE_DIR", base),
                mock.patch.object(server, "MODEL_REQUIREMENTS", FIXTURE_REQUIREMENTS),
            ):
                self.assertFalse(server._workflow_available("qwen-text", "text_chat"))
                self.assertFalse(server._workflow_available("flux-image", "image"))
                self.assertFalse(server._workflow_available("wan-video", "video"))

    def test_runtime_status_requires_every_nonempty_core_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for relative in REQUIRED_RUNTIME_PATHS:
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"runtime")

            self.assertEqual(server._local_runtime_status(base)["status"], "installed")

            python_path = base / REQUIRED_RUNTIME_PATHS[0]
            python_path.write_bytes(b"")
            status = server._local_runtime_status(base)

            self.assertEqual(status["status"], "missing")
            self.assertFalse(status["python"])
            self.assertIn(REQUIRED_RUNTIME_PATHS[0].as_posix(), status["missing"])


if __name__ == "__main__":
    unittest.main()

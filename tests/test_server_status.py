import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import server
from app.core.runtime_package import REQUIRED_RUNTIME_PATHS
from app.core.workflow_dependencies import clear_model_index_cache
from app.workflow_registry import WorkflowDef


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
    def tearDown(self):
        clear_model_index_cache()
        with server._comfy_nodes_lock:
            server._comfy_nodes_cache.update(
                {"url": "", "checked_at": 0.0, "nodes": None}
            )

    def test_stale_public_url_is_hidden_while_tunnel_is_offline(self):
        fake_state = SimpleNamespace(
            base_url="https://stale.example",
            local_api="http://127.0.0.1:18188",
        )
        fake_tunnel = SimpleNamespace(
            is_online=False,
            state=SimpleNamespace(status="retrying", base_url="", error=""),
        )

        with (
            mock.patch.object(server, "state", fake_state),
            mock.patch.object(server, "tunnel", fake_tunnel),
        ):
            self.assertEqual(server._active_public_base_url(), "")
            self.assertEqual(server._public_base_url(), fake_state.local_api)

    def test_tunnel_health_status_preserves_actionable_states(self):
        fake_tunnel = SimpleNamespace(
            is_online=False,
            state=SimpleNamespace(status="offline"),
        )
        expected = {
            "starting": "starting",
            "retrying": "retrying",
            "failed": "unavailable",
            "offline": "offline",
        }

        with mock.patch.object(server, "tunnel", fake_tunnel):
            for raw, published in expected.items():
                with self.subTest(status=raw):
                    fake_tunnel.state.status = raw
                    self.assertEqual(server._tunnel_health_status(), published)

    def test_tunnel_restart_clears_stale_state_before_manager_restart(self):
        calls = []

        class FakeState:
            def set_offline(self):
                calls.append("offline")

        class FakeTunnel:
            is_online = False
            state = SimpleNamespace(status="starting", base_url="", error="")

            def restart(self):
                calls.append("restart")
                return True

        with (
            mock.patch.object(server, "state", FakeState()),
            mock.patch.object(server, "tunnel", FakeTunnel()),
        ):
            result = asyncio.run(server._restart_tunnel_manager())

        self.assertEqual(calls, ["offline", "restart"])
        self.assertTrue(result["ok"])

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

    def test_invalid_workflow_json_is_never_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            folder = base / "workflows" / "broken"
            folder.mkdir(parents=True)
            (folder / "workflow.json").write_text("{broken", encoding="utf-8")
            workflow = WorkflowDef(
                id="broken",
                name="Broken",
                folder_name="broken",
                workflow_json="broken/workflow.json",
                dependencies={"nodes": ["SaveImage"]},
            )
            workflow._workflows_dir = base / "workflows"

            with (
                mock.patch.object(server, "BASE_DIR", base),
                mock.patch.object(server, "registry", SimpleNamespace(default_workflow_id="broken")),
                mock.patch.object(server, "_installed_comfy_node_types", return_value={"SaveImage"}),
            ):
                payload = server._workflow_payload(workflow)

            self.assertEqual(payload["validation_status"], "file_error")
            self.assertFalse(payload["available"])

    def test_workflow_payload_moves_from_missing_to_ready_and_reports_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            folder = base / "workflows" / "image"
            folder.mkdir(parents=True)
            (folder / "workflow.json").write_text(
                '{"1":{"class_type":"CheckpointLoaderSimple","inputs":{}}}',
                encoding="utf-8",
            )
            workflow = WorkflowDef(
                id="image",
                name="Image",
                folder_name="image",
                workflow_json="image/workflow.json",
                output_type="image",
                dependencies={
                    "models": ["diffusion_models/image.safetensors"],
                    "nodes": ["CheckpointLoaderSimple"],
                },
            )
            workflow._workflows_dir = base / "workflows"
            patches = (
                mock.patch.object(server, "BASE_DIR", base),
                mock.patch.object(server, "registry", SimpleNamespace(default_workflow_id="image")),
                mock.patch.object(
                    server,
                    "_installed_comfy_node_types",
                    return_value={"CheckpointLoaderSimple"},
                ),
            )
            with patches[0], patches[1], patches[2]:
                missing = server._workflow_payload(workflow)
                model = base / "models" / "diffusion_models" / "image.safetensors"
                model.parent.mkdir(parents=True)
                model.write_bytes(b"model")
                clear_model_index_cache()
                ready = server._workflow_payload(workflow)
                workflow.enabled = False
                disabled = server._workflow_payload(workflow)

            self.assertEqual(missing["dependency_status"], "missing")
            self.assertEqual(missing["missing_models"], ["image.safetensors"])
            self.assertTrue(ready["available"])
            self.assertTrue(ready["is_default"])
            self.assertEqual(disabled["validation_status"], "disabled")
            self.assertFalse(disabled["available"])

    def test_failed_comfy_node_probe_recovers_on_short_retry(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"SaveImage": {}, "LoadImage": {}}
        fake_config = SimpleNamespace(comfyui_url="http://127.0.0.1:8188")

        with (
            mock.patch.object(server, "config", fake_config),
            mock.patch.object(server.time, "monotonic", side_effect=[0.0, 2.0]),
            mock.patch("requests.get", side_effect=[OSError("starting"), response]) as get,
        ):
            first = server._installed_comfy_node_types(cache_seconds=30)
            second = server._installed_comfy_node_types(cache_seconds=30)

        self.assertIsNone(first)
        self.assertEqual(second, {"SaveImage", "LoadImage"})
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()

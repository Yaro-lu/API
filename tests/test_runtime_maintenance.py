import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.gui import main_gateway
from app.gui.main_gateway import GatewayApp


class RuntimeMaintenanceTests(unittest.TestCase):
    @staticmethod
    def _app():
        app = object.__new__(GatewayApp)
        app._runtime_maintenance_lock = threading.RLock()
        app._runtime_maintenance_in_progress = False
        app._shutting_down = False
        return app

    def test_maintenance_guard_records_and_releases_owned_services(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.side_effect = lambda role: role == "comfyui"
        app._stop_runtime_for_maintenance = mock.Mock(return_value="")
        app.after = mock.Mock()

        started, running_before, error = app._begin_runtime_maintenance()

        self.assertTrue(started)
        self.assertEqual(running_before, {"comfyui"})
        self.assertEqual(error, "")
        self.assertTrue(app._runtime_maintenance_active())

        app._end_runtime_maintenance(restart=False)
        self.assertFalse(app._runtime_maintenance_active())
        app.after.assert_not_called()

    def test_service_launch_is_rejected_while_runtime_is_replaced(self):
        app = self._app()
        app._runtime_maintenance_in_progress = True
        app._start_comfyui_service_unlocked = mock.Mock()
        app._start_api_service_unlocked = mock.Mock()

        app._start_comfyui_service()
        app._start_api_service()

        app._start_comfyui_service_unlocked.assert_not_called()
        app._start_api_service_unlocked.assert_not_called()

    def test_runtime_probe_is_rejected_while_replacement_is_active(self):
        app = self._app()
        app._runtime_maintenance_in_progress = True
        app._process_supervisor = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "正在维护"):
            app._run_runtime_probe("torch-check", ["python", "-V"])

        app._process_supervisor.run.assert_not_called()

    def test_startup_does_not_launch_torch_probe_after_maintenance_begins(self):
        app = self._app()
        app.after = lambda _delay, _callback: None

        def begin_maintenance(_runner):
            app._runtime_maintenance_in_progress = True
            return {"success": True, "gpu_name": "fixture"}

        with (
            mock.patch.object(main_gateway, "_check_system_env", side_effect=begin_maintenance),
            mock.patch.object(main_gateway, "_check_torch") as check_torch,
        ):
            app._startup_sequence()

        check_torch.assert_not_called()

    def test_maintenance_end_schedules_backend_restore(self):
        app = self._app()
        callbacks = []
        app.after = lambda _delay, callback: callbacks.append(callback)
        app._startup_sequence = mock.Mock()
        thread = mock.Mock()

        with mock.patch.object(threading, "Thread", return_value=thread) as thread_factory:
            app._end_runtime_maintenance(restart=True)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()

        thread_factory.assert_called_once()
        thread.start.assert_called_once_with()

    def test_empty_model_placeholder_is_reported_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            paths = [
                "text_encoders/qwen3.5_4b_bf16.safetensors",
                "diffusion_models/flux-2-klein-9b-fp8.safetensors",
                "text_encoders/qwen_3_8b_fp8mixed.safetensors",
                "vae/full_encoder_small_decoder.safetensors",
                "diffusion_models/wan2.1_flf2v_720p_14B_fp16.safetensors",
                "vae/wan_2.1_vae.safetensors",
                "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "clip_vision/clip_vision_h.safetensors",
            ]
            for relative in paths:
                path = base / "models" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")

            with mock.patch.object(main_gateway, "BASE_DIR", base):
                status = main_gateway._check_models_status()
                self.assertFalse(status["all_ok"])
                self.assertEqual(status["Qwen3.5"], "缺失")

                for relative in paths:
                    (base / "models" / relative).write_bytes(b"model")
                status = main_gateway._check_models_status()

            self.assertTrue(status["all_ok"])


if __name__ == "__main__":
    unittest.main()

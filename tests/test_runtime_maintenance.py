import tempfile
import threading
import time
import unittest
import io
import json
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
        app._backend_action_lock = threading.Lock()
        app._backend_action_name = ""
        app._shutting_down = False
        app._last_health = {}
        return app

    def test_system_environment_rejects_pre_cuda_13_driver(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout="NVIDIA RTX 4090, 576.80, 24564\n",
            )
        )

        self.assertFalse(result["success"])
        self.assertIn("R580", result["error"])
        self.assertEqual(result["driver_version"], "576.80")

    def test_system_environment_accepts_cuda_13_driver(self):
        result = main_gateway._check_system_env(
            lambda *args, **kwargs: mock.Mock(
                returncode=0,
                stdout="NVIDIA RTX 4090, 580.88, 24564\n",
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["driver_version"], "580.88")

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

    def test_maintenance_guard_releases_when_service_stop_raises(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.return_value = False
        app._stop_runtime_for_maintenance = mock.Mock(
            side_effect=OSError("process query failed")
        )

        started, running_before, error = app._begin_runtime_maintenance()

        self.assertFalse(started)
        self.assertEqual(running_before, set())
        self.assertIn("停止后台服务失败", error)
        self.assertIn("process query failed", error)
        self.assertFalse(app._runtime_maintenance_active())

    def test_maintenance_status_query_does_not_wait_for_slow_service_stop(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.is_running.return_value = True
        stop_started = threading.Event()
        allow_stop = threading.Event()

        def slow_stop():
            stop_started.set()
            allow_stop.wait(2)
            return ""

        app._stop_runtime_for_maintenance = slow_stop
        result = []
        worker = threading.Thread(
            target=lambda: result.append(app._begin_runtime_maintenance()),
            daemon=True,
        )
        worker.start()
        self.assertTrue(stop_started.wait(1))

        started_at = time.monotonic()
        self.assertTrue(app._runtime_maintenance_active())
        self.assertLess(time.monotonic() - started_at, 0.2)

        allow_stop.set()
        worker.join(1)
        self.assertEqual(result[0][0], True)

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
            mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]),
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
            requirements = {
                group: {
                    "title": spec["title"],
                    "items": [
                        {"path": item["path"], "size_bytes": len(b"model")}
                        for item in spec["items"]
                    ],
                }
                for group, spec in main_gateway.MODEL_REQUIREMENTS.items()
            }
            paths = [
                item["path"]
                for spec in requirements.values()
                for item in spec["items"]
            ]
            for relative in paths:
                path = base / "models" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")

            with (
                mock.patch.object(main_gateway, "BASE_DIR", base),
                mock.patch.object(main_gateway, "MODEL_REQUIREMENTS", requirements),
            ):
                status = main_gateway._check_models_status()
                self.assertFalse(status["all_ok"])
                self.assertEqual(status["Qwen3.5"], "缺失")

                for relative in paths:
                    (base / "models" / relative).write_bytes(b"model")
                status = main_gateway._check_models_status()

            self.assertTrue(status["all_ok"])

    def test_resuming_model_download_is_still_an_active_background_task(self):
        self.assertTrue(main_gateway._model_download_active({"state": "downloading"}))
        self.assertTrue(main_gateway._model_download_active({"state": "resuming"}))
        self.assertTrue(main_gateway._model_download_active({"state": "abandoning"}))
        self.assertFalse(main_gateway._model_download_active({"state": "paused"}))

    def test_startup_failure_is_not_marked_ready_and_does_not_start_backend(self):
        app = self._app()
        app._set_light = mock.Mock()
        app._footer_label = mock.Mock()
        app._dashboard_pages = mock.Mock()
        app._update_model_display = mock.Mock()
        app._start_backend = mock.Mock()
        app.after = lambda _delay, callback: callback()

        model_status = {
            "Qwen3.5": "缺失",
            "Flux2": "缺失",
            "Wan2.1": "缺失",
            "all_ok": False,
            "missing": {"Qwen3.5": [], "Flux2": [], "Wan2.1": []},
        }
        with (
            mock.patch.object(main_gateway, "missing_runtime_paths", return_value=[]),
            mock.patch.object(
                main_gateway,
                "_check_system_env",
                return_value={"success": False, "error": "NVIDIA 驱动不可用"},
            ),
            mock.patch.object(
                main_gateway,
                "_check_torch",
                return_value={"success": True, "gpu_name": "fixture"},
            ),
            mock.patch.object(main_gateway, "_check_models_status", return_value=model_status),
            mock.patch.object(main_gateway, "_ensure_extra_model_paths"),
        ):
            app._startup_sequence()

        self.assertFalse(app._environment_status["ready"])
        app._start_backend.assert_not_called()
        self.assertIn(
            mock.call("env", "offline", "需处理"),
            app._set_light.call_args_list,
        )

    def test_missing_runtime_opens_maintenance_center(self):
        app = self._app()
        app._set_light = mock.Mock()
        app._footer_label = mock.Mock()
        app._dashboard_pages = mock.Mock()
        app._open_runtime_maintenance = mock.Mock()
        app.after = lambda _delay, callback: callback()

        with (
            mock.patch.object(
                main_gateway,
                "missing_runtime_paths",
                return_value=["runtime/python/python.exe"],
            ),
            mock.patch.object(main_gateway, "_check_system_env") as check_system,
        ):
            app._startup_sequence()

        app._open_runtime_maintenance.assert_called_once_with()
        check_system.assert_not_called()
        for key in ("comfyui", "api", "tunnel"):
            self.assertIn(
                mock.call(key, "offline", "等待安装环境"),
                app._set_light.call_args_list,
            )

    def test_maintenance_is_blocked_by_external_runtime_listener(self):
        app = self._app()
        app._process_supervisor = mock.Mock()
        app._process_supervisor.terminate.return_value = ""
        app._process_supervisor.is_running.return_value = False
        app._process_supervisor.prepare_port.side_effect = [
            (False, "端口 8188 已被其他程序占用 (PID 24680)"),
            (True, ""),
        ]

        error = app._stop_runtime_for_maintenance()

        self.assertIn("24680", error)
        self.assertIn("手动关闭", error)

    def test_model_download_promotes_only_expected_size(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            status = 200

            def __init__(self, payload: bytes):
                super().__init__(payload)
                self.headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {"size_bytes": len(b"model")},
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with mock.patch("urllib.request.urlopen", return_value=Response(b"model")):
                app._download_model_file(control)

            self.assertEqual(target.read_bytes(), b"model")
            app._finish_model_download.assert_called_once_with(control)
            app._fail_model_download.assert_not_called()

    def test_model_download_keeps_incomplete_file_as_partial(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            status = 200

            def __init__(self):
                super().__init__(b"bad")
                self.headers = {"Content-Length": "5", "ETag": '"fixture"'}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {"size_bytes": 5},
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with (
                mock.patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: Response()),
                mock.patch.object(main_gateway.time, "sleep"),
            ):
                app._download_model_file(control)

            self.assertFalse(target.exists())
            self.assertTrue(target.with_suffix(target.suffix + ".part").exists())
            app._finish_model_download.assert_not_called()
            app._fail_model_download.assert_called_once()

    def test_model_download_promotes_already_complete_partial_without_network(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            partial = target.with_suffix(target.suffix + ".part")
            partial.write_bytes(b"model")
            partial.with_suffix(partial.suffix + ".json").write_text(
                json.dumps(
                    {
                        "url": "https://example.invalid/model.safetensors",
                        "expected_size": len(b"model"),
                        "validator": '"fixture"',
                    }
                ),
                encoding="utf-8",
            )
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {"size_bytes": len(b"model")},
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with mock.patch("urllib.request.urlopen") as urlopen:
                app._download_model_file(control)

            urlopen.assert_not_called()
            self.assertEqual(target.read_bytes(), b"model")
            self.assertFalse(partial.exists())
            app._finish_model_download.assert_called_once_with(control)

    def test_model_download_never_combines_untrusted_prefix_with_valid_tail(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._finish_model_download = mock.Mock()
        app._fail_model_download = mock.Mock()
        app._update_model_download_progress = mock.Mock()

        class Response(io.BytesIO):
            def __init__(self, payload: bytes, status: int, headers: dict):
                super().__init__(payload)
                self.status = status
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        responses = [
            Response(b"XX", 200, {"Content-Length": "2", "ETag": '"bad"'}),
            Response(b"cde", 206, {"Content-Length": "3", "Content-Range": "bytes 2-4/5", "ETag": '"good"'}),
            Response(b"model", 200, {"Content-Length": "5", "ETag": '"good"'}),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "model.safetensors"
            control = {
                "url": "https://example.invalid/model.safetensors",
                "target": target,
                "item": {"size_bytes": 5},
                "pause_event": threading.Event(),
                "status_var": mock.Mock(),
            }
            with (
                mock.patch("urllib.request.urlopen", side_effect=responses),
                mock.patch.object(main_gateway.time, "sleep"),
            ):
                app._download_model_file(control)

            self.assertEqual(target.read_bytes(), b"model")
            app._finish_model_download.assert_called_once_with(control)
            app._fail_model_download.assert_not_called()

    def test_model_resume_waits_for_paused_worker_to_exit(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        app._download_model_file = mock.Mock()
        pause_event = threading.Event()
        pause_seen = threading.Event()
        allow_exit = threading.Event()

        def old_download():
            pause_event.wait(2)
            pause_seen.set()
            allow_exit.wait(2)

        old_worker = threading.Thread(target=old_download, daemon=True)
        old_worker.start()
        control = {
            "state": "downloading",
            "target": Path(tempfile.gettempdir()) / "lingjing-resume-fixture.safetensors",
            "pause_event": pause_event,
            "stop_event": threading.Event(),
            "status_var": mock.Mock(),
            "button": mock.Mock(),
            "pause_button": mock.Mock(),
            "worker": old_worker,
        }

        app._pause_model_download(control)
        self.assertTrue(pause_seen.wait(1))
        app._start_model_download(control)
        self.assertEqual(control["state"], "resuming")
        app._download_model_file.assert_not_called()

        allow_exit.set()
        old_worker.join(1)
        deadline = time.time() + 1
        while time.time() < deadline and not app._download_model_file.called:
            time.sleep(0.01)

        app._download_model_file.assert_called_once_with(control)
        self.assertEqual(control["state"], "downloading")

    def test_abandon_paused_download_releases_target_only_after_writer_exits(self):
        app = self._app()
        app.after = lambda _delay, callback: callback()
        pause_event = threading.Event()
        pause_seen = threading.Event()
        allow_exit = threading.Event()
        done = threading.Event()
        target = Path(tempfile.gettempdir()) / "lingjing-abandon-fixture.safetensors"

        def old_download():
            pause_event.wait(2)
            pause_seen.set()
            allow_exit.wait(2)

        old_worker = threading.Thread(target=old_download, daemon=True)
        old_worker.start()
        control = {
            "state": "downloading",
            "target": target,
            "pause_event": pause_event,
            "status_var": mock.Mock(),
            "button": mock.Mock(),
            "pause_button": mock.Mock(),
            "worker": old_worker,
        }
        self.assertTrue(app._claim_model_transfer(control))
        app._pause_model_download(control)
        self.assertTrue(pause_seen.wait(1))

        app._abandon_paused_model_download(control, done.set)

        self.assertEqual(control["state"], "abandoning")
        self.assertTrue(app._model_transfer_active(target))
        self.assertFalse(done.is_set())
        allow_exit.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(control["state"], "abandoned")
        self.assertFalse(app._model_transfer_active(target))

    def test_same_model_target_cannot_start_twice(self):
        app = self._app()
        app._download_model_file = mock.Mock()
        target = Path(tempfile.gettempdir()) / "lingjing-transfer-fixture.safetensors"

        def control():
            return {
                "state": "idle",
                "target": target,
                "pause_event": threading.Event(),
                "stop_event": threading.Event(),
                "status_var": mock.Mock(),
                "button": mock.Mock(),
                "pause_button": mock.Mock(),
                "worker": None,
            }

        first = control()
        second = control()
        app._start_model_download(first)
        app._start_model_download(second)

        self.assertEqual(first["state"], "downloading")
        self.assertEqual(second["state"], "idle")
        self.assertIs(app._active_model_transfers[app._model_transfer_key(target)], first)


if __name__ == "__main__":
    unittest.main()
